import os, re, json, time, math, threading
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

CHANNEL = os.getenv("CHANNEL", "BelgorodDRONE")
TTL_MIN = int(os.getenv("TTL_MIN", "40"))
ROCKET_TTL = int(os.getenv("ROCKET_TTL", "120"))  # минут, если не пришёл отбой ракетной опасности            # через сколько минут метка исчезает сама
REGION = os.getenv("REGION", "Белгородская область")  # добавляется к поиску места
VIEWBOX = os.getenv("VIEWBOX", "34.9,51.5,38.5,49.8") # рамка поиска: запад,север,восток,юг
UA = "uav-map/1.0 (personal project)"
MSK = timezone(timedelta(hours=3))  # московское время (Белгород)
app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_fetch": 0, "posts": {}}
GEO = "geocache2.json"
geocache = json.load(open(GEO)) if os.path.exists(GEO) else {}
manual = json.load(open("places.json")) if os.path.exists("places.json") else {}  # {"малинов": [lat, lon]}

STOP = r"займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн"

def fetch_posts(before=None):
    url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    out = []
    for m in BeautifulSoup(r.text, "html.parser").select(".tgme_widget_message"):
        t, tm = m.select_one(".tgme_widget_message_text"), m.select_one("time")
        if t and tm and m.get("data-post"):
            out.append({"id": int(m["data-post"].split("/")[-1]),
                        "text": t.get_text(" ", strip=True),
                        "ts": datetime.fromisoformat(tm["datetime"]).timestamp()})
    return out

def refresh_posts():
    if not state["posts"]:   # первый запуск: читаем 3 страницы назад
        before = None
        for _ in range(3):
            got = fetch_posts(before)
            if not got: break
            for p in got: state["posts"][p["id"]] = p
            before = min(p["id"] for p in got)
    else:
        for p in fetch_posts(): state["posts"][p["id"]] = p
    cutoff = time.time() - max(TTL_MIN, ROCKET_TTL) * 60
    state["posts"] = {i: p for i, p in state["posts"].items() if p["ts"] >= cutoff}

def classify(text):
    low = text.lower()
    if "ракетн" in low: return "rocket_clear" if "отбой" in low else "rocket"
    if "отбой" in low: return "clear"
    if "внимание" in low or re.search(r"дрон|бпла|беспилот", low): return "alert"
    return None

def extract_places(text):
    t = re.sub(r"^\s*(внимание|отбой)[,!.:\s]*", "", text, flags=re.I)
    head = re.split(STOP, t, maxsplit=1, flags=re.I)[0]
    parts = re.split(r",|\bтакже\b|\bи\b|\.|!", head)
    return [p.strip(" \"'«»") for p in parts if len(p.strip()) > 2]

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

def build(posts, now=None, geo=geocode, zones=rocket_zones):
    now = now or time.time()
    active, unknown, rocket = [], [], None
    for p in sorted(posts, key=lambda p: p["id"]):
        kind = classify(p["text"])
        ttl = ROCKET_TTL if kind and kind.startswith("rocket") else TTL_MIN
        if now - p["ts"] > ttl * 60: continue
        hhmm = datetime.fromtimestamp(p["ts"], MSK).strftime("%H:%M")
        if kind == "rocket":
            rocket = {"time": hhmm, "age": round((now - p["ts"]) / 60), "text": p["text"]}
        elif kind == "rocket_clear":
            rocket = None
        elif kind == "clear":
            rest = extract_places(p["text"])
            if not rest or re.search(r"ранее|тревог|объявлен", p["text"], re.I):
                active, unknown = [], []
            else:
                ss = set().union(*[stems(x) for x in rest])
                active = [a for a in active if not (stems(a["place"]) & ss)]
        elif kind == "alert":
            for place in extract_places(p["text"]):
                pos = geo(place)
                item = {"place": place, "time": hhmm, "text": p["text"]}
                if pos: active.append({**item, **pos, "age": round((now - p["ts"]) / 60)})
                else: unknown.append(item)
    if rocket: rocket["zones"] = zones()
    return {"markers": active, "unknown": unknown, "rocket": rocket, "updated": datetime.now(MSK).strftime("%H:%M:%S")}

@app.route("/api/markers")
def markers():
    with lock:
        if time.time() - state["last_fetch"] > 20:
            try: refresh_posts()
            except Exception as e: print("fetch error:", e)
            state["last_fetch"] = time.time()
        return jsonify(build(list(state["posts"].values())))

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
