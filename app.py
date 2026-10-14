import os, re, json, time, math, threading
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, send_from_directory

CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "BelgorodDRONE,lpr1_Belgorod_alarm,LiveOnlain").split(",") if c.strip()]
TTL_MIN = int(os.getenv("TTL_MIN", "40"))            # через сколько минут зона БПЛА гаснет сама
ROCKET_TTL = int(os.getenv("ROCKET_TTL", "120"))     # то же для ракетной опасности
REGION = os.getenv("REGION", "Белгородская область")
VIEWBOX = os.getenv("VIEWBOX", "34.9,51.5,38.5,49.8")
UA = "uav-map/1.0 (personal project)"
MSK = timezone(timedelta(hours=3))  # московское время
app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_fetch": 0, "posts": {}, "cold_done": set(), "status": {}}
CENTER = {"lat": 50.5997, "lon": 36.5983}
GEO = "geocache3.json"
geocache = {k: v for k, v in (json.load(open("geocache_seed.json")) if os.path.exists("geocache_seed.json") else {}).items() if v}
if os.path.exists(GEO): geocache.update(json.load(open(GEO)))
manual = json.load(open("places.json")) if os.path.exists("places.json") else {}

STOP = r"займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн|над вами|над городом|к вам|подлета\w*|находитесь|находимся|не покидаем|не покидайте|остановите|выйдите|срочно"
NOISE = (r"ракетн\w* опасност\w*|опасност\w* по бпла|тревог\w* по бпла|тревога|опасность|пролёт\w*|пролет\w*|бпла|дрон\w*|"
         r"фиксаци\w*|продолжа\w*|сохраня\w*|далее|в сторону|в направлении|внимание|отбой|от \S+|много|массов\w*|множествен\w*|ударн\w*|близлежащ\w*|окрестност\w*|реактивн\w*|небо чисто\w*|можно|продолжить|продолжайте|движени\w*|миновала|кольц\w*|развязк\w*|\bгород\b")
NEIGH = r"курск|воронеж|липецк|харьков|област"
KM = re.compile(r"\d+\W*(?:й|ый|ой)?\W*(?:километр|км)", re.I)   # «26-й километр»: точка на трассе, в поиске по карте не находится
ALIASES = [   # места, которые каналы называют по-своему: (как пишут, широта, долгота, радиус зоны в метрах)
    (re.compile(r"(?<!\d)26\W*(?:й|ый|ой)?\W*(?:километр|км)", re.I), 50.542511, 36.479119, 2000),   # М-2, подъезд к Белгороду, 26-й км
    (re.compile(r"\bбабочк\w*", re.I), 50.568621, 36.460408, 1000),                                    # развязка «Бабочка» (М-2, 674-й км)
    (re.compile(r"я\W+люблю\W+белгород", re.I), 50.526444, 36.519001, 1000),                           # кольцо «Я люблю Белгород»
]
JUNK = r"cloudtips|благодарим|донат|подписывайтесь|реклам"
BEL = re.compile(r"^белгород(а|е|у)?$|белгородск\w* район")

def fetch_posts(ch, before=None):
    url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    out = []
    for m in BeautifulSoup(r.text, "html.parser").select(".tgme_widget_message"):
        try:   # одно «кривое» сообщение не должно ломать весь канал
            t, tm = m.select_one(".tgme_widget_message_text"), m.select_one("time[datetime]")
            if not (t and tm and m.get("data-post")): continue
            for br in t.find_all("br"): br.replace_with("\n")
            out.append({"ch": ch, "id": int(m["data-post"].split("/")[-1]), "text": t.get_text().strip(),
                        "ts": datetime.fromisoformat(tm["datetime"]).timestamp()})
        except Exception as e:
            print("skip message:", ch, e)
    return out

def refresh_posts():
    for ch in CHANNELS:
        st = state["status"].setdefault(ch, {"ok": None, "err": True})
        try:
            cold = ch not in state["cold_done"]
            before = None
            for n in range(3 if cold else 1):   # при первом запуске читаем 3 страницы назад
                got = fetch_posts(ch, before)
                if not got:
                    if n == 0: raise RuntimeError("на странице нет сообщений")
                    break
                for p in got: state["posts"][(ch, p["id"])] = p
                before = min(p["id"] for p in got)
            state["cold_done"].add(ch)
            st.update(ok=time.time(), err=False)
        except Exception as e:
            st["err"] = True
            print("fetch error:", ch, e)
    cutoff = time.time() - max(TTL_MIN, ROCKET_TTL) * 60
    state["posts"] = {k: p for k, p in state["posts"].items() if p["ts"] >= cutoff}

def status_out():
    now = time.time()
    out = []
    for ch in CHANNELS:
        st = state["status"].get(ch, {})
        out.append({"ch": ch, "err": st.get("err", True), "age": round((now - st["ok"]) / 60) if st.get("ok") else None})
    return out

def classify(text):
    low = text.lower()
    if re.search(JUNK, low): return None
    if "ракетн" in low: return "rocket_clear" if "отбой" in low else "rocket"
    if "отбой" in low or re.search(r"небо чисто|можно продолжить|продолжить движение|миновала", low): return "clear"
    if "внимание" in low or re.search(r"дрон|бпла|беспилот|в сторону", low): return "alert"
    return None

def extract_places(text):
    t = re.sub(r"^\s*(внимание|отбой)[,!.:\s]*", "", text, flags=re.I)
    head = re.split(STOP, t, maxsplit=1, flags=re.I)[0]
    out = []
    for p in re.split(r"[\n,;:.!]|\bтакже\b|\bи\b", head):
        p = re.sub(r"\s+", " ", re.sub(NOISE, " ", p, flags=re.I)).strip(" \"'«»-")
        if len(p) > 2 and not re.search(NEIGH, p, re.I):
            if "районы" in text.lower() and "район" not in p.lower() and re.search(r"(ий|ый|ой)$", p):
                p += " район"   # список вида «Яковлевский, Борисовский районы»
            out.append(p)
    return out

def stems(s): return {w[:5] for w in re.findall(r"\w+", s.lower().replace("ё", "е")) if len(w) > 3}

SETTLE = {"village", "hamlet", "town", "isolated_dwelling", "locality", "farm", "allotments"}
DISTRICT = {"suburb", "quarter", "neighbourhood", "city", "administrative", "residential"}

def shape(r, q):
    lat, lon = float(r["lat"]), float(r["lon"])
    bb = [float(x) for x in r.get("boundingbox", [lat, lat, lon, lon])]  # юг, север, запад, восток
    size = max((bb[1] - bb[0]) * 111, (bb[3] - bb[2]) * 111 * math.cos(math.radians(lat)))  # км
    t, gj = r.get("type", ""), r.get("geojson") or {}
    poly = gj if gj.get("type") in ("Polygon", "MultiPolygon") else None
    if "район" in q.lower():   # район целиком: настоящий контур (бледная заливка), а если контура нет, круг
        if poly and size <= 150:
            return {"lat": lat, "lon": lon, "kind": "district", "poly": poly, "r_in": None, "r_out": 0}
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    if t in SETTLE and size <= 12:   # село/посёлок: своя территория
        if poly:
            return {"lat": lat, "lon": lon, "kind": "settlement", "poly": poly, "r_in": None, "r_out": size * 500 + 1000}
        r_in = min(3000, max(1000, size * 500))
        return {"lat": lat, "lon": lon, "kind": "settlement", "r_in": r_in, "r_out": r_in + 1000}
    if t in DISTRICT or size > 12:
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    return {"lat": lat, "lon": lon, "kind": "object", "r_in": 1500, "r_out": 2500}

def candidates(q):
    """Список вариантов места (до 5), лучший по важности первым."""
    low = q.lower()
    for k, v in manual.items():   # places.json: [широта, долгота] или [широта, долгота, радиус_в_метрах]
        if k in low:
            rad = v[2] if len(v) > 2 else 1500
            return [{"lat": v[0], "lon": v[1], "kind": "object", "r_in": rad, "r_out": rad + 1000, "imp": 1}]
    for rx, la, lo, rad in ALIASES:
        if rx.search(q): return [{"lat": la, "lon": lo, "kind": "object", "r_in": rad, "r_out": rad + 1000, "imp": 1}]
    if KM.search(q): return []   # километр трассы не ищем по карте: поиск вернул бы случайное место
    if low in geocache:
        c = geocache[low]
        return [c] if isinstance(c, dict) else (c or [])
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
            params={"q": f"{q}, {REGION}", "format": "json", "limit": 5, "viewbox": VIEWBOX, "bounded": 1,
                    "polygon_geojson": 1, "polygon_threshold": 0.003 if "район" in q.lower() else 0.0005}).json()
        time.sleep(1.1)
    except Exception:
        return []
    res = [{**shape(x, q), "imp": float(x.get("importance", 0.3)) + (0.2 if x.get("type") in ("city", "town") else 0)} for x in r]
    geocache[low] = res
    json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
    return res

def dkm(a, b):
    return math.hypot((a["lat"] - b[0]) * 111, (a["lon"] - b[1]) * 111 * math.cos(math.radians(b[0])))

def resolve(places, cands=candidates):
    """Выбирает место для каждого названия; одинаковые названия (Алексеевка) уточняем по соседним местам в сообщении."""
    cl = {pl: cands(pl) for pl in places}
    pick = {pl: max(c, key=lambda x: x["imp"]) for pl, c in cl.items() if c}
    for _ in range(2):
        for pl, c in cl.items():
            others = [p for k, p in pick.items() if k != pl]
            if len(c) > 1 and others:
                cen = (sum(p["lat"] for p in others) / len(others), sum(p["lon"] for p in others) / len(others))
                if dkm(pick[pl], cen) > 25:   # лучший по важности далеко от остальных: берём ближайший к ним
                    pick[pl] = min(c, key=lambda x: dkm(x, cen))
    return pick

def rocket_zones():
    if geocache.get("__rocket__"): return geocache["__rocket__"]
    polys = []
    for q in ("Белгород", "Белгородский район"):
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
                params={"q": f"{q}, {REGION}", "format": "json", "limit": 3, "viewbox": VIEWBOX, "bounded": 1,
                        "polygon_geojson": 1, "polygon_threshold": 0.001}).json()
            time.sleep(1.1)
        except Exception:
            continue
        for x in r:
            g = x.get("geojson") or {}
            if g.get("type") in ("Polygon", "MultiPolygon"):
                polys.append(g); break
    if polys:
        geocache["__rocket__"] = polys
        json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
        return polys
    return None   # тогда на сайте рисуется запасной круг 25 км вокруг Белгорода

def zkey(pos): return (pos["kind"], round(pos["lat"], 2), round(pos["lon"], 2))

def build(posts, now=None, geo=candidates, zones=rocket_zones):
    now = now or time.time()
    drone, unknown, oblast_drone = {}, [], None
    feed = []
    fresh_rk = lambda: {"city": False, "oblast": False, "places": {}, "last": None}
    rk = fresh_rk()
    for p in sorted(posts, key=lambda p: p["ts"]):
        kind = classify(p["text"])
        if not kind: continue
        if now - p["ts"] > (ROCKET_TTL if kind.startswith("rocket") else TTL_MIN) * 60: continue
        low = p["text"].lower()
        hhmm = datetime.fromtimestamp(p["ts"], MSK).strftime("%H:%M")
        places = extract_places(p["text"])
        fpos = None
        if kind == "rocket_clear":            # первый отбой из любого канала снимает всё
            rk = fresh_rk()
        elif kind == "rocket":
            rk["last"] = {"time": hhmm, "age": round((now - p["ts"]) / 60), "text": p["text"]}
            if not places:
                if "област" in low: rk["oblast"] = True
                else:
                    rk["city"] = True; fpos = CENTER   # без места: Белгород и Белгородский район
            pk = resolve([x for x in places if not BEL.search(x.lower())], geo)
            for pl in places:
                if BEL.search(pl.lower()): rk["city"] = True; fpos = fpos or CENTER
                else:
                    pos = pk.get(pl)
                    if pos: rk["places"][zkey(pos)] = {**pos, "place": pl}; fpos = fpos or pos
        elif kind == "clear":
            if not places or re.search(r"ранее|тревог|объявлен|вся|всей|всех", low):
                drone, unknown, oblast_drone = {}, [], None
            else:
                ss = set().union(*[stems(x) for x in places])
                drone = {k: z for k, z in drone.items() if not (stems(z["place"]) & ss)}
        elif kind == "alert":
            if re.search(r"всей территории|по всей области", low) or (not places and re.search(r"белгородск\w* област", low)):
                oblast_drone = {"time": hhmm}   # тревога по всей области: плашка и оранжевая рамка, без заливки
            pk = resolve(places, geo)
            for pl in places:
                pos = pk.get(pl)
                if not pos:
                    if KM.search(pl) and any(pk.get(x) for x in places): continue   # километр уточняет найденное место рядом
                    unknown.append({"place": pl, "time": hhmm}); continue
                fpos = fpos or pos
                z = drone.get(zkey(pos))
                if z: z["sources"].add(p["ch"]); z.update(last=p["ts"], text=p["text"], time=hhmm)
                else: drone[zkey(pos)] = {**pos, "place": pl, "sources": {p["ch"]}, "last": p["ts"], "text": p["text"], "time": hhmm}
        feed.append({"ch": p["ch"], "time": hhmm, "age": round((now - p["ts"]) / 60), "kind": kind, "text": p["text"][:220],
                     "lat": fpos["lat"] if fpos else None, "lon": fpos["lon"] if fpos else None})
    marks = [{**{k: v for k, v in z.items() if k not in ("sources", "last")},
              "age": round((now - z["last"]) / 60), "n": len(z["sources"])} for z in drone.values()]
    rocket = None
    if rk["city"] or rk["oblast"] or rk["places"]:
        rocket = {**rk["last"], "city": rk["city"], "oblast": rk["oblast"], "places": list(rk["places"].values()),
                  "zones": zones() if rk["city"] else None}
    return {"markers": marks, "unknown": unknown[:8], "rocket": rocket, "oblast_drone": oblast_drone, "feed": feed[::-1][:40], "status": status_out(),
            "updated": datetime.now(MSK).strftime("%H:%M:%S")}

@app.route("/api/markers")
def markers():
    with lock:
        if time.time() - state["last_fetch"] > 20:
            refresh_posts()
            state["last_fetch"] = time.time()
        return jsonify(build(list(state["posts"].values())))

@app.route("/manifest.webmanifest")
def manifest(): return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    r = send_from_directory("static", "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r

@app.route("/api/geocache")
def geocache_dump():   # скачать найденные места, чтобы положить в репозиторий как geocache_seed.json
    body = json.dumps({k: v for k, v in geocache.items() if v}, ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={"Content-Disposition": "attachment; filename=geocache_seed.json"})

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
