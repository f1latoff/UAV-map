import os, re, json, time, math, threading
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

CHANNEL = os.getenv("CHANNEL", "BelgorodDRONE")
TTL_MIN = int(os.getenv("TTL_MIN", "40"))            # через сколько минут метка исчезает сама
REGION = os.getenv("REGION", "Белгородская область")  # добавляется к поиску места
VIEWBOX = os.getenv("VIEWBOX", "34.9,51.5,38.5,49.8") # рамка поиска: запад,север,восток,юг
UA = "uav-map/1.0 (personal project)"
app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_fetch": 0, "posts": []}
GEO = "geocache2.json"
geocache = json.load(open(GEO)) if os.path.exists(GEO) else {}
manual = json.load(open("places.json")) if os.path.exists("places.json") else {}  # {"малинов": [lat, lon]}

STOP = r"займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн"

def fetch_posts():
    r = requests.get(f"https://t.me/s/{CHANNEL}", headers={"User-Agent": UA}, timeout=15)
    out = []
    for m in BeautifulSoup(r.text, "html.parser").select(".tgme_widget_message"):
        t, tm = m.select_one(".tgme_widget_message_text"), m.select_one("time")
        if t and tm and m.get("data-post"):
            out.append({"id": int(m["data-post"].split("/")[-1]),
                        "text": t.get_text(" ", strip=True),
                        "ts": datetime.fromisoformat(tm["datetime"]).timestamp()})
    return out

def classify(text):
    low = text.lower()
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

def build(posts, now=None, geo=geocode):
    now = now or time.time()
    active, unknown = [], []
    for p in sorted(posts, key=lambda p: p["id"]):
        if now - p["ts"] > TTL_MIN * 60: continue
        kind = classify(p["text"])
        if kind == "clear":
            rest = extract_places(p["text"])
            if not rest or re.search(r"ранее|тревог|объявлен", p["text"], re.I):
                active, unknown = [], []
            else:
                ss = set().union(*[stems(x) for x in rest])
                active = [a for a in active if not (stems(a["place"]) & ss)]
        elif kind == "alert":
            for place in extract_places(p["text"]):
                pos = geo(place)
                item = {"place": place, "time": datetime.fromtimestamp(p["ts"]).strftime("%H:%M"), "text": p["text"]}
                if pos: active.append({**item, **pos, "age": round((now - p["ts"]) / 60)})
                else: unknown.append(item)
    return {"markers": active, "unknown": unknown, "updated": datetime.now().strftime("%H:%M:%S")}

@app.route("/api/markers")
def markers():
    with lock:
        if time.time() - state["last_fetch"] > 20:
            try: state["posts"] = fetch_posts()
            except Exception as e: print("fetch error:", e)
            state["last_fetch"] = time.time()
        return jsonify(build(state["posts"]))

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
