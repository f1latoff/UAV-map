import os, re, json, time, math, threading, itertools
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_from_directory

CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "BelgorodDRONE,lpr1_Belgorod_alarm,LiveOnlain").split(",") if c.strip()]
TTL_MIN = int(os.getenv("TTL_MIN", "40"))            # через сколько минут зона БПЛА гаснет сама
ROCKET_TTL = int(os.getenv("ROCKET_TTL", "120"))     # то же для ракетной опасности
REGION = os.getenv("REGION", "Белгородская область")
VIEWBOX = os.getenv("VIEWBOX", "34.9,51.6,39.6,49.8")   # вся область (Алексеевка и Ровеньки лежат восточнее 38,5)
LOCAL_VB = os.getenv("LOCAL_VIEWBOX", "36.0,51.0,37.2,50.2")   # окрестности Белгорода (около 45 км): одноимённые сёла ищем сначала здесь
UA = "uav-map/1.0 (personal project)"
MSK = timezone(timedelta(hours=3))  # московское время
app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_fetch": 0, "posts": {}, "cold_done": set(), "status": {}}
CENTER = {"lat": 50.5997, "lon": 36.5983}
def load_json(path, default):
    """Читает файл JSON; если файла нет или он повреждён, сайт не падает, а работает без него."""
    try:
        return json.load(open(path)) if os.path.exists(path) else default
    except Exception as e:
        print("не удалось прочитать", path, e)
        return default

def save_cache():
    """Записывает память целиком через временный файл: обрыв записи не портит старый файл."""
    try:
        tmp = GEO + ".tmp"
        with open(tmp, "w") as f: json.dump(dict(geocache), f, ensure_ascii=False)
        os.replace(tmp, GEO)
    except Exception as e:
        print("не удалось сохранить память:", e)

GEO = "geocache5.json"
geocache = {k: v for k, v in load_json("geocache_seed3.json", {}).items() if v}
geocache.update(load_json(GEO, {}))
BUILTIN_PLACES = {   # закреплённые вручную названия: часть слова: [широта, долгота, радиус в метрах, "село" (если это село, а не объект), название для поиска границы]
    "титовк": [50.387554, 36.844877, 800, "село", "Титовка"],   # посёлок Титовка у Шебекино
    "устинк": [50.428216, 36.881933, 800, "село", "Устинка"],   # квартал Устинка (Шебекино)
    "политотдельск": [50.473768, 36.409698, 1000, "село", "Политотдельский"],   # посёлок Политотдельский (Белгородский округ)
    "политодельск": [50.473768, 36.409698, 1000, "село", "Политотдельский"],    # то же с опечаткой в канале («Политодельский»)
    "заячий": [50.4114, 36.869707, 800, "село", "Заячий хутор"],     # квартал Заячий хутор (Шебекино)
    "заячь": [50.4114, 36.869707, 800, "село", "Заячий хутор"],      # то же в других падежах: «Заячьем хуторе», «Заячьего хутора»
}
def save_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f: json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print("не удалось сохранить", path, e)

JFILE = "journal.json"
JOURNAL = {k: v for k, v in load_json(JFILE, {}).items() if isinstance(v, dict)}   # журнал сообщений, которые сайт не понял (ключ «канал:номер»)
SUSPECT = re.compile(r"бпла|дрон|беспилот|тревог|опасност|ракет|ударн|атак|взрыв|обстрел|пр[оа]л[её]т", re.I)

def journal_add(issues):
    """Записывает в журнал новые «непонятые» сообщения (повторно одно и то же не пишется), старше 7 дней стираются."""
    changed = False
    for it in issues:
        old = JOURNAL.get(it["key"])
        if old and old.get("reason") == it["reason"]: continue
        JOURNAL[it["key"]] = {**it, "seen": time.time()}; changed = True
    for k in [k for k, v in JOURNAL.items() if v.get("ts", 0) < time.time() - 7 * 86400]: del JOURNAL[k]; changed = True
    while len(JOURNAL) > 400:
        del JOURNAL[min(JOURNAL, key=lambda k: JOURNAL[k].get("ts", 0))]; changed = True
    if changed: save_json(JFILE, JOURNAL)

manual = {**BUILTIN_PLACES, **load_json("places.json", {})}
outlines = load_json("outlines.json", {})   # контуры сёл, нарисованные вручную: {"дубовое": GeoJSON}

STOP = r"заметьте|со\s+стороны|никому\s+не|не\s+пользоваться|оставайтесь|не подходите|пройдите|займите|займи|всем|в укрыти|укрыти|уйдите|в ваш|стоп|летит|летят|движется|угроза|ударн|над вами|над городом|к вам|подлета\w*|находитесь|находимся|не покидаем|не покидайте|остановите|выйдите|срочно|на данный момент|активность не|не наблюда|сохраняйте|слушайте|покиньте|покидайте|\bидет\b|\bидёт\b|массов\w*"
NOISE = (r"ракетн\w* опасност\w*|опасност\w* по бпла|тревог\w* по бпла|тревога|опасность|пролёт\w*|пролет\w*|бпла|дрон\w*|"
         r"фиксаци\w*|продолжа\w*|сохраня\w*|далее|в сторону|в направлении|внимание|отбой|от \S+|много|массов\w*|множествен\w*|ударн\w*|близлежащ\w*|окрестност\w*|реактивн\w*|небо чисто\w*|можно|продолжить|продолжайте|движени\w*|миновала|кольц\w*|развязк\w*|\bгород\b|авиационн\w*|бомбов\w*|ракетно\b|трасс\w*|участок|логистическ\w*|высок\w*\s+вероятност\w*|вероятност\w*|возможе?н\w*|атак\w*|налет\w*|налёт\w*|ранее|объявленн\w*|тревог\w*|fpv|оптоволокн\w*|активност\w*|высок\w*|ближайш\w*|врем\w*|даем|даём|\bотбо\w*|\bбудет\b|\bбудут\b|\bне\b|предварительн\w*|вышеперечисленн\w*|\bрегион\w*|\bмер\w*|предосторожност\w*|пуск\w*|самол[её]т\w*|противника|ракет\w*|\bспутник\w*|\bтыл\w*|\bдом(?:а|ов|ам|е)?\b|\bбез\b|\bтом\b|\bчисл\w*|\bмного\b|\bднр\b|\bлнр\b|\bгг\b|\bвсу\b|продолжа\w*|фиксаци\w*|множествен\w*|сохраня\w*|\bфедеральн\w+|на\s+всей\s+территории\s+област\w*|\bсторону\b|\bособ\w+|\bможет\b|вылет\w*|\bнеб(?:о|у|а|е|ом)\b|\bнет\b|опасност\w*|\bдвс\b|\bфпв\b|никому|пользоваться|заметьте|белгородск\w+\s+област\w+|соблюда\w*|бдительн\w*|осторожн\w*|сохраняйте|спокойстви\w*|\bкпп\b|контрольно-пропускн\w*\s+пункт\w*")
NEIGH = r"курск|воронеж|липецк|брянск|сумск|херсон|запорож|\bорёл\b|\bорел\b|харьков(?!ск\w*\s+гор)|област"
KM = re.compile(r"\d+\W*(?:й|ый|ой|го)?\W*(?:километр|км)", re.I)   # «26-й километр»: точка на трассе, в поиске по карте не находится
ALIASES = [   # места, которые каналы называют по-своему: (как пишут, широта, долгота, радиус зоны в метрах)
    (re.compile(r"(?<!\d)26\W*(?:й|ый|ой|го)?\W*(?:километр|км)", re.I), 50.425194, 36.835547, 800),   # «26-й километр» = заправка у кольца (Шебекинский округ)
    (re.compile(r"\bбабочк\w*", re.I), 50.568621, 36.460408, 500),                                    # развязка «Бабочка» (М-2, 674-й км)
    (re.compile(r"^\W*развязк\w*\W*$", re.I), 50.521067, 36.704759, 500),                              # просто «развязка» (маршрут «от развязки до …», у Разумного)
    (re.compile(r"\bаэропорт\w*", re.I), 50.6439, 36.5900, 1500),                                         # аэропорт Белгорода (примерно)
    (re.compile(r"сити\s*-?\s*молл\w*", re.I), 50.552568, 36.568031, 800),                              # ТЦ «Сити Молл» (центр)
    (re.compile(r"\bвокзал\w*", re.I), 50.5940, 36.6040, 800),                                           # железнодорожный вокзал Белгорода (примерно; «автовокзал» сюда не входит)
    (re.compile(r"^\W*юго-?запад\w*\W*$", re.I), 50.5735, 36.5522, 2000),                              # «Юго-Западный» = юго-западная часть города
    (re.compile(r"я\W+люблю\W+белгород", re.I), 50.526444, 36.519001, 400),                           # кольцо «Я люблю Белгород»
]
JUNK = r"cloudtips|благодарим|донат|подписывайтесь|подпишись|реклам|https?://|www\.|t\.me|max\.ru|rustore|приложени\w*|скачай|наши каналы|наш\w* канал|официальн\w+ канал"   # реклама и ссылки: тревогой не считаются
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

TYPEWORDS = {"улица", "ул", "микрорайон", "мкр", "район", "город", "развязка", "кольцо", "аэропорт", "вокзал", "село", "поселок", "посёлок", "деревня", "хутор", "станица", "трасса"}
REPORT = r"в\s+том\s+числе|без\s+пострадавших|жертв\w*|\bвсу\b|поврежд\w*|атаковал\w*|пострада\w*|погиб\w*|ранен\w*|оперштаб|разрушен\w*|возгоран\w*|пожар\w*"

def skip_reason(text):
    low = text.lower()
    if re.search(JUNK, low): return "реклама или ссылка"
    if re.search(REPORT, low): return "сводка о последствиях"
    return "нет признаков оповещения"

def classify(text):
    low = text.lower()
    if re.search(JUNK, low): return None
    calm = re.search(r"не наблюда|активности нет", low)   # «На данный момент активность не наблюдаем» = спокойно
    if re.search(REPORT, low) and not re.search(r"летит|летят|подлета|движется|над\s+вам|займите|в\s+ваш|тревог", low): return None   # сводка о последствиях («повреждена кровля»), не оповещение
    nocl = re.search(r"отбо\w*\s+(?:пока\s+)?не\s|не\s+да[её]м\s+отбо|без\s+отбо|отбо\w*\s+нет", low)   # «отбой не даем», «отбоя не будет»: это не отбой
    ob = "отбой" in low and not nocl
    if "ракетн" in low: return "rocket_clear" if (ob or calm) else "rocket"
    if re.search(r"пуск\w*\s+(?:с\s+)?самол[её]т|пуск\w*\s+ракет", low): return "rocket"   # «Пуски с самолёта противника»
    if ob and re.search(r"опасност\w*(?:\s+\w+)?\s+сохраня|\bно\s+опасност|сохраня\w*\s+опасност", low): return "downgrade"   # красное снимается, остаётся жёлтое
    if ob or calm or re.search(r"небо чисто|можно продолжить|продолжить движение|миновала", low): return "clear"
    if "внимание" in low or re.search(r"дрон|бпла|беспилот|в сторону|стоп движ|остановить движение|остановите движение", low): return "alert"
    return None

def extract_places(text):
    """Названия мест: в каждом предложении берём часть до служебной фразы («займите укрытие», «находитесь в укрытии», «над вами…»)."""
    t = re.sub(r"^\s*(внимание|отбой)[,!.:\s]*", "", text, flags=re.I)
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", t):
        out += _places_in(re.split(STOP, sent, maxsplit=1, flags=re.I)[0], text)
    if out and re.match(r"(?:улица|ул\.)\s", out[0], re.I):   # «улица Корочанская, Волчанская, Студенческая»: все названия улицы
        out = [out[0]] + ["улица " + x if re.fullmatch(r"[А-ЯЁ][а-яё]+(?:ская|ная|ая|ой|ий)", x) else x for x in out[1:]]
    return out

def _places_in(head, text):
    out = []
    for p in re.split(r"[\n,;:.!]|\bа\s+также\b|\bи\s+также\b|\bтакже\b|\bплюс\b|\bеще\b|\bещё\b|\bи\b", head, flags=re.I):
        p = re.sub(r"\s+", " ", re.sub(NOISE, " ", p, flags=re.I)).strip(" \"'«»-")
        p = re.sub(r"\s+", " ", re.sub(r"(?<!\S)(?:а|и|в|во|на|по|к|у|с|со|о|об)(?!\S)", " ", p, flags=re.I)).strip(" \"'«»-")   # одиночные предлоги и союзы («дубовое а»)
        p = re.sub(r"^(?:в\s+)?район[ае]?\s+|^(?:рядом\s+с|около|возле|вблизи|недалеко\s+от|над|на)\s+", "", p, flags=re.I).strip()
        if p.lower() in ("район", "района", "районе"): continue
        mm = re.fullmatch(r"((?:[А-ЯЁа-яё-]+(?:ский|цкий|ской|ный)\s+)+[А-ЯЁа-яё-]+(?:ский|цкий|ской|ный))\s+район(?:ы|ов)", p)
        if mm:   # несколько районов подряд без запятых
            out += [w + " район" for w in mm.group(1).split()]; continue
        p = re.sub(r"\bрайоны\b|\bрайонов\b", "район", p, flags=re.I)
        if p.lower() in ("район", "района", "районе"): continue
        if len(p) > 2 and not re.search(NEIGH, p, re.I):
            if "районы" in text.lower() and "район" not in p.lower() and re.search(r"(ий|ый|ой)$", p):
                p += " район"   # список вида «Яковлевский, Борисовский районы»
            out.append(p)
    return out

def stems(s): return {w[:5] for w in re.findall(r"\w+", s.lower().replace("ё", "е")) if len(w) > 3}

SETTLE = {"village", "hamlet", "town", "isolated_dwelling", "locality", "farm", "allotments"}
CITYLIKE = SETTLE | {"city", "suburb", "quarter", "neighbourhood"}

PLACE = SETTLE | {"city", "suburb", "quarter", "neighbourhood", "residential"}

def _shape(r, q):
    lat, lon = float(r["lat"]), float(r["lon"])
    bb = [float(x) for x in r.get("boundingbox", [lat, lat, lon, lon])]  # юг, север, запад, восток
    size = max((bb[1] - bb[0]) * 111, (bb[3] - bb[2]) * 111 * math.cos(math.radians(lat)))  # км
    t, gj = r.get("type", ""), r.get("geojson") or {}
    poly = gj if gj.get("type") in ("Polygon", "MultiPolygon") else None
    if re.search(r"район|округ", q.lower()):   # район или округ целиком: бледная заливка по границе
        if poly and size <= 150:
            return {"lat": lat, "lon": lon, "kind": "district", "poly": poly, "r_in": None, "r_out": 0}
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    if t in PLACE or (t == "administrative" and size <= (40 if poly else 12)):   # город, посёлок, село, микрорайон: всегда одного вида (по границе или кругом)
        if poly and size <= 60:
            return {"lat": lat, "lon": lon, "kind": "settlement", "poly": poly, "r_in": None, "r_out": size * 500 + 1000}
        r_in = min(3000, max(1000, size * 500)) if size <= 12 else 3000
        return {"lat": lat, "lon": lon, "kind": "settlement", "r_in": r_in, "r_out": round(r_in * 1.5)}
    if t == "administrative" or size > 12:   # крупное административное образование: бледная заливка
        return {"lat": lat, "lon": lon, "kind": "district", "r_in": 2000, "r_out": 3000}
    return {"lat": lat, "lon": lon, "kind": "object", "r_in": 800, "r_out": 1200}

def shape(r, q):
    d = _shape(r, q)
    d["nt"] = r.get("type", "")   # тип объекта в OpenStreetMap (для подсказок в режиме проверки)
    d["dn"] = ", ".join(r.get("display_name", "").split(", ")[:3])   # как место называется в карте
    return d

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

def near_search(query, qs):
    """Собираем варианты: сёла Белгородского района, всё что нашлось рядом с Белгородом (в том числе внутри города) и города из всей области; выбирает resolve()."""
    res, seen = [], set()
    key = lambda x: (round(x["lat"], 3), round(x["lon"], 3))
    def add(items):
        for x in items:
            if key(x) not in seen: seen.add(key(x)); res.append(x)
    if not re.search(r"белгород", query, re.I):
        add([x for x in nominatim(query + ", Белгородский район", qs, vb=LOCAL_VB) if x["kind"] == "settlement"])
    loc = nominatim(query, qs, vb=LOCAL_VB)
    add([x for x in loc if x["kind"] == "settlement"])
    if not any(x.get("nt") in ("town", "city") for x in res):   # рядом города нет: смотрим, нет ли города с таким названием дальше (Алексеевка, Новый Оскол…)
        wide = nominatim(query, qs)
        add([x for x in wide if x.get("nt") in ("town", "city")])
        if not res: res = wide or loc
    return res

def district_variants(nq):
    """«Губкинский район» -> перебор: район, городской округ, муниципальный округ."""
    m = re.fullmatch(r"\s*(\S+?(?:ск|цк)(?:ий|ая|ое))\s+(?:городской\s+)?(?:муниципальный\s+)?(?:район|округ)\s*", nq, re.I)
    if not m: return None
    b = m.group(1)
    return [f"{b} район", f"{b} городской округ", f"{b} муниципальный округ", f"{b} округ"]

NOM_LOCK = threading.Lock()
KEEP = ("lat", "lon", "type", "class", "display_name", "boundingbox", "importance")

def slim(x):
    d = {k: x[k] for k in KEEP if k in x}
    g = x.get("geojson") or {}
    d["geojson"] = g if g.get("type") in ("Polygon", "MultiPolygon") and x.get("class") in (None, "boundary", "place") else {"type": "Point", "coordinates": [float(x["lon"]), float(x["lat"])]}
    return d

def nominatim(query, q_for_shape, limit=10, vb=None):
    thr = 0.003 if re.search(r"район|округ", q_for_shape.lower()) else 0.001
    key = f"nr|{vb or ''}|{limit}|{thr}|{query.lower()}"
    r = geocache.get(key)
    if r is None:   # сырой ответ карты запоминается (и попадает в файл «найденные места»)
        with NOM_LOCK:   # один запрос за раз и не чаще раза в секунду, даже если одновременно идёт прогрев
            r = geocache.get(key)
            if r is None:
                r = [slim(x) for x in requests.get("https://nominatim.openstreetmap.org/search", headers={"User-Agent": UA}, timeout=15,
                    params={"q": f"{query}, {REGION}", "format": "json", "limit": limit, "viewbox": vb or VIEWBOX, "bounded": 1,
                            "polygon_geojson": 1, "polygon_threshold": thr}).json()]
                time.sleep(1.1)
                geocache[key] = r
                save_cache()
    name = lambda x: x.get("display_name", "").split(",")[0].strip().lower()
    isp = lambda x: (x.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon")
    def bsz(x):   # размер объекта по его рамке, км
        try:
            bb = [float(v) for v in x["boundingbox"]]; la = float(x["lat"])
            return max((bb[1] - bb[0]) * 111, (bb[3] - bb[2]) * 111 * math.cos(math.radians(la)))
        except Exception: return 99
    # границу берём только у настоящих границ или населённых мест (не у ж/д станции, промзоны и т. п.) и только если она не крошечная
    polys = {name(x): x["geojson"] for x in r if isp(x) and x.get("class") in (None, "boundary", "place") and bsz(x) >= 0.3}
    out = []
    for x in r:
        if isp(x) and x.get("type") in CITYLIKE and bsz(x) < 0.3:
            x = {**x, "geojson": None}   # крошечный «контур» вместо границы села: не используем
        elif not isp(x) and x.get("type") in CITYLIKE and name(x) in polys:
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

def border_near(name, pt):
    """Граница села из поиска по карте (если она там есть) рядом с закреплённой точкой: берём её вместо круга."""
    cands = near_search(name, name)
    seen = {(round(x["lat"], 3), round(x["lon"], 3)) for x in cands}
    cands += [x for x in nominatim(name, name) if x["kind"] == "settlement" and (round(x["lat"], 3), round(x["lon"], 3)) not in seen]
    near = [x for x in cands if x.get("poly") and dkm(x, pt) <= 3]
    return min(near, key=lambda x: dkm(x, pt))["poly"] if near else None

def candidates(q):
    """Список вариантов места (до 5), лучший по важности первым."""
    low = q.lower()
    for k, v in manual.items():   # places.json: [широта, долгота] или [широта, долгота, радиус_в_метрах]
        if k in low:
            rad = v[2] if len(v) > 2 else 800
            pin = {"lat": v[0], "lon": v[1], "kind": "settlement" if len(v) > 3 else "object", "r_in": rad, "r_out": round(rad * 1.5), "imp": 1,
                   "nt": "вручную", "dn": k}
            if len(v) > 4:   # есть название для поиска: если у села в карте нарисована граница, рисуем по ней
                try:
                    b = border_near(v[4], (v[0], v[1]))
                    if b: pin = as_poly(pin, b)
                except Exception: pass
            return [pin]
    cp = city_part(q)
    if cp: return [cp]
    for rx, la, lo, rad in ALIASES:
        if rx.search(q): return [{"lat": la, "lon": lo, "kind": "object", "r_in": rad, "r_out": round(rad * 1.5), "imp": 1}]
    if KM.search(q): return []   # километр трассы не ищем по карте: поиск вернул бы случайное место
    try:
        nq = normalize(q)
        sd = split_district(nq)
        res = []
        if sd:   # «Архангельское Губкинского ГО»: сначала ищем само село, из одинаковых берём ближайшее к названному округу
            res = near_search(sd[0], sd[0])
            seen = {(round(x["lat"], 3), round(x["lon"], 3)) for x in res}   # село из названного района может лежать далеко от Белгорода: добавляем поиск по всей области
            res += [x for x in nominatim(sd[0], sd[0]) if x["kind"] == "settlement" and (round(x["lat"], 3), round(x["lon"], 3)) not in seen]
            hint = nominatim(sd[1], sd[1], 1)
            if res and hint:
                res = [min(res, key=lambda x: dkm(x, (hint[0]["lat"], hint[0]["lon"])))]
        if not res:
            fallback = []
            for v in (district_variants(nq) or []):   # «Губкинский район» на карте это городской округ: ищем вариант с настоящей границей
                got = nominatim(v, v)
                if any(c.get("poly") for c in got):
                    res = [c for c in got if c.get("poly")] + [c for c in got if not c.get("poly")]
                    break
                if got and not fallback: fallback = got   # запасной вариант без границы (круг)
            res = res or fallback
        if not res:
            res = near_search(nq, q)
    except Exception:
        return []
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
    m = manual_pos(pl)
    if m: return [m]   # нарисованный вручную контур важнее поиска
    seen, out = set(), []
    for f in name_forms(pl):
        for c in geo(f):
            k = (round(c["lat"], 3), round(c["lon"], 3))
            if k not in seen: seen.add(k); out.append(c)
    return out

def near_bonus(km):
    return 0.6 + (20 - km) * 0.02 if km <= 20 else 0.25 if km <= 40 else 0   # чем ближе к Белгороду (до 20 км), тем лучше

def prefer_towns(c):
    """Город с тем же названием, что и село, важнее, если села поблизости от Белгорода (до 25 км) нет: так Волоконовка = город, а не маленькое село."""
    towns = [x for x in c if x.get("nt") in ("town", "city")]
    if towns and len(towns) < len(c) and not any(dkm(x, (CENTER["lat"], CENTER["lon"])) <= 25 for x in c if x not in towns):
        return towns
    return c

def resolve(places, cands=candidates):
    """Выбирает место для каждого названия. Из одинаковых сёл чуть предпочитается то, что ближе к Белгороду. Если названий несколько («Никольское» и «Дубовое»), а одинаковых сёл в области много,
    берётся набор, где места ближе всего друг к другу (то есть то Никольское, что рядом с Дубовым)."""
    raw = {pl: cands(pl) for pl in places}
    cl = {pl: prefer_towns(only_settlements(c)) for pl, c in raw.items()}
    cl = {pl: sorted(c, key=lambda x: -x["imp"])[:10] for pl, c in cl.items() if c}
    pick = {pl: c[0] for pl, c in cl.items()}
    amb = [pl for pl, c in cl.items() if len(c) > 1][:4]
    if amb:
        fixed = [pick[pl] for pl in cl if pl not in amb]
        best = None
        for combo in itertools.product(*[cl[pl] for pl in amb]):
            pts = fixed + list(combo)
            near = 0   # для каждого неоднозначного места считаем расстояние до ближайшего другого названного места (дальние соседи не тянут за собой)
            for a in combo:
                others = [p for p in pts if p is not a]
                if others: near += min(min(dkm(a, (b["lat"], b["lon"])) for b in others), 100)
            cost = (near - 2 * sum(x["imp"] for x in combo)
                    - 2 * sum(near_bonus(dkm(x, (CENTER["lat"], CENTER["lon"]))) for x in combo))   # пригород Белгорода предпочтительнее
            if best is None or cost < best[0]: best = (cost, combo)
        pick.update(dict(zip(amb, best[1])))
    for pl in list(pick):   # какие варианты названия были и какой выбран (видно в режиме проверки)
        c, ch = cl[pl], pick[pl]
        if True:
            pick[pl] = {**ch, "drop": [{"nt": x.get("nt"), "km": round(dkm(x, (CENTER["lat"], CENTER["lon"])), 1), "kind": x.get("kind")} for x in raw[pl] if x not in c][:5], "alts": [
                {"km": round(dkm(x, (CENTER["lat"], CENTER["lon"])), 1), "nt": x.get("nt"), "dn": x.get("dn"), "imp": round(x["imp"], 2), "chosen": x is ch} for x in c[:8]]}
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
        save_cache()
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
                save_cache()
                return g
    except Exception as e:
        print("oblast zone error:", e)
    OZ_FAIL["t"] = time.time()
    return None

CTX_GO = re.compile(r"(?<![А-ЯЁа-яё-])(?!(?:Внимание|Отбой|Тревога|Опасность|Белгород)\b)([А-ЯЁ][а-яё-]{2,}(?: [А-ЯЁ][а-яё-]{2,})?),?[ ]+([А-ЯЁ][а-яё-]+?(?:ского|цкого|ский|цкий))\s+(?:ГО|МО)\b")
CTX_RAION = re.compile(r"(?<![А-ЯЁа-яё-])(?!(?:Внимание|Отбой|Тревога|Опасность|Белгород)\b)([А-ЯЁ][а-яё-]{2,}(?: [А-ЯЁ][а-яё-]{2,})?)[,\n][ \n]*([А-ЯЁ][а-яё-]+?(?:ского|цкого|ский|цкий))\s+район\b")

def _ctx_sub(m):
    name, adj = m.group(1), m.group(2)
    if re.search(r"(ский|цкий|ского|цкого)$", name.split()[-1]): return m.group(0)   # «Губкинский ГО»: это сам округ, а не село в нём
    return f"{name} {adj[:-3] + 'ий' if adj.endswith('ого') else adj} район"

def fix_names(t):
    t = re.sub(r"\b([Уу])син(к\w*)", r"\1стин\2", t)        # опечатка «Усинки» -> «Устинки»
    t = re.sub(r"[\u203c\u2049\u2600-\u27bf\ufe0f\u200d\U0001F000-\U0001FAFF]", " ", t)   # значки «‼️», «📍» не часть названий
    t = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " ", t)   # время в тексте («14:39») не часть названия
    t = re.sub(r"\b[Бб]лижн\w+\s+[Ии]гумен\w+", "Ближняя Игуменка", t); t = re.sub(r"\b[Дд]альн\w+\s+[Ии]гумен\w+", "Дальняя Игуменка", t)
    t = re.sub(r"\b[Нн]овосадов\w*", "Новосадовый", t)
    t = re.sub(r"\b[Рр]азумн\w+[,\s-]+71\b", "Разумное-71", t)   # «Разумная, 71» = микрорайон Разумное-71
    t = re.sub(r"\b[Нн]икольская\b(?!\s+ул)", "Никольское", t)
    t = re.sub(r"\b[Сс]оломин[аы]?\b", "Соломино", t); t = re.sub(r"\b[Сс]оломенск\w+\s+мост", "Соломинский мост", t)
    t = re.sub(r"(развязк\w*[\s-]+[Бб]абочк\w*)", r"развязка Бабочка,", t)   # «развязка Бабочка разумное» = два места
    t = re.sub(r"(?i)\bв\s+направлении\s+(?:[а-яё]+\s+){0,2}област\w+", " ", t)           # «в направлении Курской области»: куда летит, а не где опасно
    t = re.sub(r"(?i)(?:\bи\s+)?\bдалее\s+(?:на|в\s+направлении)\s+(?:(?:[а-яё]+,?\s+(?:и\s+)?){0,3}област\w*|(?-i:[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+)?))", " ", t)   # «и далее на Курскую, Воронежскую область», «и далее на Старый Оскол»
    t = re.sub(r"(?i)\bот\s+[а-яё-]+\s+(?:на|в\s+сторону)\s+(?:север\w*|юг\w*|запад\w*|восток\w*|северо-\w+|юго-\w+)", " ", t)   # «От Харькова на север»: откуда и куда летят, а не место
    t = re.sub(r"(?i)(бпла)\s+от\s+[^\n.,]+", r"\1", t)                                      # «фиксации БПЛА от Гг», «БПЛА от Новооскольского района»: откуда летит
    t = CTX_GO.sub(_ctx_sub, t); t = CTX_RAION.sub(_ctx_sub, t)                              # «Архангельское Губкинского ГО» -> «Архангельское Губкинский район»
    t = re.sub(r"\b([А-Яа-яЁё-]{3,}?)(?:ском|ский|ского)\s+(?:МО|ГО|м\.\s?о\.|(?:муниципальн|городск)\w+\s+округ\w*)", r"\1ский район", t)   # «в Яковлевском МО», «Губкинский ГО» = Яковлевский район
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

TYPEWORD = re.compile(r"^(?:поселок|посёлок|пос\.?|село|с\.|деревня|дер\.|хутор|х\.|станица|город|г\.)\s+", re.I)

def name_forms(ph):
    """«Никольского» -> «Никольское», «поселок Дубовое» -> «Дубовое»: сначала как написано, потом без слова «поселок/село», потом в начальной форме."""
    ph = ph.strip()
    base = [ph]
    st = TYPEWORD.sub("", ph).strip()
    if st and st != ph: base.append(st)
    out = []
    for b in base:
        out.append(b)
        full = []
        for w in b.split():
            for a, c in SUF:
                if w.lower().endswith(a) and len(w) > len(a) + 2:
                    full.append(w[:-len(a)] + c); break
            else: full.append(w)
        f = " ".join(full)
        if f != b: out.append(f)
    return list(dict.fromkeys(out))

def buffer_polygon(pts, half_m, steps=8):
    """Полоса заданной ширины вдоль ломаной (скруглённые концы и углы) как один многоугольник: чтобы участок трассы выглядел так же, как остальные зоны."""
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110540.0
    P = [(p[1] * kx, p[0] * ky) for p in pts]
    d = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(P, P[1:])]
    d = [(x / (math.hypot(x, y) or 1), y / (math.hypot(x, y) or 1)) for x, y in d]
    ln = [(-y, x) for x, y in d]                                  # левые нормали сегментов
    ang = lambda n: math.atan2(n[1], n[0])
    def side(sgn):
        out = [(P[0][0] + sgn * half_m * ln[0][0], P[0][1] + sgn * half_m * ln[0][1])]
        for i in range(1, len(P) - 1):
            n0, n1 = ln[i - 1], ln[i]
            left_turn = d[i - 1][0] * d[i][1] - d[i - 1][1] * d[i][0] > 0
            inner = left_turn if sgn > 0 else not left_turn
            if inner:   # внутренняя сторона поворота: одна точка
                mx, my = n0[0] + n1[0], n0[1] + n1[1]
                L = math.hypot(mx, my) or 1.0
                k = max(L / 2, 0.25)
                out.append((P[i][0] + sgn * half_m * mx / L / k, P[i][1] + sgn * half_m * my / L / k))
            else:       # внешняя сторона: дуга
                a0, a1 = ang((sgn * n0[0], sgn * n0[1])), ang((sgn * n1[0], sgn * n1[1]))
                da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
                out += [(P[i][0] + half_m * math.cos(a0 + da * t / 4), P[i][1] + half_m * math.sin(a0 + da * t / 4)) for t in range(5)]
        out.append((P[-1][0] + sgn * half_m * ln[-1][0], P[-1][1] + sgn * half_m * ln[-1][1]))
        return out
    L_, R_ = side(1), side(-1)
    aN, a0 = ang(ln[-1]), ang(ln[0])
    end = [(P[-1][0] + half_m * math.cos(aN - math.pi * t / steps), P[-1][1] + half_m * math.sin(aN - math.pi * t / steps)) for t in range(1, steps)]
    start = [(P[0][0] + half_m * math.cos(a0 + math.pi - math.pi * t / steps), P[0][1] + half_m * math.sin(a0 + math.pi - math.pi * t / steps)) for t in range(1, steps)]
    ring = L_ + end + R_[::-1] + start
    ring = [[x / kx, y / ky] for x, y in ring]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}

def route_len(pts):
    return sum(math.hypot((a[0] - b[0]) * 111, (a[1] - b[1]) * 111 * math.cos(math.radians(a[0]))) for a, b in zip(pts, pts[1:]))

STRONG = re.compile(r"тревог|летит|летят|подлета|движется|движутся|над\s+вам|над\s+город|в\s+ваш\w*\s+(?:сторону|направлении)|займите|уничтожен|высок\w*\s+активност|\bстоп\W+движ\w*|находи(?:тесь|мся)\s+в\s+укрыт|над\s+(?:трасс|дорог|сел|посел|район)\w*|фиксаци\w*|зафиксирован\w*|\bвсем\s+стоп\b|остановит\w*\s+движ\w*|остановить\s+движ\w*|прекратит\w*\s+движ\w*", re.I)

ALLW = r"по\s+всем|по\s+всей|на\s+всей|вс(?:ем|ех|е|ё)\s+(?:ранее|тревог|зон|участк|направлени)"   # общий отбой: «по всем ранее объявленным», «по всей области», «всех ранее…»; одно «Внимание всем!» не считается

EVENT = re.compile(r"(?:пр[оа]л[её]т\w*|опасност\w*|тревог\w*|угроз\w*)(?:[\s,:]+(?:ударн\w*|атак\w*|по|от|бпла|дрон\w*|беспилот\w*|вышеперечисленн\w*|район\w*|fpv|с|со|стороны|направлени\w*|в|сохраня\w*))*", re.I)

def split_parts(t):
    """Сообщение из нескольких частей со своим событием у каждой: «А, Б районы Пролет ударных БПЛА В район, опасность…»."""
    ms = list(EVENT.finditer(t))
    if len(ms) < 2: return [t]
    if extract_places(t[:ms[0].start()]):   # места перед событием
        ends = [m.end() for m in ms]; ends[-1] = len(t)
        return [t[a:b] for a, b in zip([0] + ends[:-1], ends)]
    return [t[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(t))] for i, m in enumerate(ms)]   # событие перед местами: «Опасность: А. Тревога: Б»

def level_of(text):
    """alarm = БПЛА зафиксирован (летит, над вами, займите укрытие, тревога…), danger = просто «Внимание» или опасность (возможен пролёт, высокая вероятность)."""
    if STRONG.search(text): return "alarm"
    if re.search(r"пр[оа]л[её]т", text, re.I) and not re.search(r"(?:возмож|вероятн|угроз|опасност)\w*[\s\w]{0,12}пр[оа]л[её]т", text, re.I): return "alarm"   # «Пролет ударных БПЛА» = летят; «возможен пролёт» = опасность
    return "danger"

OV = {"t": 0, "err": ""}
OVERPASS_ON = os.getenv("OVERPASS", "1") != "0"   # на Render поставь OVERPASS=0, если этот сервис контуров застройки не отвечает
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
    if not OVERPASS_ON: return None, "контуры застройки (Overpass) выключены переменной OVERPASS=0"
    if time.time() - OV["t"] < 1800: return None, "пауза после сбоя Overpass: " + OV["err"]
    q = f'[out:json][timeout:10];(way(around:1000,{lat},{lon})["landuse"="residential"];relation(around:1000,{lat},{lon})["landuse"="residential"];);out geom;'
    els, errs = None, []
    for u in OV_URLS:   # если один сервер не ответил, пробуем следующий
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=6, params={"data": q})
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

CASE = [("ского", "ское"), ("ском", "ское"), ("ском", "ский"), ("ого", "ое"), ("ому", "ое"), ("ым", "ое"), ("ым", "ый"), ("им", "ий"), ("ом", "ое"), ("ом", "ый"),
        ("ой", "ая"), ("ой", "а"), ("ую", "ая"), ("ова", "ово"), ("ева", "ево"), ("ина", "ино"), ("ки", "ка"), ("ке", "ка"), ("ку", "ка"), ("кой", "ка"),
        ("ни", "нь"), ("ги", "га"), ("ге", "га"), ("ы", "а"), ("е", "а"), ("у", "а"), ("а", "")]

def case_forms(ph):
    """Все вероятные начальные формы названия по падежным окончаниям («Новой Нелидовке» -> «Новая Нелидовка»); для сверки с нарисованными контурами."""
    opts = []
    for w in ph.split():
        o = [w] + [w[:-len(a)] + b for a, b in CASE if w.lower().endswith(a) and len(w) > len(a) + 2]
        opts.append(list(dict.fromkeys(o))[:6])
    return [" ".join(c) for c in itertools.product(*opts[:3])]

def manual_pos(name):
    """Село из outlines.json (нарисованное вручную) по названию в любом падеже; даёт готовое место с контуром."""
    forms = [f.lower() for f in case_forms(name) + case_forms(TYPEWORD.sub("", name))]
    for k, g in outlines.items():
        if any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", f) for f in forms):
            g = norm_geo(g)
            rings = [g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]]
            pts = [p for r in rings for p in r]
            return as_poly({"lat": sum(p[1] for p in pts) / len(pts), "lon": sum(p[0] for p in pts) / len(pts), "kind": "settlement",
                            "r_in": 1000, "r_out": 1500, "imp": 1}, g)
    return None

def with_outline(pos, name=""):
    """Село рисуем по контуру: сначала нарисованный вручную (outlines.json), потом застройка из OpenStreetMap; иначе остаётся круг (с пометкой, почему)."""
    if pos.get("kind") != "settlement" or pos.get("poly") or pos.get("imp") == 1: return pos
    m = manual_pos(name)
    if m: return m
    key = f"__ov_{pos['lat']:.3f}_{pos['lon']:.3f}"
    why = ""
    if key not in geocache:
        g, why = overpass_outline(pos["lat"], pos["lon"])
        if g is None and not why.startswith("в карте нет"): return {**pos, "why": why}   # сбой: «нет контура» не запоминаем
        geocache[key] = g or ""
        save_cache()
    g = geocache.get(key)
    if not g: return {**pos, "why": why or "в карте нет нарисованной застройки рядом с селом (запомнено)"}
    return as_poly(pos, g)

def zkey(pos): return (pos["kind"], round(pos["lat"], 2), round(pos["lon"], 2))

def build(posts, now=None, geo=candidates, zones=rocket_zones, ozone=oblast_zone):
    now = now or time.time()
    drone, unknown, oblast_drone = {}, [], None
    def real_names(cand, loose_words):
        zs = set().union(*[stems(z["place"]) for z in drone.values()]) if drone else set()
        out = []
        for x in cand:
            if stems(x) & zs: out.append(x); continue
            try:
                if merge_forms(x, geo): out.append(x)
            except Exception: pass
        return out + [w for w in loose_words if stems(w) & zs]
    feed = []
    issues = []   # сообщения, которые сайт не понял (для журнала)
    put_count = [0]
    fresh_rk = lambda: {"city": False, "oblast": False, "places": {}, "last": None}
    rk = fresh_rk()
    last_rk = {}   # канал -> когда было последнее ракетное сообщение (для продолжений «В сторону…», «Шебекино»)
    def put(pos, name, p, hhmm, lvl):
        put_count[0] += 1
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
        li = last_rk.get(p["ch"])
        cont = bool(li and p["ts"] - li["ts"] <= 300 and len(p["text"]) <= 60 and (kind is None or (kind == "alert" and re.match(r"\s*в\s+сторону\b", p["text"].lower()))))
        if cont: kind = "rocket"   # «Пуски с самолёта» / «Ракетная опасность», а следом короткое «В сторону Журавлевки» / «Шебекино»
        if not kind:
            if SUSPECT.search(p["text"]): issues.append({"key": f'{p["ch"]}:{p["id"]}', "ch": p["ch"], "ts": p["ts"], "text": p["text"][:400], "tag": "пропущено", "reason": skip_reason(txt)})
            continue
        if now - p["ts"] > (ROCKET_TTL if kind.startswith("rocket") else TTL_MIN) * 60: continue
        low = p["text"].lower()
        hhmm = datetime.fromtimestamp(p["ts"], MSK).strftime("%H:%M")
        routes, rest, loose = extract_routes(txt) if kind in ("alert", "clear", "downgrade") else ([], txt, [])
        places = extract_places(rest)
        if cont and not places: continue
        fpos = None
        u0, pc0, od0 = len(unknown), put_count[0], oblast_drone
        if kind == "rocket_clear":            # первый отбой из любого канала снимает всё
            rk = fresh_rk(); last_rk.pop(p["ch"], None)
        elif kind == "rocket":
            if not places and re.search(r"сохраня", low) and re.search(r"ранее\s+объявленн", low): pass   # «по ранее объявленным регионам сохраняется»: ничего нового
            else:
                rk["last"] = {"time": hhmm, "age": round((now - p["ts"]) / 60), "text": p["text"]}
                if cont and li and li.get("default_city"): rk["city"] = False   # место названо следом: это не весь город
                if not places:
                    if re.search(r"курск|воронеж|липецк|харьков(?!ск\w*\s+гор)", low) and "белгород" not in low: pass   # про соседний регион
                    elif "област" in low: rk["oblast"] = True
                    else:
                        rk["city"] = True; fpos = CENTER   # без места: Белгород и Белгородский район
                        last_rk[p["ch"]] = {"ts": p["ts"], "default_city": True}
                else: last_rk[p["ch"]] = {"ts": p["ts"], "default_city": False}
                pk = resolve([x for x in places if not BEL.search(x.lower())], lambda pl: merge_forms(pl, geo))
                for pl in places:
                    if BEL.search(pl.lower()): rk["city"] = True; fpos = fpos or CENTER
                    else:
                        pos = pk.get(pl)
                        if pos: rk["places"][zkey(pos)] = {**pos, "place": pl}; fpos = fpos or pos
        elif kind in ("clear", "downgrade"):
            names = real_names(places + [e for a, bs in routes for e in [a] + bs], [w for phr in loose for w in phr.split()])
            general = not names or re.search(ALLW, low)   # общий: без настоящих названий мест или «по всем…»
            if kind == "clear":
                if general: drone, unknown, oblast_drone = {}, [], None
                else:
                    ss = set().union(*[stems(x) for x in names])
                    drone = {k: z for k, z in drone.items() if not (stems(z["place"]) & ss)}
            else:   # «Отбой по ранее объявленным тревогам, но опасность сохраняется»: тревога снижается до опасности, зоны остаются
                ss = set().union(*[stems(x) for x in names]) if names else set()
                for z in drone.values():
                    if general or (stems(z["place"]) & ss): z["level"] = "danger"; z["last"] = max(z["last"], p["ts"])
                if general and oblast_drone: oblast_drone = {**oblast_drone, "level": "danger"}
        elif kind == "alert":
            lvl = level_of(txt)
            plv = {}   # «Краснояружский район Пролет ударных БПЛА. Ивнянский район, опасность…»: у каждой части свой уровень
            parts = split_parts(rest)
            if len(parts) > 1:
                for part in parts:
                    for x in extract_places(part): plv.setdefault(x, level_of(part))
                if plv: places = list(plv)
            lowx = re.sub(r"от\s+белгородск\w+\s+област\w+", " ", low)   # «Воронежская область от Белгородской области»: про соседний регион
            other_first = re.match(r"\s*(?:воронежск|курск|брянск|орловск|липецк|харьковск|сумск)", low)
            mass = re.search(r"массов\w*\s+пр[оа]л[её]т|массированн", lowx) and re.search(r"белгородск\w* област", lowx) and not other_first   # «массовые пролёты… Белгородская область»: по всей области, даже если названо место
            if re.search(r"всей территории|по всей области", lowx) or mass or (not places and not other_first and re.search(r"белгородск\w* област", lowx)):
                oblast_drone = {"time": hhmm, "level": lvl}   # тревога по всей области: плашка и оранжевая рамка, без заливки
            rc = lambda pl: merge_forms(pl, geo)
            for phr in loose:   # «трасса Никольское Таврово»: два названия подряд = участок; а «Новая Деревня» = одно название
                if merge_forms(phr, geo): places.append(phr)
                else:
                    ws = phr.split(); done = False
                    for k in range(1, len(ws)):   # «Шебекино Маслова Пристань» = «Шебекино» и «Маслова Пристань»
                        a, b = " ".join(ws[:k]), " ".join(ws[k:])
                        if merge_forms(a, geo) and merge_forms(b, geo): routes.append((a, [b])); done = True; break
                    if not done: routes.append((ws[0], ws[1:4]))
            for a, bs in routes:   # «от X до Y»: полоса вдоль трассы (прямая линия между селами, через все названные)
                names = [a] + bs; label = " — ".join(names)
                pk0 = resolve(names, rc)
                pts = [[pk0[n]["lat"], pk0[n]["lon"]] for n in names if n in pk0]
                if len(pts) >= 2 and route_len(pts) <= 50:
                    w = 3000 if route_len(pts) <= 20 else 6000
                    zr = {"kind": "route", "pts": pts, "w_in": w, "w_out": w + 2000, "r_in": None, "r_out": 0, "place": label,
                          "lat": sum(x[0] for x in pts) / len(pts), "lon": sum(x[1] for x in pts) / len(pts),
                          "poly": buffer_polygon(pts, w / 2)}
                    put(zr, label, p, hhmm, lvl); fpos = fpos or zr
                elif len(pts) == 1:
                    n1 = next(n for n in names if n in pk0); put(pk0[n1], n1, p, hhmm, lvl); fpos = fpos or pk0[n1]
                else:
                    unknown.append({"place": label + (" (участок слишком длинный, вероятно выбрано не то село)" if len(pts) >= 2 else ""), "time": hhmm})
                if pts:
                    for n in names:
                        if n not in pk0: unknown.append({"place": n, "time": hhmm})   # какое-то из названий не нашлось
            def split_glued(pls):   # два названия подряд без запятой («Разумное Крейда»): если целиком не находится, а по отдельности находятся, это два места
                res = []
                for pl in pls:
                    ws = pl.split()
                    if len(ws) >= 2 and ws[0].lower() not in TYPEWORDS and not KM.search(pl) and not rc(pl) and all(len(w) > 3 and rc(w) for w in ws): res += ws
                    else: res.append(pl)
                return res
            places = split_glued(places)
            pk = {k: with_outline(v, k) for k, v in resolve(places, rc).items()}
            for pl in places:
                pos = pk.get(pl)
                if not pos:
                    if KM.search(pl) and any(pk.get(x) for x in places): continue   # километр уточняет найденное место рядом
                    unknown.append({"place": pl, "time": hhmm}); continue
                fpos = fpos or pos
                put(pos, pl, p, hhmm, plv.get(pl, lvl))
        if kind == "alert":
            newu = [u["place"] for u in unknown[u0:]] if len(unknown) >= u0 else []
            base = {"key": f'{p["ch"]}:{p["id"]}', "ch": p["ch"], "ts": p["ts"], "text": p["text"][:400], "tag": "места"}
            if newu: issues.append({**base, "reason": "не найдено: " + ", ".join(newu[:6])})
            elif put_count[0] == pc0 and oblast_drone is od0: issues.append({**base, "reason": "ничего не нарисовано: места не распознаны"})
        feed.append({"ch": p["ch"], "time": hhmm, "age": round((now - p["ts"]) / 60), "kind": kind, "level": level_of(txt) if kind == "alert" else ("danger" if kind == "downgrade" else None), "text": p["text"][:220],
                     "lat": fpos["lat"] if fpos else None, "lon": fpos["lon"] if fpos else None})
    marks = [{**{k: v for k, v in z.items() if k not in ("sources", "last")},
              "age": round((now - z["last"]) / 60), "n": len(z["sources"])} for z in drone.values()]
    rocket = None
    if rk["city"] or rk["oblast"] or rk["places"]:
        rocket = {**rk["last"], "city": rk["city"], "oblast": rk["oblast"], "places": list(rk["places"].values()),
                  "zones": zones() if rk["city"] else None}
    return {"markers": marks, "unknown": unknown[:8], "rocket": rocket, "oblast_drone": oblast_drone, "oblast_poly": ozone() if (rk["oblast"] or oblast_drone) else None, "feed": feed[::-1][:40], "issues": issues, "status": status_out(),
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
    force = request.args.get("force") == "1"   # кнопка обновления на странице: читаем каналы сразу, но не чаще раза в 5 секунд
    with lock:
        if time.time() - state["last_fetch"] > (5 if force else 20):
            refresh_posts()
            state["last_fetch"] = time.time()
        out = build(list(state["posts"].values()))
        journal_add(out.pop("issues", []))
        out["fetch_age"] = round(time.time() - state["last_fetch"])   # сколько секунд назад проверены каналы
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
    out.pop("issues", None)
    out["weather"] = None; out["status"] = []
    return jsonify(out)

import html as _html

@app.route("/api/journal")
def api_journal():
    """Журнал сообщений, которые сайт не понял: не найденные места, ничего не нарисовано, пропущенные похожие на оповещения. Только с паролем."""
    if not TEST_KEY or request.args.get("key") != TEST_KEY: return ("", 404)
    key = request.args.get("key")
    if request.args.get("clear") == "1": JOURNAL.clear(); save_json(JFILE, JOURNAL)
    tag = request.args.get("tag")
    items = sorted((v for v in JOURNAL.values() if not tag or v.get("tag") == tag), key=lambda v: v.get("ts", 0), reverse=True)[:200]
    if request.args.get("json") == "1": return jsonify(items)
    q = lambda **kw: "?" + "&".join([f"key={key}"] + [f"{k}={v}" for k, v in kw.items()])
    cards = "".join(
        f"<div style='border-left:5px solid {'#f80' if v.get('tag') == 'места' else '#999'};background:#f6f6f6;margin:8px 0;padding:8px 10px;border-radius:4px'>"
        f"<small>{datetime.fromtimestamp(v.get('ts', 0), MSK).strftime('%d.%m %H:%M')} · {_html.escape(v.get('ch', ''))} · {_html.escape(v.get('tag', ''))}</small><br>"
        f"<b>{_html.escape(v.get('reason', ''))}</b><br><span style='white-space:pre-wrap'>{_html.escape(v.get('text', ''))}</span></div>" for v in items) or "<p>Пока пусто: всё, что приходило, сайт понял.</p>"
    body = ("<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<body style='font-family:sans-serif;padding:12px;max-width:760px;margin:auto;line-height:1.45'>"
            f"<h3>Журнал: что сайт не понял</h3><p>Записей: {len(items)}. "
            f"<a href='{q()}'>все</a> · <a href='{q(tag='места')}'>места</a> · <a href='{q(tag='пропущено')}'>пропущено</a> · <a href='{q(clear=1)}'>очистить</a></p>"
            "<p><small>«Места»: сообщение признано оповещением, но место не найдено или ничего не нарисовано. «Пропущено»: похоже на оповещение, но сайт его не использовал (реклама, сводка, нет признаков). Хранится 7 дней, при обновлении сайта на Render журнал обнуляется.</small></p>"
            + cards + "</body>")
    return Response(body, mimetype="text/html")

@app.route("/api/oblast")
def api_oblast():
    """Граница Белгородской области для затемнения соседних регионов; страница берёт её один раз."""
    g = oblast_zone()
    r = jsonify(g)
    r.headers["Cache-Control"] = "public, max-age=86400" if g else "no-cache"
    return r

PLACE_RATE = {}

@app.route("/api/place")
def api_place():
    """Поиск места для «Моих мест» на странице: до 5 вариантов с координатами (ходит в карту не чаще раза в 1,5 секунды с одного адреса)."""
    q = (request.args.get("q") or "").strip()[:60]
    if len(q) < 3: return jsonify([])
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    if now - PLACE_RATE.get(ip, 0) < 1.5: return jsonify({"error": "слишком часто"}), 429
    PLACE_RATE[ip] = now
    if len(PLACE_RATE) > 500: PLACE_RATE.clear()
    try:
        cl = prefer_towns(only_settlements(merge_forms(fix_names(q), candidates)))
    except Exception:
        cl = []
    cl = sorted(cl, key=lambda x: -(x["imp"] + near_bonus(dkm(x, (CENTER["lat"], CENTER["lon"])))))[:5]
    return jsonify([{"name": q, "dn": x.get("dn") or q, "lat": round(x["lat"], 5), "lon": round(x["lon"], 5),
                     "km": round(dkm(x, (CENTER["lat"], CENTER["lon"])), 1), "kind": x.get("kind")} for x in cl])

WARM_NAMES = [
    "Дубовое", "Таврово", "Никольское", "Новая Нелидовка", "Старая Нелидовка", "Майский", "Новая Деревня", "Шагаровка", "Репное", "Красное", "Долбино", "Веселая Лопань",
    "Октябрьский", "Красный Октябрь", "Устинка", "Нижний Ольшанец", "Разумное", "Крутой Лог", "Маслова Пристань", "Беловское", "Крейда", "Соломино", "Кошлаково", "Чураево",
    "Купино", "Протопоповка", "Головино", "Хутор Майский", "Нечаевка", "Пуляевка", "Ясные Зори", "Черемошное", "Северный", "Зелёная Поляна", "Мясоедово", "Севрюково",
    "Ближняя Игуменка", "Стрелецкое", "Болховец", "Пушкарное", "Драгунское", "Комсомольский", "Томаровка", "Строитель", "Борисовка", "Ракитное", "Бессоновка", "Орловка",
    "Белгород", "Шебекино", "Грайворон", "Валуйки", "Старый Оскол", "Губкин", "Короча", "Волоконовка", "Новый Оскол", "Алексеевка", "Бирюч", "Вейделевка", "Ивня",
    "Прохоровка", "Красногвардейское", "Ровеньки", "Чернянка", "Яковлево", "Харьковская гора", "Савино", "Новосадовый", "Ольшанка", "Бершаково", "Дмитриевка",
    "Губкинский район", "Старооскольский район", "Шебекинский район", "Грайворонский район", "Борисовский район", "Корочанский район", "Волоконовский район", "Валуйский район",
    "Белгородский район", "Яковлевский район", "Ракитянский район", "Красногвардейский район", "Новооскольский район", "Ивнянский район", "Прохоровский район",
    "Чернянский район", "Вейделевский район", "Алексеевский район", "Красненский район", "Ровеньский район", "Краснояружский район"]
WARM = {"running": False, "done": 0, "total": 0, "bad": [], "t0": 0}

def _warm():
    WARM.update(running=True, done=0, total=len(WARM_NAMES), bad=[], t0=time.time())
    for n in WARM_NAMES:
        try:
            if not candidates(n): WARM["bad"].append(n)
        except Exception:
            WARM["bad"].append(n)
        WARM["done"] += 1
    WARM["running"] = False

@app.route("/api/warm")
def api_warm():
    """Прогрев памяти: ищет на карте все названия из списка в фоне и запоминает. Открой ссылку один раз и жди."""
    if not TEST_KEY or request.args.get("key") != TEST_KEY: return ("", 404)
    if not WARM["running"] and request.args.get("status") != "1":
        threading.Thread(target=_warm, daemon=True).start(); time.sleep(0.3)
    w = WARM
    done = (not w["running"]) and w["total"] and w["done"] >= w["total"]
    mins = round((time.time() - w["t0"]) / 60, 1) if w["t0"] else 0
    body = (f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" + ("" if done else "<meta http-equiv='refresh' content='8'>") +
            f"<body style='font-family:sans-serif;padding:16px;line-height:1.5'><h3>Прогрев памяти</h3>"
            f"<p>{'Готово' if done else 'Идёт'}: {w['done']} из {w['total'] or len(WARM_NAMES)} названий, прошло {mins} мин.</p>"
            + ("<p><b>Теперь нажми: <a href='/api/geocache'>скачать найденные места</a></b> и загрузи файл на GitHub в корень.</p>" if done else "<p>Страница обновляется сама. Закрывать её можно, прогрев идёт на сервере.</p>")
            + (f"<p>Не нашлось: {', '.join(w['bad'])}</p>" if w["bad"] else "") + "</body>")
    return Response(body, mimetype="text/html")

@app.route("/api/geocache")
def geocache_dump():   # скачать найденные места, чтобы положить в репозиторий как geocache_seed.json
    body = json.dumps({k: v for k, v in geocache.items() if v}, ensure_ascii=False)
    return Response(body, mimetype="application/json", headers={"Content-Disposition": "attachment; filename=geocache_seed3.json"})

@app.route("/")
def index(): return send_from_directory("static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
