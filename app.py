import os, re, json, time, math, threading, itertools
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_from_directory

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
GEO = "geocache4.json"
geocache = {k: v for k, v in (json.load(open("geocache_seed2.json")) if os.path.exists("geocache_seed2.json") else {}).items() if v}
if os.path.exists(GEO): geocache.update(json.load(open(GEO)))
manual = json.load(open("places.json")) if os.path.exists("places.json") else {}
outlines = json.load(open("outlines.json")) if os.path.exists("outlines.json") else {}   # контуры сёл, нарисованные вручную: {"дубовое": GeoJSON}

STOP = r"займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн|над вами|над городом|к вам|подлета\w*|находитесь|находимся|не покидаем|не покидайте|остановите|выйдите|срочно|на данный момент|активность не|не наблюда|сохраняйте|слушайте|покиньте|покидайте|\bидет\b|\bидёт\b|массов\w*"
NOISE = (r"ракетн\w* опасност\w*|опасност\w* по бпла|тревог\w* по бпла|тревога|опасность|пролёт\w*|пролет\w*|бпла|дрон\w*|"
         r"фиксаци\w*|продолжа\w*|сохраня\w*|далее|в сторону|в направлении|внимание|отбой|от \S+|много|массов\w*|множествен\w*|ударн\w*|близлежащ\w*|окрестност\w*|реактивн\w*|небо чисто\w*|можно|продолжить|продолжайте|движени\w*|миновала|кольц\w*|развязк\w*|\bгород\b|авиационн\w*|бомбов\w*|ракетно\b|трасс\w*|участок|логистическ\w*|высок\w*\s+вероятност\w*|вероятност\w*|возможе?н\w*")
NEIGH = r"курск|воронеж|липецк|харьков(?!ск\w*\s+гор)|област"
KM = re.compile(r"\d+\W*(?:й|ый|ой|го)?\W*(?:километр|км)", re.I)   # «26-й километр»: точка на трассе, в поиске по карте не находится
ALIASES = [   # места, которые каналы называют по-своему: (как пишут, широта, долгота, радиус зоны в метрах)
    (re.compile(r"(?<!\d)26\W*(?:й|ый|ой|го)?\W*(?:километр|км)", re.I), 50.542511, 36.479119, 1500),   # М-2, подъезд к Белгороду, 26-й км
    (re.compile(r"\bбабочк\w*", re.I), 50.568621, 36.460408, 500),                                    # развязка «Бабочка» (М-2, 674-й км)
    (re.compile(r"^\W*развязк\w*\W*$", re.I), 50.521067, 36.704759, 500),                              # просто «развязка» (маршрут «от развязки до …», у Разумного)
    (re.compile(r"\bаэропорт\w*", re.I), 50.6439, 36.5900, 1500),                                         # аэропорт Белгорода (примерно)
    (re.compile(r"сити\s*-?\s*молл\w*", re.I), 50.552568, 36.568031, 800),                              # ТЦ «Сити Молл» (центр)
    (re.compile(r"\bвокзал\w*", re.I), 50.5940, 36.6040, 800),                                           # железнодорожный вокзал Белгорода (примерно; «автовокзал» сюда не входит)
    (re.compile(r"я\W+люблю\W+белгород", re.I), 50.526444, 36.519001, 400),                           # кольцо «Я люблю Белгород»
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
    calm = re.search(r"не наблюда|активности нет", low)   # «На данный момент активность не наблюдаем» = спокойно
    if "ракетн" in low: return "rocket_clear" if ("отбой" in low or calm) else "rocket"
    if "отбой" in low or calm or re.search(r"небо чисто|можно продолжить|продолжить движение|миновала", low): return "clear"
    if "внимание" in low or re.search(r"дрон|бпла|беспилот|в сторону|стоп движ|остановить движение|остановите движение", low): return "alert"
    return None

def extract_places(text):
    t = re.sub(r"^\s*(внимание|отбой)[,!.:\s]*", "", text, flags=re.I)
    head = re.split(STOP, t, maxsplit=1, flags=re.I)[0]
    out = []
    for p in re.split(r"[\n,;:.!]|\bа\s+также\b|\bи\s+также\b|\bтакже\b|\bплюс\b|\bеще\b|\bещё\b|\bи\b", head, flags=re.I):
        p = re.sub(r"\s+", " ", re.sub(NOISE, " ", p, flags=re.I)).strip(" \"'«»-")
        p = re.sub(r"\s+", " ", re.sub(r"(?<!\S)(?:а|и|в|во|на|по|к|у|с|со|о|об)(?!\S)", " ", p, flags=re.I)).strip(" \"'«»-")   # одиночные предлоги и союзы («дубовое а»)
        p = re.sub(r"^(?:в\s+)?район[ае]?\s+|^(?:рядом\s+с|около|возле|вблизи|недалеко\s+от|над|на)\s+", "", p, flags=re.I).strip()
        if p.lower() in ("район", "района", "районе"): continue
        if len(p) > 2 and not re.search(NEIGH, p, re.I):
            if "районы" in text.lower() and "район" not in p.lower() and re.search(r"(ий|ый|ой)$", p):
                p += " район"   # список вида «Яковлевский, Борисовский районы»
            out.append(p)
    return out

def stems(s): return {w[:5] for w in re.findall(r"\w+", s.lower().replace("ё", "е")) if len(w) > 3}

SETTLE = {"village", "hamlet", "town", "isolated_dwelling", "locality", "farm", "allotments"}
DISTRICT = {"suburb", "quarter", "neighbourhood", "city", "administrative", "residential"}
CITYLIKE = SETTLE | {"city", "suburb", "quarter", "neighbourhood"}

def shape(r, q):
    lat, lon = float(r["lat"]), float(r["lon"])
    bb = [float(x) for x in r.get("boundingbox", [lat, lat, lon, lon])]  # юг, север, запад, восток
    size = max((bb[1] - bb[0]) * 111, (bb[3] - bb[2]) * 111 * math.cos(math.radians(lat)))  # км
    t, gj = r.get("type", ""), r.get("geojson") or {}
    poly = gj if gj.get("type") in ("Polygon", "MultiPolygon") else None
    if re.search(r"район|округ", q.lower()):   # район или округ целиком: бледная заливка по границе
        if poly and size <= 150:
            return {"lat": lat, "lon": lon, "kind": "district", "poly": poly, "r_in": None, "r_out": 0}
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    if t in SETTLE or t == "city" or (t in ("suburb", "quarter", "neighbourhood") and poly and size <= 15):
        if poly and size <= 60:   # город, посёлок, село: по своей границе
            return {"lat": lat, "lon": lon, "kind": "settlement", "poly": poly, "r_in": None, "r_out": size * 500 + 1000}
        if size <= 12:
            r_in = min(3000, max(1000, size * 500))
            return {"lat": lat, "lon": lon, "kind": "settlement", "r_in": r_in, "r_out": round(r_in * 1.5)}
    if t in DISTRICT or size > 12:
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    return {"lat": lat, "lon": lon, "kind": "object", "r_in": 800, "r_out": 1200}

def normalize(q):
    """«Губкинского ГО» -> «Губкинский городской округ», чтобы поиск по карте понимал название."""
    q = re.sub(r"\bго\b", "городской округ", q, flags=re.I)
    q = re.sub(r"городского округа", "городской округ", q, flags=re.I)
    q = re.sub(r"муниципального округа", "муниципальный округ", q, flags=re.I)
    q = re.sub(r"\bрайон(?:а|ы|ов|е|у)\b", "район", q, flags=re.I)
    q = re.sub(r"\bокруг(?:а|и|ов|е|у|ом)\b", "округ", q, flags=re.I)
    return re.sub(r"(\w+(?:ск|цк))ого\b", r"\1ий", q, flags=re.I)

def split_district(q):
    w = q.split()
    for i, x in enumerate(w):
        if 0 < i < len(w) - 1 and re.search(r"(ск|цк)(ий|ая|ое)$", x, re.I) and w[i + 1].lower() in ("городской", "район", "муниципальный", "мо", "мр", "округ"):
            return " ".join(w[:i]), " ".join(w[i:])
    return None

def district_variants(nq):
    """«Губкинский район» -> перебор: район, городской округ, муниципальный округ."""
    m = re.fullmatch(r"\s*(\S+?(?:ск|цк)(?:ий|ая|ое))\s+(?:городской\s+)?(?:муниципальный\s+)?(?:район|округ)\s*", nq, re.I)
    if not m: return None
    b = m.group(1)
    return [f"{b} район", f"{b} городской округ", f"{b} муниципальный округ", f"{b} округ"]

def nominatim(query, q_for_shape, limit=10):
    r = requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
        params={"q": f"{query}, {REGION}", "format": "json", "limit": limit, "viewbox": VIEWBOX, "bounded": 1,
                "polygon_geojson": 1, "polygon_threshold": 0.003 if re.search(r"район|округ", q_for_shape.lower()) else 0.001}).json()
    time.sleep(1.1)
    name = lambda x: x.get("display_name", "").split(",")[0].strip().lower()
    polys = {name(x): x["geojson"] for x in r if (x.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon")}
    out = []
    for x in r:
        if (x.get("geojson") or {}).get("type") not in ("Polygon", "MultiPolygon") and x.get("type") in CITYLIKE and name(x) in polys:
            x = {**x, "geojson": polys[name(x)]}   # у города/села граница лежит в соседнем результате с тем же названием
        out.append({**shape(x, q_for_shape), "imp": float(x.get("importance", 0.3)) + (0.2 if x.get("type") in ("city", "town") else 0)})
    return out

CITY = (50.5958, 36.5873)   # центр Белгорода (Соборная площадь)

def city_part(q):
    """«центр города», «север города», «в юго-западной части Белгорода»: часть Белгорода как круг вокруг точки, смещённой от центра."""
    low = q.lower().replace("ё", "е")
    if not re.search(r"(?:города|белгород\w*)", low) or re.search(r"белгородск\w*\s+(?:район|област)", low): return None
    if re.search(r"центр\w*", low):
        lat, lon, rad = CITY[0], CITY[1], 1000
    else:
        ns = 1 if "север" in low else -1 if re.search(r"\bюг|южн|юго", low) else 0
        ew = 1 if re.search(r"восток|восточ", low) else -1 if re.search(r"запад", low) else 0
        if not ns and not ew: return None
        k = 0.707 if ns and ew else 1            # по диагонали смещение по каждой оси меньше
        lat = CITY[0] + ns * k * 3.5 / 111
        lon = CITY[1] + ew * k * 3.5 / (111 * math.cos(math.radians(CITY[0])))
        rad = 2000
    return {"lat": lat, "lon": lon, "kind": "object", "r_in": rad, "r_out": round(rad * 1.5), "imp": 1}

def candidates(q):
    """Список вариантов места (до 5), лучший по важности первым."""
    low = q.lower()
    for k, v in manual.items():   # places.json: [широта, долгота] или [широта, долгота, радиус_в_метрах]
        if k in low:
            rad = v[2] if len(v) > 2 else 800
            return [{"lat": v[0], "lon": v[1], "kind": "object", "r_in": rad, "r_out": round(rad * 1.5), "imp": 1}]
    cp = city_part(q)
    if cp: return [cp]
    for rx, la, lo, rad in ALIASES:
        if rx.search(q): return [{"lat": la, "lon": lo, "kind": "object", "r_in": rad, "r_out": round(rad * 1.5), "imp": 1}]
    if KM.search(q): return []   # километр трассы не ищем по карте: поиск вернул бы случайное место
    if low in geocache:
        c = geocache[low]
        return [c] if isinstance(c, dict) else (c or [])
    try:
        nq = normalize(q)
        sd = split_district(nq)
        res = []
        if sd:   # «Архангельское Губкинского ГО»: сначала ищем само село, из одинаковых берём ближайшее к названному округу
            res = nominatim(sd[0], sd[0])
            hint = nominatim(sd[1], sd[1], 1)
            if res and hint:
                res = [min(res, key=lambda x: dkm(x, (hint[0]["lat"], hint[0]["lon"])))]
        if not res:
            for v in (district_variants(nq) or []):   # «Губкинский район» на карте это городской округ
                res = nominatim(v, v)
                if res: break
        if not res:
            res = nominatim(nq, q)
    except Exception:
        return []
    geocache[low] = res
    json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
    return res

def dkm(a, b):
    return math.hypot((a["lat"] - b[0]) * 111, (a["lon"] - b[1]) * 111 * math.cos(math.radians(b[0])))

def only_settlements(c):
    """Если среди вариантов есть настоящее село, улицы и прочее с таким же названием отбрасываем (кроме заданных нами вручную)."""
    if any(x["kind"] == "settlement" for x in c) and not any(x.get("imp") == 1 for x in c):
        return [x for x in c if x["kind"] == "settlement"]
    return c

def merge_forms(pl, geo):
    """Варианты места по всем формам названия («Никольского» и «Никольское»), без повторов."""
    seen, out = set(), []
    for f in name_forms(pl):
        for c in geo(f):
            k = (round(c["lat"], 3), round(c["lon"], 3))
            if k not in seen: seen.add(k); out.append(c)
    return out

def resolve(places, cands=candidates):
    """Выбирает место для каждого названия. Из одинаковых сёл чуть предпочитается то, что ближе к Белгороду. Если названий несколько («Никольское» и «Дубовое»), а одинаковых сёл в области много,
    берётся набор, где места ближе всего друг к другу (то есть то Никольское, что рядом с Дубовым)."""
    cl = {pl: only_settlements(cands(pl)) for pl in places}
    cl = {pl: sorted(c, key=lambda x: -x["imp"])[:10] for pl, c in cl.items() if c}
    pick = {pl: c[0] for pl, c in cl.items()}
    amb = [pl for pl, c in cl.items() if len(c) > 1][:4]
    if amb:
        fixed = [pick[pl] for pl in cl if pl not in amb]
        best = None
        for combo in itertools.product(*[cl[pl] for pl in amb]):
            pts = fixed + list(combo)
            cost = (sum(dkm(a, (b["lat"], b["lon"])) for k, a in enumerate(pts) for b in pts[k + 1:]) - 2 * sum(x["imp"] for x in combo)
                    + 0.03 * sum(dkm(x, (CENTER["lat"], CENTER["lon"])) for x in combo))   # чем ближе к Белгороду, тем чуть лучше
            if best is None or cost < best[0]: best = (cost, combo)
        pick.update(dict(zip(amb, best[1])))
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

OZ_FAIL = {"t": 0}

def oblast_zone():
    """Граница всей области (сильно упрощённая) для лёгкой заливки при тревоге на всю область. Берётся один раз и запоминается."""
    if geocache.get("__oblast__"): return geocache["__oblast__"]
    if time.time() - OZ_FAIL["t"] < 300: return None   # после неудачи не долбим сервис, пробуем снова через 5 минут
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
            params={"q": REGION, "format": "json", "limit": 3, "countrycodes": "ru", "featuretype": "state",
                    "polygon_geojson": 1, "polygon_threshold": 0.01}).json()
        time.sleep(1.1)
        for x in r:
            g = x.get("geojson") or {}
            if g.get("type") in ("Polygon", "MultiPolygon"):
                geocache["__oblast__"] = g
                json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
                return g
    except Exception as e:
        print("oblast zone error:", e)
    OZ_FAIL["t"] = time.time()
    return None

def fix_names(t):
    t = re.sub(r"\b([Уу])син(к\w*)", r"\1стин\2", t)        # опечатка «Усинки» -> «Устинки»
    t = re.sub(r"\b[Хх]ар\s?г[ао]р\w*", "Харьковская гора", t)       # «харгора», «хар гора», «Харгара» = Харьковская гора
    t = re.sub(r"\b[Дд]убовск\w*\s+пол\w*", "Парк Дуба", t)   # «Дубовское поле» (в любом падеже) = Парк Дуба
    return re.sub(r"\b([Сс]евр)я(ков\w*)", r"\1ю\2", t)    # «Севрякова» -> «Севрюкова»

RT_STOP = r"всем|никому|остановить|остановите|стоп|можно|не пользоваться|находим\w*|находит\w*|займите|над|подлет\w*|летит|уничтожен\w*|опасн\w*|движени\w*|покиньте|покидайте|идет|идёт|массов\w*|пролет\w*|пролёт\w*|уходите|укрыти\w*|выйдите|прошу|внимание|ударн\w*|бпла|дрон\w*|угроз\w*"
_END = r"(?=\s*,?\s*(?:также|" + RT_STOP + r")\b|[.!:;\n]|$)"
ROUTE = re.compile(r"\bот\s+(?P<a>[^,.!:;\n]+?)\s+(?:до|в\s+сторону|к)\s+(?P<b>[^.!:;\n]+?)" + _END, re.I)
ROUTE2 = re.compile(r"\b(?:автодорог\w*|трасс\w*|дорог\w*|участок)\s+(?P<n>[^.!:;\n]+?)" + _END, re.I)   # «трасса Никольское Таврово» без «от … до»
CUT = lambda x: x.strip(" \"'«»-—–")

def split_route_names(n):
    parts = [CUT(x) for x in re.split(r"\s*[—–-]\s*|,|\bи\b|\bдо\b", n) if CUT(x)]
    return parts if len(parts) >= 2 and all(len(x) >= 4 and not re.search(r"\d", x) for x in parts) else None

def extract_routes(text):
    """«от X до Y, Z» и «трасса X Y» -> (участки [(X, [Y, Z])], текст без этих фраз, фразы из 2-4 слов без разделителей)."""
    routes, loose = [], []
    def take(m):
        bs = [CUT(x) for x in re.split(r",|\bи\b", m.group("b")) if CUT(x)]
        if CUT(m.group("a")) and bs: routes.append((CUT(m.group("a")), bs))
        return " "
    rest = ROUTE.sub(take, text)
    def take2(m):
        n = m.group("n")
        if re.search(r"[—–-]|,|\bи\b|\bдо\b", n):
            parts = split_route_names(n)
            if parts: routes.append((parts[0], parts[1:4])); return " "
        else:
            w = n.split()
            if 2 <= len(w) <= 4 and all(len(x) >= 4 and not re.search(r"\d", x) for x in w):
                loose.append(" ".join(w)); return " "
        return m.group(0)
    return routes, ROUTE2.sub(take2, rest), loose

SUF = [("ского", "ское"), ("ого", "ое"), ("ой", "а"), ("ова", "ово"), ("ева", "ево"), ("ина", "ино"), ("ки", "ка"), ("ни", "нь"), ("ги", "га"), ("ы", "а"), ("а", "")]

def name_forms(ph):
    """«Никольского» -> «Никольское», «Масловой Пристани» -> «Маслова Пристань»: сначала как написано, потом в начальной форме."""
    ph = ph.strip(); out = [ph]; full = []
    for w in ph.split():
        for a, b in SUF:
            if w.lower().endswith(a) and len(w) > len(a) + 2:
                full.append(w[:-len(a)] + b); break
        else: full.append(w)
    f = " ".join(full)
    if f != ph: out.append(f)
    return out

def route_len(pts):
    return sum(math.hypot((a[0] - b[0]) * 111, (a[1] - b[1]) * 111 * math.cos(math.radians(a[0]))) for a, b in zip(pts, pts[1:]))

STRONG = re.compile(r"тревог|летит|летят|подлета|движется|движутся|над\s+вам|над\s+город|ударн|в\s+ваш\w*\s+(?:сторону|направлении)|займите|fpv|уничтожен", re.I)
DANGER = re.compile(r"опасност\w*\s+по\s+(?:бпла|дрон\w*|беспилот\w*)|угроз\w*\s+(?:бпла|дрон)|возможе?н\w*|вероятност", re.I)

def level_of(text):
    """alarm = БПЛА зафиксирован («Внимание» тоже сюда), danger = только опасность (возможен пролёт, высокая вероятность)."""
    if STRONG.search(text): return "alarm"
    return "danger" if DANGER.search(text) else "alarm"

OV = {"t": 0, "err": ""}
OV_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]

def hull(pts):
    """Выпуклая оболочка точек (для застройки, заданной составным контуром)."""
    pts = sorted(set(map(tuple, pts)))
    if len(pts) < 3: return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0: up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]

def norm_geo(g):
    if g.get("type") == "FeatureCollection": g = g["features"][0]
    if g.get("type") == "Feature": g = g["geometry"]
    return g

def overpass_outline(lat, lon):
    """Контур застройки села (landuse=residential) из OpenStreetMap через Overpass. Возвращает (контур или None, причина, если контура нет)."""
    if time.time() - OV["t"] < 600: return None, "пауза после сбоя Overpass: " + OV["err"]
    q = f'[out:json][timeout:10];(way(around:1000,{lat},{lon})["landuse"="residential"];relation(around:1000,{lat},{lon})["landuse"="residential"];);out geom;'
    els, errs = None, []
    for u in OV_URLS:   # если один сервер не ответил, пробуем следующий
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=14, params={"data": q})
            r.raise_for_status()
            els = r.json().get("elements", []); break
        except Exception as e:
            errs.append(f"{u.split('/')[2]}: {type(e).__name__} {str(e)[:60]}")
    if els is None:
        OV["t"] = time.time(); OV["err"] = "; ".join(errs)[:300]; print("overpass error:", OV["err"])
        return None, "Overpass не ответил (" + OV["err"] + ")"
    polys = []
    for w in els:
        if w.get("type") == "relation":   # составной контур: берём выпуклую оболочку внешних линий
            ring = [list(p) for p in hull([[p["lon"], p["lat"]] for m in w.get("members", []) if m.get("role") == "outer" for p in m.get("geometry", [])])]
            if len(ring) < 3: continue
            ring.append(ring[0])
        else:
            ring = [[p["lon"], p["lat"]] for p in w.get("geometry", [])]
            if len(ring) < 4: continue
            if ring[0] != ring[-1]: ring.append(ring[0])
        if len(ring) > 41:
            step = len(ring) // 40 + 1
            ring = ring[::step] + [ring[0]]
        cx, cy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if math.hypot((cy - lat) * 111, (cx - lon) * 111 * math.cos(math.radians(lat))) <= 1.5:   # только застройка рядом с центром села
            polys.append([ring])
    if not polys: return None, "в карте нет нарисованной застройки рядом с селом (Overpass ответил, но пусто)"
    return {"type": "MultiPolygon", "coordinates": polys[:25]}, ""

def as_poly(pos, g):
    rings = [g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]]
    xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
    size = max((max(ys) - min(ys)) * 111, (max(xs) - min(xs)) * 111 * math.cos(math.radians(pos["lat"])))
    return {**pos, "poly": g, "r_in": None, "r_out": size * 500 + 700}

def with_outline(pos, name=""):
    """Село рисуем по контуру: сначала нарисованный вручную (outlines.json), потом застройка из OpenStreetMap; иначе остаётся круг (с пометкой, почему)."""
    if pos.get("kind") != "settlement" or pos.get("poly") or pos.get("imp") == 1: return pos
    low = name.lower()
    for k, g in outlines.items():
        if k in low: return as_poly(pos, norm_geo(g))
    key = f"__ov_{pos['lat']:.3f}_{pos['lon']:.3f}"
    why = ""
    if key not in geocache:
        g, why = overpass_outline(pos["lat"], pos["lon"])
        if g is None and not why.startswith("в карте нет"): return {**pos, "why": why}   # сбой: «нет контура» не запоминаем
        geocache[key] = g or ""
        json.dump(geocache, open(GEO, "w"), ensure_ascii=False)
    g = geocache.get(key)
    if not g: return {**pos, "why": why or "в карте нет нарисованной застройки рядом с селом (запомнено)"}
    return as_poly(pos, g)

def zkey(pos): return (pos["kind"], round(pos["lat"], 2), round(pos["lon"], 2))

def build(posts, now=None, geo=candidates, zones=rocket_zones, ozone=oblast_zone):
    now = now or time.time()
    drone, unknown, oblast_drone = {}, [], None
    feed = []
    fresh_rk = lambda: {"city": False, "oblast": False, "places": {}, "last": None}
    rk = fresh_rk()
    def put(pos, name, p, hhmm, lvl):
        k = zkey(pos); z = drone.get(k)
        if z:
            z["sources"].add(p["ch"]); z.update(last=p["ts"], text=p["text"], time=hhmm)
            if lvl == "alarm": z["level"] = "alarm"   # опасность усиливается до тревоги, обратно не понижается
        else:
            drone[k] = {**pos, "place": name, "sources": {p["ch"]}, "last": p["ts"], "text": p["text"], "time": hhmm,
                        "level": lvl, "pt": pos.get("kind") == "object"}
    for p in sorted(posts, key=lambda p: p["ts"]):
        txt = fix_names(p["text"])
        kind = classify(txt)
        if not kind: continue
        if now - p["ts"] > (ROCKET_TTL if kind.startswith("rocket") else TTL_MIN) * 60: continue
        low = p["text"].lower()
        hhmm = datetime.fromtimestamp(p["ts"], MSK).strftime("%H:%M")
        routes, rest, loose = extract_routes(txt) if kind in ("alert", "clear") else ([], txt, [])
        places = extract_places(rest)
        fpos = None
        if kind == "rocket_clear":            # первый отбой из любого канала снимает всё
            rk = fresh_rk()
        elif kind == "rocket":
            rk["last"] = {"time": hhmm, "age": round((now - p["ts"]) / 60), "text": p["text"]}
            if not places:
                if re.search(r"курск|воронеж|липецк|харьков(?!ск\w*\s+гор)", low) and "белгород" not in low: pass   # про соседний регион
                elif "област" in low: rk["oblast"] = True
                else:
                    rk["city"] = True; fpos = CENTER   # без места: Белгород и Белгородский район
            pk = resolve([x for x in places if not BEL.search(x.lower())], geo)
            for pl in places:
                if BEL.search(pl.lower()): rk["city"] = True; fpos = fpos or CENTER
                else:
                    pos = pk.get(pl)
                    if pos: rk["places"][zkey(pos)] = {**pos, "place": pl}; fpos = fpos or pos
        elif kind == "clear":
            cl = places + [e for a, bs in routes for e in [a] + bs] + [w for phr in loose for w in phr.split()]
            if not cl or re.search(r"ранее|тревог|объявлен|вся|всей|всех", low):
                drone, unknown, oblast_drone = {}, [], None
            else:
                ss = set().union(*[stems(x) for x in cl])
                drone = {k: z for k, z in drone.items() if not (stems(z["place"]) & ss)}
        elif kind == "alert":
            lvl = level_of(txt)
            if re.search(r"всей территории|по всей области", low) or (not places and re.search(r"белгородск\w* област", low)):
                oblast_drone = {"time": hhmm, "level": lvl}   # тревога по всей области: плашка и оранжевая рамка, без заливки
            rc = lambda pl: merge_forms(pl, geo)
            for phr in loose:   # «трасса Никольское Таврово»: два названия подряд = участок; а «Новая Деревня» = одно название
                if merge_forms(phr, geo): places.append(phr)
                else: routes.append((phr.split()[0], phr.split()[1:4]))
            for a, bs in routes:   # «от X до Y»: полоса вдоль трассы (прямая линия между селами, через все названные)
                names = [a] + bs; label = " — ".join(names)
                pk0 = resolve(names, rc)
                pts = [[pk0[n]["lat"], pk0[n]["lon"]] for n in names if n in pk0]
                if len(pts) >= 2 and route_len(pts) <= 50:
                    w = 3000 if route_len(pts) <= 20 else 6000
                    zr = {"kind": "route", "pts": pts, "w_in": w, "w_out": w + 2000, "r_in": None, "r_out": 0, "place": label,
                          "lat": sum(x[0] for x in pts) / len(pts), "lon": sum(x[1] for x in pts) / len(pts)}
                    put(zr, label, p, hhmm, lvl); fpos = fpos or zr
                elif len(pts) == 1:
                    n1 = next(n for n in names if n in pk0); put(pk0[n1], n1, p, hhmm, lvl); fpos = fpos or pk0[n1]
                else:
                    unknown.append({"place": label + (" (участок слишком длинный, вероятно выбрано не то село)" if len(pts) >= 2 else ""), "time": hhmm})
                if pts:
                    for n in names:
                        if n not in pk0: unknown.append({"place": n, "time": hhmm})   # какое-то из названий не нашлось
            pk = {k: with_outline(v, k) for k, v in resolve(places, rc).items()}
            for pl in places:
                pos = pk.get(pl)
                if not pos:
                    if KM.search(pl) and any(pk.get(x) for x in places): continue   # километр уточняет найденное место рядом
                    unknown.append({"place": pl, "time": hhmm}); continue
                fpos = fpos or pos
                put(pos, pl, p, hhmm, lvl)
        feed.append({"ch": p["ch"], "time": hhmm, "age": round((now - p["ts"]) / 60), "kind": kind, "level": level_of(txt) if kind == "alert" else None, "text": p["text"][:220],
                     "lat": fpos["lat"] if fpos else None, "lon": fpos["lon"] if fpos else None})
    marks = [{**{k: v for k, v in z.items() if k not in ("sources", "last")},
              "age": round((now - z["last"]) / 60), "n": len(z["sources"])} for z in drone.values()]
    rocket = None
    if rk["city"] or rk["oblast"] or rk["places"]:
        rocket = {**rk["last"], "city": rk["city"], "oblast": rk["oblast"], "places": list(rk["places"].values()),
                  "zones": zones() if rk["city"] else None}
    return {"markers": marks, "unknown": unknown[:8], "rocket": rocket, "oblast_drone": oblast_drone, "oblast_poly": ozone() if (rk["oblast"] or oblast_drone) else None, "feed": feed[::-1][:40], "status": status_out(),
            "updated": datetime.now(MSK).strftime("%H:%M:%S")}

WX = {"t": 0, "ok": 0, "data": None, "err": None}
WMO = {0: ("☀️", "Ясно"), 1: ("🌤", "Малооблачно"), 2: ("⛅", "Облачно"), 3: ("☁️", "Пасмурно"), 45: ("🌫", "Туман"), 48: ("🌫", "Туман"),
       51: ("🌦", "Морось"), 53: ("🌦", "Морось"), 55: ("🌦", "Морось"), 56: ("🌧", "Ледяная морось"), 57: ("🌧", "Ледяная морось"),
       61: ("🌧", "Дождь"), 63: ("🌧", "Дождь"), 65: ("🌧", "Сильный дождь"), 66: ("🌧", "Ледяной дождь"), 67: ("🌧", "Ледяной дождь"),
       71: ("🌨", "Снег"), 73: ("🌨", "Снег"), 75: ("🌨", "Сильный снег"), 77: ("🌨", "Снежная крупа"),
       80: ("🌦", "Ливень"), 81: ("🌦", "Ливень"), 82: ("🌧", "Сильный ливень"), 85: ("🌨", "Снегопад"), 86: ("🌨", "Снегопад"),
       95: ("⛈", "Гроза"), 96: ("⛈", "Гроза с градом"), 99: ("⛈", "Гроза с градом")}

def weather():
    """Погода в Белгороде (Open-Meteo, без ключа); обновляется раз в 15 минут, при ошибке пробуем снова через минуту."""
    now = time.time()
    if now - WX["t"] > 900:
        WX["t"] = now - 840
        try:
            resp = requests.get("https://api.open-meteo.com/v1/forecast", headers={"User-Agent": UA}, timeout=6,
                params={"latitude": CENTER["lat"], "longitude": CENTER["lon"], "current": "temperature_2m,weather_code,is_day",
                        "timezone": "Europe/Moscow"})
            resp.raise_for_status()
            c = resp.json()["current"]
            icon, label = WMO.get(int(c["weather_code"]), ("🌡", "Погода"))
            if not c.get("is_day", 1) and icon in ("☀️", "🌤"): icon = "🌙"
            WX.update(t=now, ok=now, err=None, data={"temp": round(c["temperature_2m"]), "icon": icon, "label": label})
        except Exception as e:
            WX["err"] = f"{type(e).__name__}: {e}"[:200]
            print("weather error:", WX["err"])
    return WX["data"] if WX["data"] and now - WX["ok"] < 3 * 3600 else None   # старше 3 часов не показываем

@app.route("/api/markers")
def markers():
    with lock:
        if time.time() - state["last_fetch"] > 20:
            refresh_posts()
            state["last_fetch"] = time.time()
        out = build(list(state["posts"].values()))
    out["weather"] = weather()
    if out["weather"] is None: out["weather_err"] = WX["err"]   # причина, если погоды нет (видно в /api/markers)
    return jsonify(out)

@app.route("/manifest.webmanifest")
def manifest(): return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    r = send_from_directory("static", "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r

TEST_KEY = os.getenv("TEST_KEY", "")   # пароль режима проверки; если не задан, режим выключен

@app.route("/api/test", methods=["POST"])
def api_test():
    """Проверка разбора: текст сообщений (несколько через строку ---) рисуется на карте, данные сайта не меняются."""
    if not TEST_KEY or request.args.get("key") != TEST_KEY: return ("", 404)
    texts = [t.strip() for t in re.split(r"\n\s*---+\s*\n", (request.get_json(silent=True) or {}).get("text", "")) if t.strip()][:10]
    now = time.time()
    posts = [{"ch": "test", "id": i, "text": t, "ts": now - (len(texts) - i) * 20} for i, t in enumerate(texts)]
    with lock: out = build(posts)
    out["weather"] = None; out["status"] = []
    return jsonify(out)

@app.route("/api/geocache")
def geocache_dump():   # скачать найденные места, чтобы положить в репозиторий как geocache_seed.json
    body = json.dumps({k: v for k, v in geocache.items() if v}, ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={"Content-Disposition": "attachment; filename=geocache_seed2.json"})

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
