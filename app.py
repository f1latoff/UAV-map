import os, re, json, time, math, threading
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "BelgorodDRONE,lpr1_Belgorod_alarm,LiveOnlain").split(",") if c.strip()]
TTL_MIN = int(os.getenv("TTL_MIN", "40"))            # через сколько минут зона БПЛА гаснет сама
ROCKET_TTL = int(os.getenv("ROCKET_TTL", "120"))     # то же для ракетной опасности
REGION = os.getenv("REGION", "Белгородская область")
VIEWBOX = os.getenv("VIEWBOX", "34.9,51.5,38.5,49.8")
UA = "uav-map/1.0 (personal project)"
MSK = timezone(timedelta(hours=3))  # московское время
app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_fetch": 0, "posts": {}, "cold_done": set()}
GEO = "geocache2.json"
geocache = json.load(open(GEO)) if os.path.exists(GEO) else {}
manual = json.load(open("places.json")) if os.path.exists("places.json") else {}

STOP = r"займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн"
NOISE = (r"ракетн\w* опасност\w*|опасност\w* по бпла|тревог\w* по бпла|тревога|опасность|пролёт\w*|пролет\w*|бпла|дрон\w*|"
         r"фиксаци\w*|продолжа\w*|сохраня\w*|далее|в сторону|в направлении|внимание|отбой|от \S+|много|массов\w*|множествен\w*|ударн\w*")
NEIGH = r"курск|воронеж|липецк|харьков|област"
JUNK = r"cloudtips|благодарим|донат|подписывайтесь|реклам"
BEL = re.compile(r"^белгород(а|е|у)?$|белгородск\w* район")

def fetch_posts(ch, before=None):
    url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    out = []
    for m in BeautifulSoup(r.text, "html.parser").select(".tgme_widget_message"):
        t, tm = m.select_one(".tgme_widget_message_text"), m.select_one("time")
        if t and tm and m.get("data-post"):
            for br in t.find_all("br"): br.replace_with("\n")
            out.append({"ch": ch, "id": int(m["data-post"].split("/")[-1]), "text": t.get_text().strip(),
                        "ts": datetime.fromisoformat(tm["datetime"]).timestamp()})
    return out

def refresh_posts():
    for ch in CHANNELS:
        try:
            cold = ch not in state["cold_done"]
            before = None
            for _ in range(3 if cold else 1):   # при первом запуске читаем 3 страницы назад
                got = fetch_posts(ch, before)
                if not got: break
                for p in got: state["posts"][(ch, p["id"])] = p
                before = min(p["id"] for p in got)
            state["cold_done"].add(ch)
        except Exception as e:
            print("fetch error:", ch, e)
    cutoff = time.time() - max(TTL_MIN, ROCKET_TTL) * 60
    state["posts"] = {k: p for k, p in state["posts"].items() if p["ts"] >= cutoff}

def classify(text):
    low = text.lower()
    if re.search(JUNK, low): return None
    if "ракетн" in low: return "rocket_clear" if "отбой" in low else "rocket"
    if "отбой" in low: return "clear"
    if "внимание" in low or re.search(r"дрон|бпла|беспилот|в сторону", low): return "alert"
    return None

def extract_places(text):
    t = re.sub(r"^\s*(внимание|отбой)[,!.:\s]*", "", text, flags=re.I)
    head = re.split(STOP, t, maxsplit=1, flags=re.I)[0]
    out = []
    for p in re.split(r"[\n,;.!]|\bтакже\b|\bи\b", head):
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
    if "район" in q.lower():
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    if t in SETTLE and size <= 12:   # село/посёлок: своя территория
        if poly:
            return {"lat": lat, "lon": lon, "kind": "settlement", "poly": poly, "r_in": None, "r_out": size * 500 + 1000}
        r_in = min(3000, max(1000, size * 500))
        return {"lat": lat, "lon": lon, "kind": "settlement", "r_in": r_in, "r_out": r_in + 1000}
    if t in DISTRICT or size > 12:
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    return {"lat": lat, "lon": lon, "kind": "object", "r_in": 1500, "r_out": 2500}

def geocode(q):
    low = q.lower()
    for k, v in manual.items():   # places.json: [широта, долгота] или [широта, долгота, радиус_в_метрах]
        if k in low:
            rad = v[2] if len(v) > 2 else 1500
            return {"lat": v[0], "lon": v[1], "kind": "object", "r_in": rad, "r_out": rad + 1000}
    if low in geocache: return geocache[low]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
            params={"q": f"{q}, {REGION}", "format": "json", "limit": 1, "viewbox": VIEWBOX, "bounded": 1,
                    "polygon_geojson": 1, "polygon_threshold": 0.0005}).json()
        time.sleep(1.1)
    except Exception:
        return None
    res = shape(r[0], q) if r else None
    geocache[low] = res
    json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
    return res

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

def build(posts, now=None, geo=geocode, zones=rocket_zones):
    now = now or time.time()
    drone, unknown, oblast_drone = {}, [], None
    fresh_rk = lambda: {"city": False, "oblast": False, "places": {}, "last": None}
    rk = fresh_rk()
    for p in sorted(posts, key=lambda p: p["ts"]):
        kind = classify(p["text"])
        if not kind: continue
        if now - p["ts"] > (ROCKET_TTL if kind.startswith("rocket") else TTL_MIN) * 60: continue
        low = p["text"].lower()
        hhmm = datetime.fromtimestamp(p["ts"], MSK).strftime("%H:%M")
        places = extract_places(p["text"])
        if kind == "rocket_clear":            # первый отбой из любого канала снимает всё
            rk = fresh_rk()
        elif kind == "rocket":
            rk["last"] = {"time": hhmm, "age": round((now - p["ts"]) / 60), "text": p["text"]}
            if not places:
                if "област" in low: rk["oblast"] = True
                else: rk["city"] = True        # без места: Белгород и Белгородский район
            for pl in places:
                if BEL.search(pl.lower()): rk["city"] = True
                else:
                    pos = geo(pl)
                    if pos: rk["places"][zkey(pos)] = {**pos, "place": pl}
        elif kind == "clear":
            if not places or re.search(r"ранее|тревог|объявлен|вся|всей|всех", low):
                drone, unknown, oblast_drone = {}, [], None
            else:
                ss = set().union(*[stems(x) for x in places])
                drone = {k: z for k, z in drone.items() if not (stems(z["place"]) & ss)}
        elif kind == "alert":
            if re.search(r"всей территории|по всей области", low): oblast_drone = {"time": hhmm}
            for pl in places:
                pos = geo(pl)
                if not pos:
                    unknown.append({"place": pl, "time": hhmm}); continue
                z = drone.get(zkey(pos))
                if z: z["sources"].add(p["ch"]); z.update(last=p["ts"], text=p["text"], time=hhmm)
                else: drone[zkey(pos)] = {**pos, "place": pl, "sources": {p["ch"]}, "last": p["ts"], "text": p["text"], "time": hhmm}
    marks = [{**{k: v for k, v in z.items() if k not in ("sources", "last")},
              "age": round((now - z["last"]) / 60), "n": len(z["sources"])} for z in drone.values()]
    rocket = None
    if rk["city"] or rk["oblast"] or rk["places"]:
        rocket = {**rk["last"], "city": rk["city"], "oblast": rk["oblast"], "places": list(rk["places"].values()),
                  "zones": zones() if rk["city"] else None}
    return {"markers": marks, "unknown": unknown[:8], "rocket": rocket, "oblast_drone": oblast_drone,
            "updated": datetime.now(MSK).strftime("%H:%M:%S")}

@app.route("/api/markers")
def markers():
    with lock:
        if time.time() - state["last_fetch"] > 20:
            refresh_posts()
            state["last_fetch"] = time.time()
        return jsonify(build(list(state["posts"].values())))

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
