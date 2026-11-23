#bridge.py — Qwen (LM Studio, GPU0) -> ComfyUI (3070 Ti, GPU1); критик и оценка — та же модель LM Studio
# мультичат · прогресс шагов через WS · телеметрия (Вт) · ИСХОДНИКИ (фон + персонаж + текст)
import asyncio, base64, contextlib, ctypes, glob, io, json, os, platform, random, re, shutil, subprocess, time, uuid
import aiohttp
from aiohttp import web

try:
    from PIL import Image, ImageChops
except Exception:
    Image = ImageChops = None

# ===== ПУТИ И БАЗА =====
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MEM_FILE   = os.path.join(BASE_DIR, "memory.json")
CFG_FILE   = os.path.join(BASE_DIR, "config.json")
LLM_DEBUG  = os.path.join(BASE_DIR, "llm_debug.json")
CHATS_FILE = os.path.join(BASE_DIR, "chats.json")
CHAT_FILE  = os.path.join(BASE_DIR, "chat_history.json")   # только для миграции
LAST_FILE  = os.path.join(BASE_DIR, "last_shot.json")      # только для миграции
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")             # исходники пользователя
os.makedirs(UPLOAD_DIR, exist_ok=True)

LM_URL     = "http://127.0.0.1:1234"
COMFY_URL  = "http://127.0.0.1:8188"
WEB_PORT   = 7860
MIN_TOKENS = 3072
HIST_TURNS = 12
CLIP_LEN   = 1500

REF_ROLES = ("фон", "персонаж", "референс")
REF_CACHE = {}            # (file, role) -> описание VLM
SDXL_BUCKETS = [(1216, 832), (832, 1216), (1344, 768), (768, 1344), (1536, 640),
                (1024, 1024), (1152, 896), (896, 1152), (1280, 768), (768, 1280)]

IDEA_FILE = os.path.join(BASE_DIR, "ideas.json")
DEFAULT_IDEAS = [
    "зеленоволосая кошечка-библиотекарь в очках и вязаном жилете тянется к верхней полке старой библиотеки, вокруг лестница и стопки фолиантов, тёплый закатный свет из витражного окна, средний план",
    "девушка-механик с медным каре и веснушками лежит под капотом маслкара в гараже, верстак с инструментами и канистры вокруг, оранжевый закат через ворота, низкий ракурс",
    "белая фурри-волчица в меховой накидке сидит у костра на заснеженной поляне, искры летят к звёздному небу, вокруг тёмные ели и следы на снегу, тёплый свет огня на морде, крупный план",
    "андроид-девушка с неоновыми прядями стоит под дождём в киберпанк-переулке, вывески отражаются в лужах, пар из решёток, холодный сине-розовый свет, взгляд в камеру, поясной план",
]
CACHED_IDEAS = None

# ===== ИДЕИ: приземлённый генератор =====
IDEA_TEMP = 0.55            # было 0.9 — главная причина бреда
IDEA_ASK  = 12              # просим с запасом, чтобы было что отсеять
IDEA_KEEP = 8
IDEA_MODE = "реализм"       # реализм | аниме | фантастика
IDEA_FORCE_LOCAL = False    # True = вообще без LLM, мгновенно и без галлюцинаций
IDEA_SEEN_FILE = os.path.join(BASE_DIR, "ideas_seen.json")

IDEA_MODES = {
    "реализм": {
        "line": "Обычные люди в правдоподобных бытовых местах. Никакой магии, монстров, фурри, "
                "роботов, эльфов, киберпанка и неона. Одежда и реквизит — из повседневной жизни.",
        "ban": ["фурри", "антро", "монстр", "дракон", "эльф", "демон", "робот", "андроид",
                "киберпанк", "неон", "маги", "волшеб", "портал", "левит", "инопланет",
                "зомби", "вампир", "крыль", "нимб", "парящ", "летающ", "гигантск",
                "кошечк", "волчиц", "единорог", "русалк", "гном", "орк", "гоблин",
                "призрак", "постапокал", "стимпанк", "супергерой", "колдов", "заклинани", "телепорт"],
    },
    "аниме": {
        "line": "Аниме-повседневность. Допустима лёгкая стилизация (кошачьи ушки, необычный цвет волос, "
                "большие глаза), но сцена бытовая: учёба, работа, дом, улица. Без боёв, магии и монстров.",
        "ban": ["монстр", "дракон", "демон", "робот", "киберпанк", "портал", "левит",
                "инопланет", "зомби", "летающ", "гигантск", "постапокал", "меч", "клинк",
                "заклинани", "боев", "взрыв"],
    },
    "фантастика": {
        "line": "Можно фантастику, но одна сцена — один мир. Не смешивай киберпанк с фэнтези, "
                "а магию с бытовой техникой. Физика, поза, свет и одежда правдоподобны внутри этого мира.",
        "ban": [],
    },
}

# совместимые связки: персонаж + занятие + место (место уже в нужном падеже)

IDEA_SCENES = [
    # --- помещения ---
    {"kind": "in",  "who": "девушка-бариста с тёмным каре",        "act": "вытирает стойку",
     "where": "в маленькой кофейне",              "props": "кофемашина, чашки на полке, меловое меню"},
    {"kind": "in",  "who": "студентка в растянутом свитере",       "act": "пишет конспект, подперев щёку",
     "where": "в читальном зале библиотеки",      "props": "стопка книг, настольная лампа, окно"},
    {"kind": "in",  "who": "мужчина-механик в испачканной робе",   "act": "затягивает болт на снятом колесе",
     "where": "в гараже",                         "props": "верстак, ключи, домкрат"},
    {"kind": "in",  "who": "повар в фартуке",                      "act": "помешивает соус в сотейнике",
     "where": "на кухне ресторана",               "props": "плита, полка с посудой, разделочная доска"},
    {"kind": "in",  "who": "девушка-швея",                         "act": "ведёт строчку на машинке",
     "where": "в мастерской",                     "props": "рулоны ткани, манекен, ножницы"},
    {"kind": "in",  "who": "медсестра в халате",                   "act": "заполняет журнал",
     "where": "на посту в коридоре больницы",     "props": "тележка, кушетка, лампа"},
    {"kind": "in",  "who": "парень-музыкант",                      "act": "меняет струну на гитаре",
     "where": "в репетиционной комнате",          "props": "усилитель, чехол, кабели"},
    {"kind": "in",  "who": "женщина-кондитер с мукой на фартуке",  "act": "раскатывает тесто скалкой",
     "where": "в пекарне",                        "props": "противни, мешки с мукой, витрина"},
    {"kind": "in",  "who": "мужчина-гончар",                       "act": "центрует глиняную чашу на круге",
     "where": "в гончарной мастерской",           "props": "полка с горшками, ведро воды, тряпки"},
    {"kind": "in",  "who": "девушка-флорист в зелёном фартуке",    "act": "собирает букет",
     "where": "в цветочном магазине",             "props": "вёдра с цветами, ленты, секатор"},
    {"kind": "in",  "who": "программист в толстовке",              "act": "правит код, подперев подбородок",
     "where": "в офисе",                          "props": "два монитора, стикеры, кружка"},
    {"kind": "in",  "who": "женщина-библиотекарь в очках",         "act": "расставляет книги по полке",
     "where": "в фонде библиотеки",               "props": "тележка с книгами, стремянка, картотечный шкаф"},
    {"kind": "in",  "who": "мальчик лет десяти",                   "act": "собирает модель самолёта",
     "where": "на чердаке дома",                  "props": "коробка с деталями, клей, настольная лампа"},
    {"kind": "in",  "who": "мужчина-часовщик с лупой на глазу",    "act": "вскрывает корпус карманных часов",
     "where": "в часовой мастерской",             "props": "пинцеты, шестерёнки, лампа"},
    {"kind": "in",  "who": "девушка-скрипачка в чёрном платье",    "act": "настраивает колки скрипки",
     "where": "за кулисами концертного зала",     "props": "футляр, пюпитр, канифоль"},
    {"kind": "in",  "who": "женщина-врач в белом халате",          "act": "смотрит снимок на просвет",
     "where": "в ординаторской больницы",         "props": "негатоскоп, папка, стол"},
    {"kind": "in",  "who": "парень-студент в очках",               "act": "паяет плату под лампой",
     "where": "в кружковой комнате",              "props": "паяльник, мотки провода, лупа"},
    {"kind": "in",  "who": "мужчина-столяр в стружке на рубашке",  "act": "строгает доску рубанком",
     "where": "в столярном цехе",                 "props": "верстак, струбцины, пила"},
    # --- улица ---
    {"kind": "out", "who": "парень-курьер в мокрой куртке",        "act": "пристёгивает сумку к велосипеду",
     "where": "на узкой улице старого города",    "props": "витрины, бордюр, лужи"},
    {"kind": "out", "who": "женщина-садовник в перчатках",         "act": "поливает грядку из лейки",
     "where": "на дачном участке",                "props": "теплица, шланг, глиняные горшки"},
    {"kind": "out", "who": "девушка с фотоаппаратом",              "act": "ловит кадр, приложив камеру к глазу",
     "where": "на набережной",                    "props": "перила, фонари, вода"},
    {"kind": "out", "who": "рыбак в резиновых сапогах",            "act": "распутывает леску",
     "where": "на деревянном мостке у озера",     "props": "ведро, удочка, камыши"},
    {"kind": "out", "who": "парень со скейтом",                    "act": "сидит на ступенях и шнурует кеды",
     "where": "во дворе школы",                   "props": "перила, граффити, рюкзак"},
    {"kind": "out", "who": "продавщица цветов",                    "act": "подрезает стебли",
     "where": "у лотка на рынке",                 "props": "вёдра с водой, навес, ценники"},
    {"kind": "out", "who": "девушка-геолог с рюкзаком",            "act": "разглядывает камень в руке",
     "where": "на тропе по склону холма",         "props": "карта, кусты, осыпь"},
    {"kind": "out", "who": "мужчина-пасечник в шляпе с сеткой",    "act": "поднимает рамку из улья",
     "where": "на пасеке",                        "props": "ульи, дымарь, перчатки"},
    {"kind": "out", "who": "женщина-фермер в комбинезоне",         "act": "насыпает зерно в кормушку",
     "where": "на дворе фермы",                   "props": "куры, ведро, забор"},
    {"kind": "out", "who": "почтальон с сумкой через плечо",       "act": "опускает письмо в ящик",
     "where": "на тихой улице посёлка",           "props": "почтовый ящик, калитка, сирень"},
    {"kind": "out", "who": "девушка-велосипедистка в шлеме",       "act": "поправляет перчатку у рамы",
     "where": "на обочине загородной дороги",     "props": "велосипед, насос, бутылка воды"},
    {"kind": "out", "who": "женщина-художница с этюдником",        "act": "смешивает краску на палитре",
     "where": "на пленэре в городском парке",     "props": "этюдник, складной стул, тюбики"},
    {"kind": "out", "who": "мальчик с сачком",                     "act": "ловит бабочку у клумбы",
     "where": "в городском парке",                "props": "сачок, клумба, фонтан"},
    {"kind": "out", "who": "девушка-туристка с треккинговыми палками", "act": "смотрит на указатель троп",
     "where": "на горной тропе",                  "props": "указатель, рюкзак, туман в долине"},
    {"kind": "out", "who": "мужчина-дворник в оранжевом жилете",   "act": "сгребает листья в кучу",
     "where": "у подъезда панельного дома",       "props": "метла, мешок, скамейка"},
    {"kind": "out", "who": "девушка-конюх в резиновых сапогах",    "act": "ведёт лошадь под уздцы",
     "where": "у конюшни",                        "props": "сбруя, ведро, ограда"},
]

IDEA_LIGHT = {
    "in": [("утром",            "мягкий рассеянный свет из окна"),
           ("днём",             "ровный дневной свет из окна, лёгкие тени"),
           ("вечером",          "тёплый свет настольной лампы, окно уже тёмное"),
           ("ночью",            "одна лампа, остальное в тени"),
           ("в пасмурный день", "приглушённый свет из окна, без резких теней"),
           ("поздним вечером",  "торшер у кресла, полумрак по углам")],
    "out": [("ранним утром",    "низкое солнце, длинные тени, лёгкая дымка"),
            ("днём",            "яркое солнце, короткие тени"),
            ("на закате",       "тёплый контровой свет, оранжевое небо"),
            ("в пасмурный день","ровный рассеянный свет, мягкие тени"),
            ("после дождя",     "мокрый асфальт, отражения, облачное небо"),
            ("вечером",         "фонари и витрины, синие сумерки"),
            ("в снегопад",      "белый рассеянный свет, следы на снегу")],
}

IDEA_SHOT = ["крупный план по грудь", "поясной план", "средний план в три четверти",
             "план по колени", "общий план, человек в полный рост",
             "крупный план лица и рук", "вид сбоку, поясной план"]

IDEA_MOOD = ["спокойствие", "сосредоточенность", "лёгкая усталость", "задумчивость",
             "едва заметная улыбка", "увлечённость делом", "умиротворение",
             "лёгкая озабоченность", "тихая радость"]

def build_idea_local():
    """Осмысленная сцена без участия LLM — всегда согласована."""
    sc = random.choice(IDEA_SCENES)
    t, light = random.choice(IDEA_LIGHT[sc["kind"]])
    return (f"{sc['who']} {sc['act']} {sc['where']}, {t}. "
            f"Рядом {sc['props']}. {light.capitalize()}, "
            f"настроение — {random.choice(IDEA_MOOD)}, {random.choice(IDEA_SHOT)}.")

IDEA_T = (
    "Ты подбираешь ОДНУ правдоподобную сцену для аниме-картинки. "
    "Миры не сочиняй — собирай сцену из готовых элементов.\n"
    "РЕЖИМ: {mode_line}\n\n"
    "ВЫБЕРИ персонажа с занятием и местом из списка (или очень близкий бытовой аналог):\n{who}\n\n"
    "Время и свет: утром / днём / на закате / вечером / ночью / в пасмурный день / после дождя.\n"
    "План: крупный по грудь / поясной / средний в три четверти / по колени / общий в полный рост.\n\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1) Один человек, одно понятное действие, одно место. Без «и ещё», «а также», без второго персонажа.\n"
    "2) Занятие возможно в этом месте и в этой одежде: рыбак — у воды, повар — на кухне.\n"
    "3) Свет один и согласован со временем: ночью нет заката, в пасмурный день нет резких теней, "
    "в помещении свет из окна или от лампы.\n"
    "4) Погода и одежда согласованы: дождь — куртка, снег — тёплая одежда.\n"
    "5) Поза физически возможна, руки заняты одним делом. «Сидит и стоит» — запрещено.\n"
    "6) 30-50 слов, 2 предложения, живой русский, без списков и без тегов.\n"
    "7) ЗАПРЕЩЕНО: {ban}. Также запрещены: 8k, hdr, masterpiece, danbooru, лоры, "
    "английские термины камеры, слова «шедевр», «идеальная композиция».\n"
    "8) Не повторяй прошлые идеи и не меняй только цвет волос — меняй занятие и место.\n\n"
    "ПРИМЕРЫ ФОРМАТА (содержание не копируй):\n{ex}\n\n"
    "УЖЕ БЫЛО (не повторяй):\n{hist}\n\n"
    'Ответь ТОЛЬКО валидным JSON-объектом {{"ideas": [...]}} из {n} строк, без пояснений и markdown. '
)

def idea_prompt(mode, seen):
    prof = IDEA_MODES.get(mode) or IDEA_MODES["реализм"]
    who = "\n".join(f"- {s['who']} — {s['act']} — {s['where']}" for s in IDEA_SCENES)
    ex = "\n".join("- " + build_idea_local() for _ in range(3))
    hist = "\n".join("- " + x for x in (seen or [])[-10:]) or "—"
    return IDEA_T.format(mode_line=prof["line"], who=who, ex=ex, hist=hist,
                         ban=", ".join(prof["ban"]) or "—", n=IDEA_ASK)

# --- фильтр осмысленности ---

IDEA_BAD = ("8k", "hdr", "masterpiece", "best quality", "danbooru", "лора", "промпт",
            "тег", "шедевр", "идеальная композиция", "trending", "award",
            "рендер", "шейдинг", "постобработк", "фотошоп", "midjourney",
            "stable diffusion", "sdxl", "novelai", "civitai", "pixiv", "artstation",
            "чекпоинт", "апскейл", "сэмплер")
IDEA_PERSON = ("девушк", "парен", "парн", "женщин", "мужчин", "девочк", "мальчик",
               "бариста", "механик", "студент", "повар", "рыбак", "садовник", "швея",
               "медсестр", "продав", "музыкант", "геолог", "курьер", "фотограф",
               "мастер", "учител", "водител", "тренер", "художниц", "художник",
               "кондитер", "гончар", "флорист", "программист", "библиотекар",
               "часовщик", "врач", "столяр", "пасечник", "фермер", "почтальон",
               "дворник", "конюх", "скрипач", "турист", "велосипедист")

IDEA_PLACE = ("кофейн", "улиц", "библиотек", "участк", "гараж", "набережн", "кухн",
              "мосток", "озер", "мастерск", "двор", "коридор", "больниц", "рынок",
              "комнат", "троп", "холм", "парк", "магазин", "вокзал", "поезд", "автобус",
              "балкон", "крыш", "переулок", "лес", "пляж", "поле", "склад", "цех",
              "офис", "класс", "столов", "спальн", "подъезд", "остановк", "ступен",
              "пекарн", "чердак", "зал", "пасек", "ферм", "дорог", "конюшн")

IDEA_CONTRA = [
    (("ночь", "ночью", "ночн"), ("закат", "полдень", "солнечн", "яркое солнце", "рассвет")),
    (("дожд", "ливень", "мокр"), ("ясное небо", "безоблачн", "сухая пыль")),
    # "стоит" убрано: "в комнате стоит лампа" + сидящий персонаж — ложный брак
    (("сидит", "сидя"), ("стоя", "бежит", "идет", "идёт")),
    (("в помещении", "в комнате", "на кухне", "в гараже", "в мастерской"),
     ("звёздн", "звездн", "лунный свет")),
    (("зима", "снег", "мороз"), ("купальник", "загорает", "жара")),
    (("пасмурн", "облачн"), ("яркое солнце", "резкие тени", "безоблачн")),
    (("крупный план", "по грудь"), ("в полный рост", "общий план")),
]

def _norm_idea(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = re.sub(r"^(?:\d+[.)\-]|[-•*])\s*", "", s)
    return s.strip(" \"'«»")

def _has(t, stems):
    return any(re.search(r"\b" + re.escape(w), t) for w in stems)

def _words(s):
    return set(re.findall(r"[а-яёa-z]{3,}", s.lower()))

def idea_ok(s, mode, taken):
    t = str(s or "").lower()
    n = len(t.split())
    if n < 18 or n > 65:
        return False
    if len(re.findall(r"[a-z]", t)) > len(re.findall(r"[а-яё]", t)) * 0.25:
        return False
    if not _has(t, IDEA_PERSON) or not _has(t, IDEA_PLACE):
        return False
    if _has(t, IDEA_BAD) or _has(t, (IDEA_MODES.get(mode) or IDEA_MODES["реализм"])["ban"]):
        return False
    for a, b in IDEA_CONTRA:
        if _has(t, a) and _has(t, b):
            return False
    stems = [w[:5] for w in re.findall(r"[а-яё]{4,}", t)]
    if stems and max(stems.count(x) for x in set(stems)) > 3:
        return False
    cur = _words(t)
    for prev in taken:
        old = _words(str(prev))
        if cur and old and len(cur & old) / float(len(cur | old)) > 0.55:
            return False
    return True

def load_seen():
    try:
        d = json.load(open(IDEA_SEEN_FILE, encoding="utf-8"))
        return [str(x) for x in d][-60:] if isinstance(d, list) else []
    except Exception:
        return []

def save_seen(lst):
    try:
        json.dump(lst[-60:], open(IDEA_SEEN_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception as e:
        print("[ideas] история не сохранилась:", e)

def load_ideas():
    global CACHED_IDEAS
    if CACHED_IDEAS is not None:
        return CACHED_IDEAS
    if os.path.isfile(IDEA_FILE):
        try:
            d = json.load(open(IDEA_FILE, encoding="utf-8"))
            if isinstance(d, list) and len(d) >= 4:
                CACHED_IDEAS = [str(x).strip() for x in d if str(x).strip()][:12]
                return CACHED_IDEAS
        except Exception:
            pass
    return None

def save_ideas(ideas):
    try:
        json.dump(ideas, open(IDEA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        print("[ideas] не сохранилось:", e)

async def generate_ideas(mode=None):
    """Просит сцены у LLM, фильтрует бред, добирает конструктором."""
    global CACHED_IDEAS
    mode = mode if mode in IDEA_MODES else IDEA_MODE
    seen = load_seen()
    out = []
    if not IDEA_FORCE_LOCAL:
        try:
            with contextlib.nullcontext(HTTP) as s:
                txt = await llm(s, [
                    {"role": "system",
                     "content": 'Ты подборщик правдоподобных сцен. Отвечай только валидным JSON-объектом {"ideas": ["...", ...]}.'},
                    {"role": "user", "content": idea_prompt(mode, seen)}
                ], IDEA_TEMP, 1600, True, schema=S_IDEAS, timeout=300)
                raw = parse_json(txt)
                if isinstance(raw, dict):
                    raw = raw.get("ideas") or raw.get("list") or []
                cand = [_norm_idea(x) for x in raw if isinstance(x, str)]
                for c in cand:
                    if len(out) >= IDEA_KEEP:
                        break
                    if idea_ok(c, mode, out + seen[-12:]):
                        out.append(c)
                print(f"[ideas] {mode}: модель дала {len(cand)}, прошло фильтр {len(out)}")
        except Exception as e:
            print("[ideas] генерация не удалась:", e)
    tries = 0
    while len(out) < IDEA_KEEP and tries < 300:
        tries += 1
        c = build_idea_local()
        if idea_ok(c, mode, out + seen[-12:]):
            out.append(c)
    if not out:
        out = list(DEFAULT_IDEAS[:IDEA_KEEP])
    random.shuffle(out)
    CACHED_IDEAS = out
    save_ideas(out)
    save_seen(seen + out)
    print(f"[ideas] итог {len(out)} шт, режим {mode}")
    return out

def get_ideas():
    """Синхронный getter для HTML — возвращает кэш или дефолт."""
    return load_ideas() or DEFAULT_IDEAS

RESOLUTIONS = {
    "1216x832 (3:2 альбом, SDXL-натив)":  (1216, 832),
    "832x1216 (2:3 портрет, SDXL-натив)": (832, 1216),
    "1344x768 (16:9, SDXL-натив)":        (1344, 768),
    "768x1344 (9:16, SDXL-натив)":        (768, 1344),
    "1536x640 (2.4:1 кино, SDXL-натив)":  (1536, 640),
    "1024x1024 (квадрат)":                (1024, 1024),
    "1408x704 (2:1 лежачий, текущий)":    (1408, 704),
    "704x1408 (2:1 стоячий)":             (704, 1408),
    "1280x720 (HD)":                      (1280, 720),
    "1600x1336 (как в ручном)":           (1600, 1336),
    "1920x1080 (FullHD)":                 (1920, 1080),
    "1024x1536 (2:3 портрет)":            (1024, 1536),
    "1536x1024 (3:2 альбом)":             (1536, 1024),
}

DEFAULT_CONFIG = {
    "output_dir": r"E:\ComfyUI_windows_portable\ComfyUI\output",
    "ckpt": "Diving-Illustrious Flat Anime Paradigm Shift_v8.0+VAE.safetensors",
    "resolution": "1408x704 (2:1 лежачий, текущий)",
    "fw_fh": (2000, 1000),
    "steps": 45, "cfg": 5.5, "max_images": 3,
    "use_critic": True,
    "upscaler": "4x-AnimeSharp.pth",
    "save_dir": "",
    "feedback_on": True,
    "sampler": "euler_ancestral", "scheduler": "normal", "clip_skip": 2,
    "quality_on": True,
    "hires_on": True, "hires_scale": 1.5, "hires_denoise": 0.4,
    "hires_steps": 20, "hires_cfg": 5.0,
    "hires_sampler": "dpmpp_2m_sde", "hires_scheduler": "karras",
    "hires_max_px": 2600000,
    # --- исходники ---
    "ref_mode": "auto",          # auto | tags | img2img | composite
    "ref_denoise": 0.55,
    "ref_auto_size": True,
    "ref_cutout": False,
    "ref_ipadapter": False,
    "ref_ipa_weight": 0.7,
    "loras": [
        {"name": "rendering_detailer_base10-000400.safetensors",      "strength": 0.8, "on": True},
        {"name": "sexy_details_v5.safetensors",                       "strength": 0.6, "on": True},
        {"name": "ATRex_style-12V2Rev.safetensors",                   "strength": 0.5, "on": True},
        {"name": "omi-s2-illustriousxl-lora-nochekaiser.safetensors", "strength": 0.6, "on": True},
        {"name": "anima-highres-aesthetic-boost.safetensors",         "strength": 0.7, "on": True},
        {"name": "", "strength": 0.8, "on": False},
        {"name": "", "strength": 0.8, "on": False},
        {"name": "", "strength": 0.8, "on": False},
        {"name": "", "strength": 0.8, "on": False},
        {"name": "", "strength": 0.8, "on": False},
    ],
}

def load_config():
    if os.path.isfile(CFG_FILE):
        try:
            c = json.load(open(CFG_FILE, encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG); merged.update(c)
            for dead in ("vision_backend", "solo"):   # legacy: критик всегда LM Studio
                merged.pop(dead, None)
            if len(merged.get("loras", [])) < 10:
                merged["loras"] = DEFAULT_CONFIG["loras"]
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(c):
    json.dump(c, open(CFG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

CONFIG = load_config()

# ===== МУЛЬТИЧАТ =====

CHATS = {}                      # id -> сессия чата
PID_MAP = {}                    # prompt_id ComfyUI -> {chat,item,steps1,steps2,epoch,node}
WS_CLIENT = "heretic-" + uuid.uuid4().hex[:8]

def make_chat(title=None):
    cid = uuid.uuid4().hex[:10]
    ch = {"id": cid, "title": title or ("Чат " + str(len(CHATS) + 1)),
          "created": time.time(),
          "messages": [], "items": [], "turns": [],
          "last": {"theme": "", "shots": [], "ts": 0},
          "dialog": {"stage": "idle", "theme": "", "history": [], "refs": []},
          "busy": False, "cancel": False, "progress": None, "epoch": 0, "task": None}
    CHATS[cid] = ch
    return ch

def save_chats():
    try:
        data = {}
        for cid, ch in CHATS.items():
            data[cid] = {"id": cid, "title": ch["title"], "created": ch["created"],
                         "messages": ch["messages"][-200:],
                         "items": [{k: v for k, v in it.items() if k != "progress"}
                                   for it in ch["items"][-50:]],
                         "turns": ch["turns"][-80:], "last": ch["last"]}
        json.dump(data, open(CHATS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        print("[chats] не сохранилось:", e)

def load_chats():
    if os.path.isfile(CHATS_FILE):
        try:
            d = json.load(open(CHATS_FILE, encoding="utf-8"))
            for cid, c in d.items():
                CHATS[cid] = {"id": cid, "title": c.get("title") or "Чат",
                              "created": c.get("created", time.time()),
                              "messages": c.get("messages", [])[-200:],
                              "items": [{k: v for k, v in it.items() if k != "progress"}
                                        for it in c.get("items", [])[-50:]],
                              "turns": c.get("turns", [])[-80:],
                              "last": c.get("last") or {"theme": "", "shots": [], "ts": 0},
                              "dialog": {"stage": "idle", "theme": "", "history": [], "refs": []},
                              "busy": False, "cancel": False, "progress": None, "epoch": 0, "task": None}
        except Exception as e:
            print("[chats] не загрузились:", e)
    for c in CHATS.values():
        for it in c["items"]:
            if it.get("status") not in ("готово", "брак"):
                it["status"], it["ok"] = "брак", False
    if not CHATS:
        ch = make_chat("Чат 1")
        try:  # миграция со старой одиночной памяти
            if os.path.isfile(CHAT_FILE):
                d = json.load(open(CHAT_FILE, encoding="utf-8"))
                ch["turns"] = [t for t in d.get("turns", [])
                               if t.get("role") in ("user", "assistant") and t.get("content")][-80:]
            if os.path.isfile(LAST_FILE):
                d = json.load(open(LAST_FILE, encoding="utf-8"))
                ch["last"] = {"theme": d.get("theme", ""), "shots": d.get("shots", [])[-6:],
                              "ts": d.get("ts", 0)}
        except Exception:
            pass
    save_chats()

def launch(ch, coro):
    t = asyncio.create_task(coro)
    ch["task"] = t
    def _done(task):
        if ch.get("task") is t:
            ch["task"] = None
    t.add_done_callback(_done)
    return t

def say(ch, role, text, images=None):
    m = {"role": role, "text": text}
    if images:
        m["images"] = images
    ch["messages"].append(m)
    del ch["messages"][:-200]
    save_chats()

def chat_add(ch, role, content):
    c = str(content or "").strip()
    if not c:
        return
    ch["turns"].append({"role": role, "content": c})
    del ch["turns"][:-80]
    save_chats()

def chat_msgs(ch, limit=None):
    n = limit or HIST_TURNS
    out = []
    for t in ch["turns"][-n:]:
        c = t["content"]
        out.append({"role": t["role"], "content": c if len(c) <= CLIP_LEN else c[:CLIP_LEN] + " …"})
    return out

def last_shot(ch):
    return ch["last"]["shots"][-1] if ch["last"]["shots"] else None

def clear_last(ch):
    ch["last"] = {"theme": "", "shots": [], "ts": 0}
    save_chats()

load_chats()

# ===== РАСПОЗНАВАНИЕ «ПРАВКА ИЛИ НОВАЯ ТЕМА» =====

FRAME_NUM = {"первый": 1, "первого": 1, "второй": 2, "второго": 2,
             "третий": 3, "третьего": 3, "четвёртый": 4, "четвертый": 4,
             "пятый": 5, "шестой": 6}

def pick_base(ch, text):
    shots = ch["last"]["shots"]
    if not shots:
        return {}
    t = (text or "").lower()
    n = None
    m = re.search(r"кадр\w*\s*(?:№|#)?\s*(\d+)", t)
    if m:
        n = int(m.group(1))
    else:
        for w, k in FRAME_NUM.items():
            if re.search(r"\b" + w + r"\b", t):
                n = k
                break
    if n:
        for sh in shots:
            if sh.get("idx") == n:
                return sh
    return shots[-1]

def _lat(s): return sum(c.isascii() and c.isalpha() for c in s)
def _rus(s): return sum("\u0400" <= c <= "\u04ff" for c in s)

def looks_like_prompt(t):
    t = str(t or "")
    return t.count(",") >= 6 and _lat(t) > _rus(t) * 2 and _lat(t) > 40

def extract_prompt(text):
    t = str(text or "").strip()
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    tags = [l for l in lines if l.count(",") >= 3 and _lat(l) > _rus(l)]
    if tags:
        return ", ".join(tags).strip(" ,")
    parts = re.split(r"\n+|(?<=[.!?])\s+|:\s+", t)
    best = max(parts, key=lambda p: p.count(",")) if parts else ""
    if looks_like_prompt(best):
        return best.strip(" ,")
    if looks_like_prompt(t):
        return t.strip(" ,")
    return ""

INTENT_SYS = (
    "Ты классификатор намерений в арт-чате. Верни ТОЛЬКО JSON:\n"
    '{"no_questions":false,"freeform":false,"new_topic":false,"edit":false,"raw_prompt":null}\n'
    "no_questions=true — человек просит генерировать без уточняющих вопросов. Понимай СМЫСЛ, а не слова: "
    "«без вопросов», «не спрашивай ничего», «давай сразу», «рисуй как понял», «без анкет», "
    "«не надо интервью», «пропусти опрос», «не уточняй», «делай на своё усмотрение и рисуй».\n"
    "freeform=true — тему отдаёт на твой выбор: «придумай сам», «удиви», «что-нибудь», "
    "«на твой вкус», «любой сюжет», «выбери сам».\n"
    "new_topic=true — явно просит забыть прошлое и начать заново.\n"
    "edit=true — просит изменить уже готовый кадр (убери/добавь/замени/сделай ночь/крупнее).\n"
    "raw_prompt — если в сообщении готовый промпт английскими тегами «как есть», верни его строкой, иначе null.\n"
    "Не выдумывай намерение. Вопрос пользователя к тебе («какой цвет лучше?») — это НЕ no_questions.\n"
    "Примеры:\n"
    "«сделай без вопросов, сама реши» → no_questions=true\n"
    "«нарисуй что-нибудь сам, удиви» → freeform=true, no_questions=true\n"
    "«а можно без уточнений?» → no_questions=true\n"
    "«убери очки» → edit=true\n"
    "«начни заново, другая тема» → new_topic=true\n"
    "«какой цвет волос лучше?» → все false/null"
)

async def classify_intent(s, text, has_last):
    msgs = [{"role": "system", "content": INTENT_SYS},
            {"role": "user", "content":
                f"[контекст: готовый кадр в чате: {'да' if has_last else 'нет'}]\n{text}"}]
    v = parse_json(await llm(s, msgs, 0.1, 400, True, schema=S_INTENT, timeout=45))
    if not isinstance(v, dict):
        raise RuntimeError("классификатор вернул не объект")
    rp = v.get("raw_prompt")
    return {"no_questions": bool(v.get("no_questions")),
            "freeform":     bool(v.get("freeform")),
            "new_topic":    bool(v.get("new_topic")),
            "edit":         bool(v.get("edit")),
            "raw_prompt":   str(rp).strip() if isinstance(rp, str) and rp.strip() else None}

# ===== ПРОМПТЫ =====
QUALITY = ("masterpiece, best quality, very aesthetic, newest, absurdres, "
           "beautiful detailed eyes, detailed face, detailed hair, detailed fabric texture, "
           "clean lines, crisp lineart, anime coloring, cel shading, sharp focus")

NEGATIVE = ("multiple people, two people, three people, multiple girls, 2girls, 3girls, multiple boys, 2boys, "
            "crowd, group of people, extra person, background people, clone, duplicate character, twins, "
            "conjoined twins, two heads, multiple heads, extra head, missing head, headless, fused heads, "
            "floating head, two faces, extra face, multiple faces, deformed face, ugly face, poorly drawn face, "
            "asymmetrical face, distorted facial features, poorly drawn eyes, asymmetric eyes, uneven eyes, "
            "cross-eyed, extra eyes, third eye, missing eyes, deformed iris, deformed pupils, extra ears, "
            "missing ears, deformed ears, extra nose, missing nose, deformed nose, extra mouth, missing mouth, "
            "deformed mouth, deformed lips, bad teeth, extra teeth, missing teeth, long neck, twisted neck, "
            "(bad anatomy:1.2), bad proportions, gross proportions, anatomical nonsense, mutated, mutation, "
            "deformed body, disfigured, malformed body, malformed limbs, (extra limbs:1.2), missing limbs, "
            "disconnected limbs, floating limbs, long body, elongated torso, missing arms, extra arms, "
            "third arm, multiple arms, fused arms, poorly drawn arms, missing legs, extra legs, third leg, "
            "multiple legs, fused legs, poorly drawn legs, twisted legs, backwards legs, backwards limbs, "
            "backwards hands, extra joints, backwards joints, broken wrist, twisted wrist, missing hands, "
            "extra hands, third hand, multiple hands, fused hands, poorly drawn hands, mutated hands, "
            "(bad hands:1.2), missing fingers, (extra fingers:1.3), six fingers, too many fingers, "
            "(fused fingers:1.3), fewer fingers, malformed fingers, long fingers, extra thumb, missing thumb, "
            "extra digits, fewer digits, missing toes, extra toes, fused toes, bad feet, poorly drawn feet, "
            "extra feet, missing feet, realistic, photorealistic, photo, photograph, live action, 3d, cgi, "
            "3d render, western cartoon")

SYS = (
    "Ты — арт-директор генерации аниме-картинок на Illustrious XL (SDXL).\n"
    "Пользователь даёт тему. Придумай серию сцен-вариаций и для каждой напиши промпт ТЕГАМИ.\n"
    'Отвечай ТОЛЬКО валидным JSON: {"plan":"1-2 предложения по-русски","images":[{"prompt":"...","seed":null}]}\n'
    "\nЖЁСТКИЕ ПРАВИЛА поля prompt:\n"
    "1) Только английские danbooru-теги через запятую, нижний регистр, пробелы вместо подчёркиваний, одна строка.\n"
    "2) РОВНО 80-150 тегов. Меньше 80 — мало детали. Больше 150 — CLIP размывает внимание, картинка мылится.\n"
    "3) Никаких русских слов, предложений, пояснений, markdown, кавычек внутри строки.\n"
    "4) НЕ пиши masterpiece, best quality, very aesthetic, newest, absurdres, anime coloring, cel shading — они добавляются автоматически.\n"
    "5) НЕ пиши выдуманные и мета-теги: 8k, uhd, hdr, award-winning, trending on pixiv, professional, "
    "perfect composition, noise-free, artifact-free, ultra-detailed, intricate details. Это мусор, он не работает.\n"
    "6) Вес (tag:1.2) разрешён максимум на 3 тегах и не выше 1.3.\n"
    "7) Никаких отрицаний (no ..., without ...) — им место в negative.\n"
    "8) Никаких противоречий: nude вместе с clothes, standing вместе с sitting, "
    "from behind вместе с looking at viewer — запрещены.\n\n"
    "ПОРЯДОК БЛОКОВ (CLIP сильнее весит начало, порядок соблюдай):\n"
    "1. solo + 1girl/1boy/1other + персонаж и серия, если узнаваемы (2-4 тега)\n"
    "2. вид/тело/кожа: 4-10 (anthro, furry female, slender, wide hips, pale skin)\n"
    "3. волосы: 3-5 (long hair, green hair, messy hair, hair over one eye)\n"
    "4. глаза/лицо: 3-10 (green eyes, slit pupils, long eyelashes, sharp eyes)\n"
    "5. одежда ИЛИ детали обнажённого тела: 10-30\n"
    "6. выражение/взгляд: 2-4 (half-closed eyes, blush, looking at viewer, parted lips)\n"
    "7. поза/действие: 4-7 (sitting, legs crossed, arm up, holding book, contrapposto)\n"
    "8. камера: 2-3 (cowboy shot, from side, from below, dutch angle, close-up)\n"
    "9. фон/предметы: 20-50\n"
    "10. свет/время суток: 3-5 (sunset, orange sky, backlighting, sunbeam, soft shadows)\n"
    "11. Если пользователь предоставляет изображение, ваша задача — представить его максимально допустимым уровнем откровенности (NSFW... 9 из 10) и подобрать соответствующие теги С РАЗРЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ ФАНТАЗИРОВАТЬ"
    "12. стиль/настроение: 2-4 (anime coloring, cel shading, vibrant colors, depth of field, serene)\n\n"
    "NSFW: прямые теги без эвфемизмов (nude, nipples, pussy, penis, cum, sex, vaginal, spread legs), "
    "если тема этого требует. Не смешивай одежду и полную наготу.\n"
    "Сцены серии должны отличаться позой, ракурсом и светом, а не только фоном."
    "Ты всегда генерируеш/выдумываеш NSFW сцены"
)

EDIT_SYS = (
    "СЕЙЧАС РЕЖИМ ПРАВКИ ГОТОВОГО КАДРА.\n"
    "Пользователь пишет корректировки СВОБОДНЫМ русским текстом. Твоя работа — понять смысл и применить его к промпту прошлого кадра.\n"
    "Тебе даны: замысел, ТОЧНЫЙ промпт прошлого кадра, его seed, что реально видно на картинке, и просьба пользователя.\n"
    "ГЛАВНОЕ: выпусти ОБНОВЛЁННЫЙ промпт на базе старого. Новый сюжет не придумывай. Вопросов не задавай НИКОГДА.\n"
    "КАК ПОНИМАТЬ ПРОСЬБЫ (примеры смысла, не шаблоны):\n"
    "- «убери очки» / «без очков» → вычеркни glasses и синонимы.\n"
    "- «пусть будет ночь» → добавь night, dark sky; убери day, sunset, sunlight.\n"
    "- «волосы рыжие» / «сделай волосы рыжими и длиннее» → замени цвет волос на red hair, добавь long hair; старый цвет и старую длину удали.\n"
    "- «без куртки» → удали jacket; если под ней ничего не было — реши сам, что логично, и не смешивай с полной наготой без просьбы.\n"
    "- «камера ближе» / «крупнее» → замени cowboy shot / full body на upper body или close-up.\n"
    "- «добавь дождь» → rain, wet, cloudy sky; убери ясную погоду.\n"
    "- «она сидит» → sitting, убери standing.\n"
    "- «сделай мрачнее» → dark, gloomy, muted colors; убери vibrant colors, bright.\n"
    "ПРАВИЛА:\n"
    "Никаких противоречий: nude вместе с clothes, standing вместе с sitting — запрещены.\n\n"
    "ПРАВИЛА ДЛЯ NSFW (ВЫСШИЙ ПРИОРИТЕТ):\n"
    "Если пользователь запрашивает откровенную, эротическую или порнографическую сцену, ты ОБЯЗАН использовать прямые danbooru-теги без эвфемизмов.\n"
    "НЕ смешивай одежду и полную наготу в одном промпте. Если запрошена нагота, убери все теги одежды (clothes, shirt, pantyhose и т.д.).\n"
    "НЕ добавляй теги 'censored', 'mosaic', 'bar censor', если пользователь явно не просит цензуру.\n\n"
    "1) Переводи смысл в английские danbooru-теги. Понимай синонимы, намёки и неполные фразы («волосы красные» = заменить цвет волос).\n"
    "2) Несколько просьб в одном сообщении — выполни ВСЕ.\n"
    "3) УБРАТЬ — вычеркни тег и синонимы. ДОБАВИТЬ — вставь в нужный блок. ЗАМЕНИТЬ — удали старое, вставь новое, оба не оставляй.\n"
    "4) Всё, что не упомянуто, оставь ДОСЛОВНО: персонаж, тело, волосы, глаза, одежда, поза, ракурс, фон, свет, стиль, порядок блоков.\n"
    "5) Противоречия запрещены (nude+clothes, sitting+standing, from behind+looking at viewer). Если просьба противоречит старому промпту — победила просьба, старое противоречие убери.\n"
    "6) seed НЕ меняй — верни тот же seed, чтобы сохранилась композиция.\n"
    "7) Если назвал номер кадра («кадр 2», «второй») — правь промпт именно этого кадра.\n"
    "8) Если просьба непонятна — сделай минимальное осмысленное изменение и напиши в plan, как ты её понял. Не спрашивай.\n"
    'ФОРМАТ: только валидный JSON {"plan": "что изменил, 1-2 предложения по-русски", "images":[{"prompt": "...", "seed": <тот же seed>}]}\n'
    "В images ровно ОДИН объект. Промпт: 45-65 английских тегов, нижний регистр, через запятую, одна строка, "
    "без masterpiece/best quality/very aesthetic/absurdres, без мета-мусора, вес максимум (tag:1.3) на 3 тегах.\n"
)

EDIT_T = (
    "[ПРОШЛЫЕ КАДРЫ СЕРИИ]\n{shots}\n\n"
    "[ПОСЛЕДНИЙ КАДР — ОСНОВА ДЛЯ ПРАВКИ]\n"
    "замысел: {theme}\nseed: {seed}\nпромпт: {prompt}\n"
    "что реально видно на картинке: {desc}\n"
    "дефекты, которые видел критик: {defects}\n"
    "вердикт и прошлая оценка: {verdict}\n\n"
    "[ПРОСЬБА ПОЛЬЗОВАТЕЛЯ]\n{request}\n\n"
    "Выдай обновлённый промпт по правилам режима правки. Только JSON."
)

FIX_T = ("Промпт аниме-генерации: {prompt}\nКритик нашёл дефекты: {defects}\n"
         "Перепиши теги, чтобы убрать дефекты. Сохрани тему и стиль. "
         'Ответь только JSON: {{"prompt": "..."}}')
SUM_T = ("Старое саммари работы: {old}\nНовые генерации: {new}\n"
         "Обнови саммари (до 300 токенов, по-русски): какие темы рисовали, что получалось, какие дефекты повторялись. "
         "Ответь только текстом саммари.")
CRIT_T = (
    "Ты строгий зрительный критик аниме-изображений.\n"
    "ЗАМЫСЕЛ: {theme}\nПРОМПТ: {prompt}\n"
    "Описывай ТОЛЬКО то, что реально видно на картинке. Не выдумывай детали.\n"
    "ВАЖНО: Если в промпте запрошена нагота или эротика (nude, sex и т.д.), это НЕ является дефектом. Оценивай только качество отрисовки анатомии в этих позах.\n"
    "Проверь: 1) анатомия — сколько персонажей, одна ли голова, по две руки и ноги, по 5 пальцев, корректность поз;\n"
    "2) соответствие замыслу и тегам (включая NSFW-теги);\n"
    "3) стиль flat anime, не фотореализм;\n"
    "4) композиция, свет, фон.\n"
    'Ответь ТОЛЬКО JSON:\n'
    '{{"ok":true, "defects":[], "comment":"кратко по-русски", '
    '"description":"подробно по-русски: кто изображён, волосы, глаза, одежда или нагота, поза, '
    'ракурс, фон, свет, стиль, качество линий и мелких деталей"}}'
)

ASK_SYS = (
    "Ты помогаешь человеку придумать картинку. Он дал тему.\n"
    "СЕЙЧАС промпт писать НЕЛЬЗЯ. Задай от 10 до 30 уточняющих вопросов по его теме.\n"
    "ЯЗЫК: максимально простой, бытовой, для новичка. Вопрос короткий, до 8 слов.\n"
    "ЗАПРЕЩЁННЫЕ слова: ракурс, кадрирование, композиция, промпт, теги, лора, рендер, шейдинг, "
    "контровой свет, глубина резкости, анатомия, референс, палитра, данbooru.\n"
    "К КАЖДОМУ вопросу дай от 5 до 7 готовых вариантов ответа. Последний всегда «свой вариант».\n"
    "Варианты конкретные, 1-4 слова, без пояснений и без скобок.\n"
    "Темы вопросов: кто на картинке, пол, возраст, телосложение, волосы, глаза, одежда или нагота, "
    "что делает, поза, где находится, что вокруг, время суток, погода, освещение, настроение, "
    "главные цвета, насколько крупно показан человек, стиль рисовки, мелкие предметы, степень откровенности.\n"
    "Не спрашивай о том, что человек уже написал, и о настройках генерации.\n"
    "Ответь ТОЛЬКО валидным JSON без пояснений, строго такого вида:\n"
    '{"questions":[{"q": "кто на картинке?", "options": ["девушка", "парень", "фурри", "робот", "животное", "свой вариант"]}]}\n'
    "Всегда спрашиваешь: сколько людей, пол: мужской/женский, рост, телосложение, задний фон/место/локация, "
    "насколько много NSFW тегов надо использовать/насколько сцена постельная/сексуальный характер, одежда."
)

FB_T = ("Ты арт-директор. Ты сам придумал промпт, картинка сгенерирована.\n"
        "ЗАМЫСЕЛ: {theme}\nТВОЙ ПРОМПТ: {prompt}\n"
        "ЧТО РЕАЛЬНО ВИДНО НА КАРТИНКЕ (описание зрительной модели): {desc}\n"
        "ЗАМЕЧЕННЫЕ ДЕФЕКТЫ: {defects}\n\n"
        "Дай честную оценку по-русски, 5-8 строк, без воды и без похвалы ради похвалы:\n"
        "1) удалась ли картинка и соответствует ли замыслу — да / частично / нет\n"
        "2) что получилось хорошо, конкретно\n"
        "3) что не так, конкретно\n"
        "4) какие 3-6 тегов добавить или убрать в следующий раз\n"
        "Опирайся ТОЛЬКО на описание выше, не выдумывай детали. Обычный текст, без JSON и markdown.")

# --- промпты для разбора исходников ---

REF_T = (
    "Это ИСХОДНИК для генерации аниме-картинки. Роль картинки: {role}.\n"
    "Опиши ТОЛЬКО то, что реально видно, без домыслов.\n"
    "Персонаж: пол, возраст на вид, телосложение, волосы (длина/цвет/причёска), глаза (цвет/форма), "
    "кожа, одежда по слоям, обувь, аксессуары, поза, ракурс, выражение лица.\n"
    "Фон/локация: что за место, 5-10 конкретных объектов, материалы, время суток, погода, "
    "источники и направление света, перспектива, палитра.\n"
    "Стиль-референс: манера рисовки, линии, покраска, палитра, детализация.\n"
    "Однотонный белый или прозрачный фон вокруг персонажа — это вырезанный фон, НЕ часть сцены.\n"
    "Ответь ТОЛЬКО JSON:\n"
    '{{"description":"по-русски, 4-8 предложений",'
    '"tags":"25-45 английских danbooru-тегов через запятую, нижний регистр, без masterpiece/best quality",'
    '"has_person":true}}'
)

REF_SYS = (
    "ПОЛЬЗОВАТЕЛЬ ПРИЛОЖИЛ ИСХОДНИКИ (фон, персонаж, стиль). Их описания дала зрительная модель.\n"
    "Внешность персонажа (волосы, глаза, одежда, телосложение) и локацию бери ИЗ ОПИСАНИЙ, не выдумывай.\n"
    "Теги из описаний можно копировать дословно. Противоречий между исходником и промптом быть не должно.\n"
    "Просьба пользователя важнее исходника только в том, что он явно попросил изменить.\n"
    "Про то, что уже видно на исходниках, вопросов НЕ задавай."
)

REF_MODE_HINT = {
    "tags": "Исходники — смысловой референс, генерация с нуля. Персонаж и локация обязаны совпадать с описаниями.",
    "img2img": "Исходник пойдёт в ComfyUI как основа (img2img): композиция, поза и ракурс сохранятся. "
               "Промпт обязан описывать ВСЮ сцену целиком, включая то, что уже видно на исходнике.",
    "composite": "Фон и персонаж склеены в черновой коллаж, он пойдёт как основа img2img. "
                 "Персонаж — из референса персонажа, локация и свет — из референса фона. "
                 "Не меняй внешность, позу и место без просьбы.",
}

def refs_block(info, mode):
    if not info:
        return ""
    lines = ["[ИСХОДНИКИ ПОЛЬЗОВАТЕЛЯ — ГЛАВНЫЙ ИСТОЧНИК ПРАВДЫ]"]
    for r in info:
        lines.append(f"— {r['role']} ({r['file']}): {r.get('desc') or '(описание недоступно)'}")
        if r.get("tags"):
            lines.append("  теги: " + r["tags"])
    lines.append(REF_MODE_HINT.get(mode, ""))
    return "\n".join(lines)

# ===== ОБЩЕЕ СОСТОЯНИЕ =====

ASSETS = {"ckpts": [], "loras": [], "upscales": [], "ipa": False}
ASSET_LOCK = asyncio.Lock()
MODEL_ID = None
HTTP = None
DEBUG_LOG = []

def load_mem():
    if os.path.isfile(MEM_FILE):
        return json.load(open(MEM_FILE, encoding="utf-8"))
    return {"summary": "", "entries": []}

def save_mem(m):
    json.dump(m, open(MEM_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def _memctx(mem):
    lines = []
    for e in mem.get("entries", [])[-3:]:
        lines.append(json.dumps({k: e.get(k) for k in ("theme", "ok", "defects", "feedback")},
                                ensure_ascii=False))
    return (f"[САММАРИ]\n{mem.get('summary') or 'пусто'}\n[ПОСЛЕДНИЕ]\n" + "\n".join(lines))

def parse_json(t):
    t = re.sub(r"```[a-zA-Z]*", "", t or "").replace("`", "")
    try:
        return json.loads(t)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        objs, depth, start = [], 0, None
        for i, ch in enumerate(t):
            if ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(t[start:i + 1]); start = None
        for cand in reversed(objs):
            try:
                return json.loads(cand)
            except Exception:
                continue
    raise RuntimeError(f"модель не выдала JSON: {t[:200]!r}")

# ===== LM STUDIO =====

LM_TIMEOUT = 300  # потолок на один запрос к модели, секунд

def rf(name, schema):
    """response_format для LM Studio: JSON гарантируется грамматикой на этапе сэмплирования."""
    return {"type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema}}

_IMG_ITEM = {
    "type": "object",
    "required": ["prompt", "seed"],
    "properties": {
        "prompt": {"type": "string"},
        "seed": {"type": ["integer", "null"]},
    },
}

def _plan_schema(max_items):
    return {
        "type": "object",
        "required": ["plan", "images"],
        "properties": {
            "plan": {"type": "string"},
            "images": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_items,
                "items": _IMG_ITEM,
            },
        },
    }

S_PLAN = rf("plan", _plan_schema(10))
S_EDIT = rf("edit", _plan_schema(1))

S_INTENT = rf("intent", {"type": "object",
    "required": ["no_questions", "freeform", "new_topic", "edit", "raw_prompt"], "properties": {
        "no_questions": {"type": "boolean"}, "freeform": {"type": "boolean"},
        "new_topic": {"type": "boolean"}, "edit": {"type": "boolean"},
        "raw_prompt": {"type": ["string", "null"]}}})

S_IDEAS = rf("ideas", {"type": "object", "required": ["ideas"], "properties": {
    "ideas": {"type": "array", "minItems": 4, "maxItems": 16, "items": {"type": "string"}}}})

S_QUESTIONS = rf("questions", {"type": "object", "required": ["questions"], "properties": {
    "questions": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
        "type": "object", "required": ["q", "options"], "properties": {
            "q": {"type": "string"},
            "options": {"type": "array", "minItems": 2, "maxItems": 8,
                        "items": {"type": "string"}}}}}}})

S_FIX = rf("fix", {"type": "object", "required": ["prompt"],
                   "properties": {"prompt": {"type": "string"}}})

S_CRIT = rf("critique", {"type": "object",
    "required": ["ok", "defects", "comment", "description"], "properties": {
        "ok": {"type": "boolean"}, "defects": {"type": "array", "items": {"type": "string"}},
        "comment": {"type": "string"}, "description": {"type": "string"}}})

S_REF = rf("ref", {"type": "object", "required": ["description", "tags", "has_person"],
    "properties": {"description": {"type": "string"}, "tags": {"type": "string"},
                   "has_person": {"type": "boolean"}}})

async def post_json(s, url, payload):
    async with s.post(url, json=payload) as r:
        return await r.json()

def _msg_text(msg):
    for k in ("content", "reasoning_content", "reasoning"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _dump(tag, payload, resp):
    DEBUG_LOG.append({"attempt": tag, "request": payload, "response": resp})
    del DEBUG_LOG[:-6]
    try:
        json.dump(DEBUG_LOG, open(LLM_DEBUG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass

async def ensure_model(s):
    global MODEL_ID
    if not MODEL_ID:
        models = (await (await s.get(LM_URL + "/v1/models")).json())["data"]
        MODEL_ID = next((m["id"] for m in models if "qwen" in m["id"].lower()), models[0]["id"])
        print("[llm] модель:", MODEL_ID)

async def llm(s, messages, temp, mt, want_json=False, schema=None, timeout=LM_TIMEOUT):
    await ensure_model(s)
    need_json = bool(want_json or schema)
    budget = int(mt)                                   # 1.2: без пола MIN_TOKENS

    base = {"model": MODEL_ID, "messages": messages, "temperature": temp,
            "stream": False, "max_tokens": budget,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False}}
    if schema:
        base["response_format"] = schema

    native = {"model": MODEL_ID, "temperature": temp, "stream": False, "store": False,
              "max_output_tokens": budget, "reasoning": "off",
              "system_prompt": "\n".join(m["content"] for m in messages
                                         if m["role"] == "system" and isinstance(m["content"], str)),
              "input": "\n\n".join(m["content"] for m in messages
                                   if m["role"] != "system" and isinstance(m["content"], str))}
    if schema:
        native["response_format"] = schema

    variants = [("schema_off", LM_URL + "/v1/chat/completions", base),
                ("native_off",   LM_URL + "/api/v1/chat",           native)]
    if need_json:
        # запасной вариант без грамматики — только если сервер не умеет response_format
        plain = dict(base)
        plain.pop("response_format", None)
        plain["max_tokens"] = max(budget, 1500)
        variants.append(("plain_retry", LM_URL + "/v1/chat/completions", plain))

    err = ""
    for tag, url, payload in variants:
        print(f"[llm] {tag} · schema={'да' if schema else 'нет'} · budget={budget}")
        try:
            j = await asyncio.wait_for(post_json(s, url, payload), timeout)   # 1.3: таймаут
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = f"{tag}: {e}"; continue
        _dump(tag, payload, j)
        if j.get("error"):
            err = f"{tag}: {j['error']}"; continue
        if tag == "native_off":
            txt = "\n".join(o.get("content", "") for o in j.get("output", [])
                            if o.get("type") in ("message", "text")).strip()
        else:
            ch = (j.get("choices") or [{}])[0]
            txt = _msg_text(ch.get("message", {}))
        if not txt:
            err = f"{tag}: пустой content"; continue
        if need_json:
            try:
                parse_json(txt)
            except Exception as e:
                err = f"{tag}: {e}"; continue
        print(f"[llm] сработал вариант: {tag}")
        return txt
    raise RuntimeError(f"LLM не дала пригодный ответ. {err} | см. llm_debug.json")

# ===== COMFYUI: ассеты =====

def _combo_list(oi, node, field):
    """Имена из object_info: старый формат [[...]] и новый ['COMBO', {'options': [...]}]."""
    try:
        spec = oi[node]["input"]["required"][field]
    except Exception:
        return []
    if spec and isinstance(spec[0], list):            # старый ComfyUI
        return [str(x) for x in spec[0]]
    if len(spec) > 1 and isinstance(spec[1], dict):   # новый ComfyUI (COMBO)
        out = []
        for o in spec[1].get("options") or []:
            if isinstance(o, str):
                out.append(o)
            elif isinstance(o, dict):
                v = o.get("content") or o.get("name") or o.get("value")
                if v:
                    out.append(str(v))
        return out
    return []

async def fetch_assets(s=None):
    own = s is None and HTTP is None
    if s is None:
        s = HTTP or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
    try:
        if not ASSETS["ckpts"]:
            oi = await (await s.get(COMFY_URL + "/object_info/CheckpointLoaderSimple")).json()
            ASSETS["ckpts"] = _combo_list(oi, "CheckpointLoaderSimple", "ckpt_name")
        if not ASSETS["loras"]:
            oi = await (await s.get(COMFY_URL + "/object_info/LoraLoader")).json()
            ASSETS["loras"] = _combo_list(oi, "LoraLoader", "lora_name")
        if not ASSETS["upscales"]:
            oi = await (await s.get(COMFY_URL + "/object_info/UpscaleModelLoader")).json()
            ASSETS["upscales"] = _combo_list(oi, "UpscaleModelLoader", "model_name")
        if not ASSETS.get("ipa"):
            oi = await (await s.get(COMFY_URL + "/object_info/IPAdapterUnifiedLoader")).json()
            ASSETS["ipa"] = bool(oi) and "IPAdapterUnifiedLoader" in oi
    except Exception as e:
        print("[assets] ошибка:", e)
    finally:
        if own:
            await s.close()
    print("[assets] ckpts:", len(ASSETS["ckpts"]),
          "| loras:", len(ASSETS["loras"]),
          "| upscales:", len(ASSETS["upscales"]),
          "| ipadapter:", ASSETS.get("ipa"))
    if ASSETS["ckpts"] and CONFIG["ckpt"] not in ASSETS["ckpts"]:
        CONFIG["ckpt"] = ASSETS["ckpts"][0]
        save_config(CONFIG)

def _pick_img(outputs, node_id, prefix):
    n = outputs.get(node_id) or {}
    for im in (n.get("images") or []):
        fn = im.get("filename")
        if fn and im.get("type") != "temp":
            return os.path.basename(fn)
    for v in outputs.values():
        for im in (v.get("images") or []):
            fn = im.get("filename") or ""
            if im.get("type") != "temp" and os.path.basename(fn).startswith(prefix):
                return os.path.basename(fn)
    return None

def _disk_img(prefix):
    files = [f for f in glob.glob(os.path.join(CONFIG["output_dir"], prefix + "*"))
             if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    return os.path.basename(max(files, key=os.path.getmtime)) if files else None

def _comfy_err(st):
    for m in st.get("messages", []):
        if m and m[0] in ("execution_error", "execution_interrupted"):
            d = m[1] if len(m) > 1 else {}
            return f"{d.get('node_type', '?')} [{d.get('node_id', '?')}]: {d.get('exception_message', m[0])}"
    return "причина не указана"

def save_dir():
    d = CONFIG.get("save_dir") or os.path.join(BASE_DIR, "generated")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print("[archive] папка недоступна:", e)
        d = CONFIG["output_dir"]
    return d

def img_path(name):
    n = os.path.basename(name or "")
    for root in (save_dir(), CONFIG["output_dir"], UPLOAD_DIR):
        p = os.path.join(root, n)
        if os.path.isfile(p):
            return p
    return os.path.join(CONFIG["output_dir"], n)

def safe_name(t, n=30):
    t = re.sub(r"[^0-9A-Za-zА-Яа-яЁё _-]+", "", str(t or ""))
    t = t.strip().replace(" ", "_")
    return t[:n] or "series"

def archive(fin, base, theme, seed):
    d = save_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = safe_name(theme)
    res = {"final": fin, "base": base}
    for kind, name in (("final", fin), ("base", base)):
        if not name:
            continue
        src = os.path.join(CONFIG["output_dir"], os.path.basename(name))
        if not os.path.isfile(src):
            continue
        ext = os.path.splitext(src)[1] or ".png"
        suf = "" if kind == "final" else "_base"
        dst = os.path.join(d, f"{stamp}_{slug}_{seed}{suf}{ext}")
        k = 1
        while os.path.exists(dst):
            dst = os.path.join(d, f"{stamp}_{slug}_{seed}{suf}_{k}{ext}")
            k += 1
        try:
            shutil.copy2(src, dst)
            res[kind] = os.path.basename(dst)
        except Exception as e:
            print("[archive] не скопировано:", e)
    return res["final"], res["base"]

def sidecar(name, theme, prompt, seed, verdict, fb):
    try:
        p = os.path.join(save_dir(), os.path.splitext(os.path.basename(name))[0] + ".txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"тема: {theme}\nseed: {seed}\nвердикт: {verdict}\n\n"
                    f"оценка модели:\n{fb}\n\nпромпт:\n{prompt}\n")
    except Exception as e:
        print("[archive] txt не записан:", e)

# ===== ИСХОДНИКИ: файлы, коллаж, загрузка в ComfyUI, описание =====

def ref_path(name):
    return os.path.join(UPLOAD_DIR, os.path.basename(str(name or "")))

def pick_canvas(w, h):
    ar = w / max(1, h)
    return min(SDXL_BUCKETS, key=lambda b: abs(b[0] / b[1] - ar))

def _cover(im, w, h):
    iw, ih = im.size
    k = max(w / iw, h / ih)
    im = im.resize((max(1, int(iw * k)), max(1, int(ih * k))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))

def _cutout(im, tol=26):
    """Грубое вырезание однотонного фона (белый/серый скан персонажа)."""
    im = im.convert("RGBA")
    px = im.load()
    W, H = im.size
    corners = [px[0, 0], px[W - 1, 0], px[0, H - 1], px[W - 1, H - 1]]
    if any(c[3] < 250 for c in corners):
        return im                                   # альфа уже есть
    bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    if max(abs(c[i] - bg[i]) for c in corners for i in range(3)) > tol:
        return im                                   # фон не однотонный
    diff = ImageChops.difference(im.convert("RGB"), Image.new("RGB", im.size, bg)).convert("L")
    im.putalpha(diff.point(lambda v: 0 if v < tol else 255))
    return im

def make_composite(bg_file, fg_file, w, h, cut=False):
    if Image is None:
        raise RuntimeError("нужен Pillow: pip install pillow")
    bg = _cover(Image.open(ref_path(bg_file)).convert("RGB"), w, h).convert("RGBA")
    fg = Image.open(ref_path(fg_file)).convert("RGBA")
    if cut:
        fg = _cutout(fg)
    k = min((h * 0.94) / fg.height, (w * 0.90) / fg.width)
    fg = fg.resize((max(1, int(fg.width * k)), max(1, int(fg.height * k))), Image.LANCZOS)
    bg.alpha_composite(fg, ((w - fg.width) // 2, h - fg.height))
    out = ref_path("comp-" + uuid.uuid4().hex[:8] + ".png")
    bg.convert("RGB").save(out, "PNG")
    print("[refs] коллаж:", os.path.basename(out), f"{w}x{h}")
    return out

def _ref_preview(path, max_side=1280):
    """Превью для VLM: прозрачность заливается белым, чтобы не было чёрного фона."""
    if Image is None:
        return _shrink(path, max_side)
    im = Image.open(path)
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.split()[-1])
        im = flat
    else:
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "jpeg"

async def comfy_upload(s, path, subfolder="heretic_refs"):
    with open(path, "rb") as f:
        blob = f.read()
    fd = aiohttp.FormData()
    fd.add_field("image", io.BytesIO(blob), filename=os.path.basename(path), content_type="image/png")
    fd.add_field("subfolder", subfolder)
    fd.add_field("type", "input")
    fd.add_field("overwrite", "true")
    async with s.post(COMFY_URL + "/upload/image", data=fd) as r:
        j = await r.json()
    name = j.get("name") or os.path.basename(path)
    sub = j.get("subfolder") or subfolder
    return f"{sub}/{name}" if sub else name

async def describe_ref(s, path, role):
    d = {"role": role, "file": os.path.basename(path), "desc": "", "tags": "", "has_person": None}
    if not os.path.isfile(path):
        return d
    try:
        await ensure_model(s)
        raw, fmt = _ref_preview(path, 1280)
        b64 = base64.b64encode(raw).decode()
        payload = {"model": MODEL_ID, "temperature": 0.2, "max_tokens": 1000, "stream": False,
                   "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
                   "response_format": S_REF,
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": REF_T.format(role=role)},
                       {"type": "image_url",
                        "image_url": {"url": f"data:image/{fmt};base64," + b64}}]}]}
        j = await asyncio.wait_for(post_json(s, LM_URL + "/v1/chat/completions", payload), 180)
        if j.get("error"):
            raise RuntimeError(str(j["error"])[:160])
        v = parse_json(_msg_text((j.get("choices") or [{}])[0].get("message", {})))
        d["desc"] = str(v.get("description") or "")[:1400]
        d["tags"] = str(v.get("tags") or "")[:900]
        d["has_person"] = v.get("has_person")
        print(f"[refs] {role} · {d['file']} описан ({len(d['tags'].split(','))} тегов)")
    except Exception as e:
        print("[refs] описание не удалось:", e)
    return d

async def prepare_refs(ch, s, refs, intent="new", prep=True):
    """Описывает исходники и готовит основу для img2img / коллажа."""
    out = {"info": [], "init": None, "size": None, "denoise": None, "mode": "tags", "ipa": None}
    refs = refs or []
    if not refs:
        last = ch["last"]
        if intent == "edit" and last.get("init"):        # правка того же коллажа
            out.update(info=last.get("refs") or [], init=last.get("init"),
                       size=last.get("size"), denoise=last.get("denoise"),
                       mode=last.get("mode") or "img2img")
        return out
    for rf in refs:
        key = (rf["file"], rf["role"])
        if key not in REF_CACHE:
            REF_CACHE[key] = await describe_ref(s, ref_path(rf["file"]), rf["role"])
        out["info"].append(REF_CACHE[key])
    if not prep:
        return out

    cfg = CONFIG
    info = out["info"]
    bg = next((r for r in info if r["role"] == "фон"), None)
    fg = next((r for r in info if r["role"] == "персонаж"), None)
    mode = cfg.get("ref_mode", "auto")
    if mode == "auto":
        mode = "composite" if (bg and fg) else ("img2img" if (bg or fg) else "tags")
    if mode == "composite" and not (bg and fg):
        mode = "img2img" if (bg or fg) else "tags"
    out["mode"] = mode

    src = None
    if mode == "composite":
        try:
            w, h = pick_canvas(*Image.open(ref_path(bg["file"])).size)
            src = make_composite(bg["file"], fg["file"], w, h, bool(cfg.get("ref_cutout")))
        except Exception as e:
            print("[refs] коллаж не собрался:", e)
            out["mode"] = mode = "img2img"
            src = ref_path((bg or fg)["file"])
    elif mode == "img2img":
        src = ref_path((fg or bg or info[0])["file"])

    if src and os.path.isfile(src):
        try:
            iw, ih = Image.open(src).size
            out["size"] = pick_canvas(iw, ih) if cfg.get("ref_auto_size", True) else None
            out["denoise"] = float(cfg.get("ref_denoise", 0.55))
            out["init"] = await comfy_upload(s, src)
            print(f"[refs] режим {mode} · основа {out['init']} · denoise {out['denoise']}")
        except Exception as e:
            print("[refs] основа не загружена в ComfyUI:", e)
            out["mode"] = "tags"

    if cfg.get("ref_ipadapter") and ASSETS.get("ipa") and fg:
        try:
            out["ipa"] = await comfy_upload(s, ref_path(fg["file"]))
        except Exception as e:
            print("[refs] ipadapter:", e)
    return out

def prune_uploads(days=7):
    """Чистит старые черновые коллажи, исходники не трогает."""
    cut = time.time() - days * 86400
    for f in glob.glob(os.path.join(UPLOAD_DIR, "comp-*.png")):
        try:
            if os.path.getmtime(f) < cut:
                os.remove(f)
        except Exception:
            pass

# ===== ГРАФ =====

def build_graph(content, seed, pref_final="heretic", pref_base="heretic_base",
                init=None, denoise=None, size=None, ipa=None):
    cfg = CONFIG
    w, h = size or RESOLUTIONS.get(cfg["resolution"], (1408, 704))
    fw, fh = cfg.get("fw_fh") or [2000, 1000]
    if init and cfg.get("ref_auto_size", True):
        k = max(fw, fh) / float(max(w, h))
        fw, fh = max(512, int(w * k) // 8 * 8), max(512, int(h * k) // 8 * 8)
    loras = [l for l in cfg["loras"] if l.get("on") and l.get("name")]
    g = {"ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": cfg["ckpt"]}}}
    m, c, v = ["ckpt", 0], ["ckpt", 1], ["ckpt", 2]
    for i, l in enumerate(loras):
        if l["name"] not in ASSETS["loras"]:
            continue
        nid = f"lora{i}"
        sm = float(l.get("strength", 0.8))
        sc = float(l.get("strength_clip", sm))
        g[nid] = {"class_type": "LoraLoader", "inputs": {
            "model": m, "clip": c, "lora_name": l["name"],
            "strength_model": sm, "strength_clip": sc}}
        m, c = [nid, 0], [nid, 1]
    cs = int(cfg.get("clip_skip", 2) or 0)
    if cs > 0:
        g["clipskip"] = {"class_type": "CLIPSetLastLayer",
                         "inputs": {"clip": c, "stop_at_clip_layer": -cs}}
        c = ["clipskip", 0]
    pos = content.strip().rstrip(",")
    if cfg.get("quality_on", True):
        pos = QUALITY + ", " + pos
    neg = NEGATIVE
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": c, "text": pos}}
    g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": c, "text": neg}}

    if init:
        g["ref_img"] = {"class_type": "LoadImage", "inputs": {"image": init}}
        g["ref_fit"] = {"class_type": "ImageScale", "inputs": {
            "image": ["ref_img", 0], "upscale_method": "lanczos",
            "width": w, "height": h, "crop": "disabled"}}
        g["latent"] = {"class_type": "VAEEncode",
                       "inputs": {"pixels": ["ref_fit", 0], "vae": v}}
        d1 = max(0.05, min(1.0, float(denoise if denoise is not None
                                     else cfg.get("ref_denoise", 0.55))))
    else:
        g["latent"] = {"class_type": "EmptyLatentImage",
                       "inputs": {"width": w, "height": h, "batch_size": 1}}
        d1 = 1.0

    if ipa and cfg.get("ref_ipadapter") and ASSETS.get("ipa"):
        g["ipa_load"] = {"class_type": "IPAdapterUnifiedLoader",
                         "inputs": {"model": m, "preset": "PLUS (high strength)"}}
        g["ipa_img"] = {"class_type": "LoadImage", "inputs": {"image": ipa}}
        g["ipa"] = {"class_type": "IPAdapterAdvanced", "inputs": {
            "model": ["ipa_load", 0], "ipadapter": ["ipa_load", 1], "image": ["ipa_img", 0],
            "weight": float(cfg.get("ref_ipa_weight", 0.7)), "weight_type": "linear",
            "combine_embeds": "concat", "start_at": 0.0, "end_at": 1.0,
            "embeds_scaling": "V only"}}
        m = ["ipa", 0]

    g["ks1"] = {"class_type": "KSampler", "inputs": {
        "model": m, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
        "seed": seed, "steps": int(cfg["steps"]), "cfg": float(cfg["cfg"]),
        "sampler_name": cfg.get("sampler", "euler_ancestral"),
        "scheduler": cfg.get("scheduler", "normal"), "denoise": d1}}
    g["dec1"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks1", 0], "vae": v}}
    g["save_base"] = {"class_type": "SaveImage",
                      "inputs": {"images": ["dec1", 0], "filename_prefix": pref_base}}
    up = cfg.get("upscaler")
    has_up = bool(up and up in ASSETS["upscales"])
    if has_up:
        g["up_model"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": up}}
    src = ["dec1", 0]
    if cfg.get("hires_on", True):
        hs = float(cfg.get("hires_scale", 1.5))
        hw, hh = int(w * hs), int(h * hs)
        cap = int(cfg.get("hires_max_px", 2600000))
        if hw * hh > cap:
            k = (cap / float(hw * hh)) ** 0.5
            hw, hh = int(hw * k), int(hh * k)
        hw = max(512, hw // 8 * 8)
        hh = max(512, hh // 8 * 8)
        if has_up:
            g["up1"] = {"class_type": "ImageUpscaleWithModel",
                        "inputs": {"upscale_model": ["up_model", 0], "image": src}}
            src = ["up1", 0]
        g["sc1"] = {"class_type": "ImageScale", "inputs": {
            "image": src, "upscale_method": "lanczos", "width": hw, "height": hh, "crop": "disabled"}}
        g["enc2"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["sc1", 0], "vae": v}}
        g["ks2"] = {"class_type": "KSampler", "inputs": {
            "model": m, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["enc2", 0],
            "seed": seed, "steps": int(cfg.get("hires_steps", 20)), "cfg": float(cfg.get("hires_cfg", 5.0)),
            "sampler_name": cfg.get("hires_sampler", "dpmpp_2m_sde"),
            "scheduler": cfg.get("hires_scheduler", "karras"),
            "denoise": float(cfg.get("hires_denoise", 0.4))}}
        g["dec2"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks2", 0], "vae": v}}
        src = ["dec2", 0]
    if has_up and (fw > w or fh > h):
        g["up2"] = {"class_type": "ImageUpscaleWithModel",
                    "inputs": {"upscale_model": ["up_model", 0], "image": src}}
        g["sc2"] = {"class_type": "ImageScale", "inputs": {
            "image": ["up2", 0], "upscale_method": "lanczos", "width": fw, "height": fh, "crop": "disabled"}}
        src = ["sc2", 0]
    g["save_final"] = {"class_type": "SaveImage",
                       "inputs": {"images": src, "filename_prefix": pref_final}}
    return g

# ===== ПРОГРЕСС ШАГОВ (WebSocket ComfyUI) =====

def _progress(ch, info, node, value, mx):
    if ch["epoch"] != info["epoch"]:
        return
    item = info["item"]
    s1, s2 = info["steps1"], info["steps2"]
    total = max(1, s1 + s2)
    if node == "ks2":
        stage, done = "hires", s1 + value
    else:
        stage, done = "база", value
    pr = {"active": True, "idx": item.get("idx"), "node": node, "stage": stage,
          "value": int(value), "max": int(mx),
          "pct": int(round(value * 100.0 / max(1, mx))),
          "overall": int(round(done * 100.0 / total))}
    ch["progress"] = pr
    item["progress"] = pr

async def ws_loop():
    url = COMFY_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws?clientId=" + WS_CLIENT
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    print("[ws] подключен к ComfyUI, прогресс шагов активен")
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            j = json.loads(m.data)
                        except Exception:
                            continue
                        t, d = j.get("type"), (j.get("data") or {})
                        pid = d.get("prompt_id")
                        if not pid and len(PID_MAP) == 1:
                            pid = next(iter(PID_MAP))
                        info = PID_MAP.get(pid)
                        if not info:
                            continue
                        ch = info["chat"]
                        if t == "progress":
                            node = d.get("node") or info.get("node") or "ks1"
                            _progress(ch, info, node, d.get("value", 0), d.get("max", 1))
                        elif t == "executing":
                            node = d.get("node")
                            info["node"] = node
                            if node == "ks1":
                                _progress(ch, info, "ks1", 0, info["steps1"])
                            elif node == "ks2":
                                _progress(ch, info, "ks2", 0, info["steps2"])
                            elif node and ch["epoch"] == info["epoch"]:
                                pr = dict(ch["progress"] or {"active": True, "idx": info["item"].get("idx"),
                                                             "value": 0, "max": info["steps1"],
                                                             "pct": 0, "overall": 0})
                                pr["stage"] = "апскейл" if node in ("up2", "sc2", "up1", "sc1", "ref_fit") else "подготовка"
                                pr["node"] = node
                                ch["progress"] = pr
                        elif t == "execution_start" and ch["epoch"] == info["epoch"]:
                            pr = dict(ch["progress"] or {})
                            pr.update(active=True, stage="подготовка")
                            ch["progress"] = pr
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[ws] обрыв:", e)
            await asyncio.sleep(3)

async def comfy_generate(ch, s, content, seed, item, init=None, denoise=None, size=None, ipa=None):
    tag = str(random.randint(100000000, 999999999))
    pref_final, pref_base = "heretic_" + tag, "heretic_base_" + tag
    graph = build_graph(content, seed, pref_final, pref_base,
                        init=init, denoise=denoise, size=size, ipa=ipa)
    cfg = CONFIG
    steps1 = int(cfg["steps"])
    steps2 = int(cfg.get("hires_steps", 20)) if cfg.get("hires_on", True) else 0
    queued = False
    try:
        q = await (await s.get(COMFY_URL + "/queue")).json()
        queued = bool(q.get("queue_running"))
    except Exception:
        pass
    async with s.post(COMFY_URL + "/prompt", json={"prompt": graph, "client_id": WS_CLIENT}) as r:
        j = await r.json()
    if "prompt_id" not in j:
        raise RuntimeError(f"ComfyUI отверг граф: {str(j)[:600]}")
    pid = j["prompt_id"]
    info = {"chat": ch, "item": item, "steps1": steps1, "steps2": steps2,
            "epoch": ch["epoch"], "node": None}
    PID_MAP[pid] = info
    pr = {"active": True, "idx": item.get("idx"), "node": None,
          "stage": "в очереди" if queued else "подготовка",
          "value": 0, "max": steps1, "pct": 0, "overall": 0}
    ch["progress"] = pr
    item["progress"] = pr
    print(f"[comfy] prompt {pid} | final={pref_final} base={pref_base} | init={init}")
    try:
        empty = 0
        for _ in range(1800):
            if ch["cancel"]:
                raise RuntimeError("отменено")
            await asyncio.sleep(1)
            h = await (await s.get(f"{COMFY_URL}/history/{pid}")).json()
            if pid not in h:
                continue
            entry = h[pid]
            st = entry.get("status", {}) or {}
            if st.get("status_str") == "error":
                raise RuntimeError("ComfyUI: " + _comfy_err(st))
            o = entry.get("outputs", {}) or {}
            fin = _pick_img(o, "save_final", pref_final) or _disk_img(pref_final)
            base = _pick_img(o, "save_base", pref_base) or _disk_img(pref_base)
            if fin:
                return fin, base or fin
            empty += 1
            print(f"[comfy] история без картинок ({empty}), ключи outputs: {list(o.keys())}")
            if empty >= 15:
                raise RuntimeError(f"ComfyUI отчитался '{st.get('status_str')}', но файлов нет. "
                                   f"outputs={list(o.keys())}. Проверь папку output и консоль ComfyUI.")
        raise RuntimeError("таймаут генерации")
    finally:
        PID_MAP.pop(pid, None)
        if ch["epoch"] == info["epoch"]:
            ch["progress"] = None
        item.pop("progress", None)

# ===== КРИТИК И ОЦЕНКА (LM STUDIO) =====

def _shrink(path, max_side=1280):
    try:
        im = Image.open(path)
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "jpeg"
    except Exception:
        return open(path, "rb").read(), "png"

async def critique(s, path, prompt, theme):
    empty = {"ok": True, "comment": "", "defects": [], "description": ""}
    if not (CONFIG.get("use_critic", True) or CONFIG.get("feedback_on", True)):
        return dict(empty, comment="осмотр выключен")
    if not os.path.isfile(path):
        return dict(empty, comment="файл картинки не найден")
    t0 = time.time()
    print(f"[critique] LM Studio · {os.path.basename(path)}")
    raw, fmt = _shrink(path)
    b64 = base64.b64encode(raw).decode()
    content = [{"type": "text", "text": CRIT_T.format(theme=theme, prompt=prompt)},
               {"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64," + b64}}]
    await ensure_model(s)
    err = ""
    # та же модель, что пишет теги: сначала просим без thinking, потом обычный вызов
    for extra in ({"reasoning_effort": "none",
                   "chat_template_kwargs": {"enable_thinking": False}}, {}):
        payload = {"model": MODEL_ID, "temperature": 0.2, "max_tokens": 1200,
                   "stream": False, "response_format": S_CRIT,
                   "messages": [{"role": "user", "content": content}], **extra}
        try:
            j = await asyncio.wait_for(post_json(s, LM_URL + "/v1/chat/completions", payload), 240)
            if j.get("error"):
                err = str(j["error"])[:200]; continue
            txt = _msg_text((j.get("choices") or [{}])[0].get("message", {}))
            if not txt:
                err = "пустой ответ"; continue
            v = parse_json(txt)
            if not isinstance(v, dict):
                err = "критик вернул не объект"; continue
            print(f"[critique] посмотрела за {time.time() - t0:.0f} с")
            return dict(empty, **{k: v[k] for k in empty if k in v})
        except Exception as e:
            err = str(e)[:200]
    print(f"[critique] ошибка: {err}")
    return dict(empty, comment=f"LM Studio не смогла посмотреть: {err}")

async def feedback(s, theme, prompt, v, path=None):
    desc = (v or {}).get("description") or ""
    if not desc and not path:
        return ""
    text = FB_T.format(theme=theme, prompt=prompt,
                       desc=desc or "(описания нет, смотри на картинку сам)",
                       defects=", ".join((v or {}).get("defects") or []) or "нет")
    if path and os.path.isfile(path):
        try:
            await ensure_model(s)
            raw, fmt = _shrink(path)
            b64 = base64.b64encode(raw).decode()
            payload = {"model": MODEL_ID, "temperature": 0.5, "max_tokens": 700,
                       "stream": False, "reasoning_effort": "none",
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": text},
                           {"type": "image_url",
                            "image_url": {"url": f"data:image/{fmt};base64," + b64}}]}]}
            j = await asyncio.wait_for(post_json(s, LM_URL + "/v1/chat/completions", payload), 180)
            if not j.get("error"):
                t = _msg_text((j.get("choices") or [{}])[0].get("message", {}))
                if t:
                    return t
        except Exception as e:
            print("[feedback] картинка не отправилась:", e)
    try:
        return await llm(s, [{"role": "user", "content": text}], 0.5, 700, timeout=180)
    except Exception as e:
        return f"(оценка не получена: {e})"

# ===== СЕРИЯ / УТОЧНЕНИЯ (на чат) =====

async def run_clarify(ch, user_text, refs=None):
    ch["busy"] = True
    epoch = ch["epoch"]
    dlg = ch["dialog"]
    dlg["theme"] = user_text
    dlg["history"] = [{"role": "user", "content": user_text}]
    dlg["refs"] = refs or []
    dlg["stage"] = "idle"
    fallback = False
    try:
        with contextlib.nullcontext(HTTP) as s:
            say(ch, "b", "Готовлю вопросы...")
            mem = load_mem()
            msgs = [{"role": "system", "content": ASK_SYS}]
            R = await prepare_refs(ch, s, refs, "new", prep=False)
            if R["info"]:
                say(ch, "b", "Посмотрел исходники: " + ", ".join(r["role"] for r in R["info"]))
                msgs.append({"role": "system", "content": REF_SYS})
                msgs.append({"role": "user", "content": refs_block(R["info"], "tags") +
                             "\nИсходники уже даны. Не спрашивай про то, что на них видно. "
                             "Максимум 8 вопросов, только про то, чего не хватает."})
            if mem.get("summary"):
                msgs.append({"role": "user", "content": "[ПРОШЛЫЙ ОПЫТ]\n" + mem["summary"]
                             + "\n(это фон, спрашивай только про новую тему)"})
            msgs += chat_msgs(ch, 6)
            msgs += dlg["history"]
            txt = await llm(s, msgs, 0.7, 4096, True, schema=S_QUESTIONS, timeout=300)
            if ch["epoch"] != epoch:
                return
            obj = parse_json(txt)
            if isinstance(obj, dict):
                raw = obj.get("questions") or obj.get("q") or []
            elif isinstance(obj, list):
                raw = obj
            else:
                raw = []
            if isinstance(raw, (dict, str)):
                raw = [raw]
            lines, clean = [], []
            for i, q in enumerate(raw, 1):
                if isinstance(q, str):
                    q = {"q": q, "options": []}
                if not isinstance(q, dict):
                    continue
                text = str(q.get("q") or q.get("question") or "").strip()
                if not text:
                    continue
                opts = q.get("options") or q.get("variants") or []
                if isinstance(opts, str):
                    opts = [opts]
                opts = [str(o).strip() for o in opts if str(o).strip()][:8]
                clean.append({"q": text, "options": opts})
                lines.append(f"{i}. {text}")
                if opts:
                    lines.append("   " + "   ".join(f"{j}) {o}" for j, o in enumerate(opts, 1)))
            if not clean:
                fallback = True
                say(ch, "b", "Вопросов не вышло — собираю промпты сразу, без уточнений.")
            else:
                dlg["history"].append({"role": "assistant",
                                       "content": json.dumps({"questions": clean}, ensure_ascii=False)})
                dlg["stage"] = "wait"
                chat_add(ch, "user", user_text)
                chat_add(ch, "assistant", "Задал уточняющие вопросы: " +
                         "; ".join(q["q"] for q in clean)[:800])
                say(ch, "q", f"Вопросов: {len(clean)}\n\n" + "\n".join(lines)
                    + "\n\nОтветь номерами: «1-2, 2-5, 3-1» (вопрос-вариант). Можно не на все."
                    + "\nМожно ответить словами. «генерируй» — решу сам.")
    except asyncio.CancelledError:
        dlg["stage"] = "idle"
        if ch["epoch"] == epoch:
            say(ch, "b", "Остановлено.")
    except Exception as e:
        if ch["epoch"] == epoch:
            say(ch, "e", f"Ошибка: {e}")
        dlg["stage"] = "idle"
    finally:
        ch["busy"] = False
    if fallback and ch["epoch"] == epoch and not ch["cancel"]:
        await run_series(ch, user_text, no_questions=True, refs=refs, intent="new")

async def run_series(ch, user_text, direct=False, no_questions=False, refs=None, intent=None):
    ch["busy"] = True
    ch["cancel"] = False
    epoch = ch["epoch"]
    dlg = ch["dialog"]
    last = ch["last"]
    mem = load_mem()
    answers = None
    if dlg["stage"] == "wait":
        answers = user_text
        user_text = dlg["theme"] or user_text
        refs = refs or dlg.get("refs") or []
        dlg["stage"] = "idle"
        intent = "new"                                    # ответы на вопросы = новая тема
    else:
        intent = intent or ("edit" if last_shot(ch) else "new")   # пришёл из h_chat
    try:
        with contextlib.nullcontext(HTTP) as s:
            await fetch_assets(s)
            R = await prepare_refs(ch, s, refs, intent)
            kw = dict(init=R["init"], denoise=R["denoise"], size=R["size"], ipa=R["ipa"])
            rb = refs_block(R["info"], R["mode"])
            if R["info"]:
                say(ch, "b", "Исходники: " + ", ".join(r["role"] for r in R["info"])
                    + f"\nРежим: {R['mode']}"
                    + (f" · denoise {R['denoise']}" if R["init"] else "")
                    + (f" · холст {R['size'][0]}x{R['size'][1]}" if R["size"] else "")
                    + (" · IPAdapter" if R["ipa"] else ""))
            if direct:
                intent = "new"
                if not extract_prompt(user_text):
                    say(ch, "e", "Не нашёл в сообщении теги промпта. Пришли промпт в том же сообщении.")
                    return
            memctx = _memctx(mem)
            history = chat_msgs(ch, HIST_TURNS)
            shots = last["shots"]
            base = pick_base(ch, user_text)
            if intent == "edit" and base.get("prompt"):
                for it in ch["items"]:
                    if it.get("file") and it.get("file") == base.get("file"):
                        it["status"], it["ok"] = "брак", False
                say(ch, "b", f"Режим правки · кадр #{base.get('idx', 1)} "
                             f"(seed {base.get('seed')}) → брак, генерирую заново.")
                brief = "\n".join(
                    f"кадр {sh.get('idx')}: seed {sh.get('seed')} | ok={sh.get('ok')} | {sh.get('prompt')}"
                    for sh in shots[-4:])
                msgs = [{"role": "system", "content": SYS},
                        {"role": "system", "content": EDIT_SYS}]
                if rb:
                    msgs.append({"role": "system", "content": REF_SYS})
                    msgs.append({"role": "user", "content": rb})
                msgs += history + [{"role": "user", "content": EDIT_T.format(
                    shots=brief or "(нет)",
                    theme=last["theme"] or "(не задан)",
                    seed=base.get("seed", 0),
                    prompt=base.get("prompt", ""),
                    desc=base.get("desc") or "(описания нет)",
                    defects=", ".join(base.get("defects") or []) or "не указаны",
                    verdict=(base.get("verdict") or "")[:600] or "(нет)",
                    request=user_text)}]
                chat_add(ch, "user", f"[ПРАВКА кадра seed {base.get('seed')}]: {user_text}")
            else:
                intent = "new"
                say(ch, "b", "Прямой режим: беру твой промпт как есть, без вопросов и переписывания."
                    if direct else "Собираю промпты...")
                msgs = [{"role": "system", "content": SYS}]
                if rb:
                    msgs.append({"role": "system", "content": REF_SYS})
                msgs.append({"role": "user", "content":
                             memctx + ("\n" + rb if rb else "") + "\n[ЗАПРОС]\n" + user_text})
                chat_add(ch, "user", user_text)
            if answers is not None:
                msgs += dlg["history"]
                msgs += [{"role": "user", "content": answers},
                         {"role": "user", "content":
                          "Уточнения получены. Ответы могут быть в виде «номер вопроса-номер варианта» — "
                          "сопоставь их со своим списком вопросов. Что не отвечено — реши сам. "
                          "Вопросов больше не задавай. Выдай план и промпты по правилам системы. Только JSON."}]
            if no_questions:
                msgs.append({"role": "user", "content":
                             "Вопросов НЕ задавай. Все решения прими сам. Сразу выдай план и промпты. Только JSON."})
            if direct:
                raw = extract_prompt(user_text)
                plan = {"plan": "Прямая генерация промпта пользователя без изменений.",
                        "theme": raw[:60], "images": [{"prompt": raw, "seed": None}]}
            else:
                plan = parse_json(await llm(s, msgs, 0.5 if intent == "edit" else 0.7, 1200 if intent == "edit" else 2400, True, schema=S_EDIT if intent == "edit" else S_PLAN, timeout=300))
            if ch["epoch"] != epoch:
                return
            if not isinstance(plan, dict):
                plan = {"images": plan if isinstance(plan, list) else []}
            if not plan.get("images") and not plan.get("questions"):
                say(ch, "b", "План пришёл пустой — требую промпты повторно.")
                msgs.append({"role": "user", "content":
                             "Ты не выдал ни одного промпта. Вопросы запрещены. Верни ТОЛЬКО JSON вида "
                             '{"plan":"...","images":[{"prompt":"45-65 английских тегов","seed":null}]}. '
                             "Минимум один объект в images."})
                plan = parse_json(await llm(s, msgs, 0.4, 2400, True, schema=S_EDIT if intent == "edit" else S_PLAN, timeout=300))
                if not isinstance(plan, dict):
                    plan = {"images": plan if isinstance(plan, list) else []}
                if ch["epoch"] != epoch:
                    return
            if not plan.get("images"):
                qs = plan.get("questions") or []
                if isinstance(qs, str):
                    qs = [qs]
                if qs and intent == "new" and not no_questions:
                    dlg["stage"] = "wait"
                    dlg["theme"] = user_text
                    dlg["refs"] = refs or []
                    dlg["history"].append({"role": "user", "content": answers or user_text})
                    dlg["history"].append({"role": "assistant",
                                           "content": json.dumps(qs, ensure_ascii=False)})
                    say(ch, "q", "\n".join(
                        f"{i}. " + str(q.get("q") if isinstance(q, dict) else q)
                        for i, q in enumerate(qs, 1)))
                    return
                if no_questions or direct:
                    raise RuntimeError("модель не выдала промпты даже после повторного запроса")
            dlg["history"], dlg["theme"], dlg["refs"] = [], "", []
            imgs = plan.get("images") or []
            if isinstance(imgs, dict):
                imgs = [imgs]
            imgs = [x for x in imgs if isinstance(x, dict) and str(x.get("prompt") or "").strip()]
            imgs = imgs[:1] if intent == "edit" else imgs[:CONFIG["max_images"]]
            if not imgs:
                say(ch, "b", "План пустой.")
                return
            say(ch, "b", (plan.get("plan", "") or "") +
                (f"\nПравка: 1 шт." if intent == "edit" else f"\nСерия: {len(imgs)} шт."))
            theme = last["theme"] if (intent == "edit" and last["theme"]) else (plan.get("theme") or user_text)
            first = max([int(it.get("idx") or 0) for it in ch["items"]] or [0]) + 1
            for idx, im in enumerate(imgs, first):
                if ch["cancel"] or ch["epoch"] != epoch:
                    if ch["epoch"] == epoch:
                        say(ch, "b", "Остановлено.")
                    break
                p = im["prompt"]
                seed = int(im.get("seed") or (base.get("seed") if intent == "edit" else None)
                           or random.randint(1, 2**32))
                item = {"idx": idx, "status": "генерация", "prompt": p, "seed": seed,
                        "file": None, "verdict": "", "ok": None, "retry": False,
                        "mode": R["mode"], "src": [r["file"] for r in R["info"]]}
                ch["items"].append(item)
                fin, base_img = await comfy_generate(ch, s, p, seed, item, **kw)
                if ch["epoch"] != epoch:
                    return
                fin, base_img = archive(fin, base_img, theme, seed)
                item["file"], item["status"] = fin, "осмотр"
                v = await critique(s, img_path(fin), p, theme)
                if ch["epoch"] != epoch:
                    return
                if CONFIG.get("use_critic", True) and not v.get("ok", True) and not ch["cancel"]:
                    item["retry"], item["status"] = True, "ретрай"
                    p = parse_json(await llm(s, [{"role": "user", "content": FIX_T.format(
                        prompt=p, defects=", ".join(v.get("defects", [])))}], 0.4, 600, True, schema=S_FIX, timeout=120)).get("prompt", p)
                    seed += 1
                    fin, base_img = await comfy_generate(ch, s, p, seed, item, **kw)
                    if ch["epoch"] != epoch:
                        return
                    fin, base_img = archive(fin, base_img, theme, seed)
                    item.update(file=fin, prompt=p, seed=seed, status="осмотр 2")
                    v = await critique(s, img_path(fin), p, theme)
                    if ch["epoch"] != epoch:
                        return
                item["ok"] = bool(v.get("ok", True))
                item["verdict"] = (v.get("comment", "") + "  " + ", ".join(v.get("defects", []))).strip()
                fb = ""
                if CONFIG.get("feedback_on", True) and not ch["cancel"]:
                    item["status"] = "оценка"
                    fb = await feedback(s, theme, p, v, img_path(fin))
                    if ch["epoch"] != epoch:
                        return
                if fb:
                    say(ch, "b", f"Кадр #{idx} · seed {seed}\n{fb}")
                    item["verdict"] = (item["verdict"] + "\n\n" + fb).strip()
                else:
                    say(ch, "b", f"Кадр #{idx}: зрительная модель недоступна — оценку по картинке дать не могу.")
                item["status"] = "готово" if item["ok"] else "брак"
                sidecar(fin, theme, p, seed, item["verdict"], fb)
                last["theme"] = theme
                last["ts"] = time.time()
                last["refs"], last["init"] = R["info"], R["init"]
                last["size"], last["denoise"], last["mode"] = R["size"], R["denoise"], R["mode"]
                last["shots"].append({
                    "idx": idx, "prompt": p, "seed": seed, "file": fin, "ok": item["ok"],
                    "defects": v.get("defects", []),
                    "desc": (v.get("description") or "")[:1200],
                    "verdict": (v.get("comment", "") or "")[:400],
                    "feedback": (fb or "")[:600]})
                del last["shots"][:-6]
                mem["entries"].append({"theme": theme, "prompt": p, "seed": seed, "file": fin,
                                       "ok": item["ok"], "defects": v.get("defects", []),
                                       "seen": (v.get("description") or "")[:600],
                                       "feedback": fb[:600]})
                mem["entries"] = mem["entries"][-200:]
                save_mem(mem)
                chat_add(ch, "assistant",
                         f"Кадр #{idx} готов. seed {seed}. файл {fin}. "
                         f"ok={item['ok']}. промпт: {p[:900]}"
                         + (f" | дефекты: {', '.join(v.get('defects', []))}" if v.get("defects") else "")
                         + (f" | оценка: {fb[:400]}" if fb else ""))
                save_chats()
            if not ch["cancel"] and ch["epoch"] == epoch and mem["entries"]:
                compact = [{k: e.get(k) for k in ("theme", "ok", "defects", "feedback")}
                           for e in mem["entries"][-6:]]
                mem["summary"] = await llm(s, [{"role": "user", "content": SUM_T.format(
                    old=mem["summary"], new=json.dumps(compact, ensure_ascii=False))}], 0.3, 400)
                save_mem(mem)
            if ch["epoch"] == epoch:
                say(ch, "b", ("Правка готова. " if intent == "edit" else "Серия завершена. ")
                    + "Файлы: " + save_dir()
                    + "\nНапиши «исправь …» — поправлю этот же кадр, или «новая тема: …» — начну заново.")
    except asyncio.CancelledError:
        if ch["epoch"] == epoch:
            say(ch, "b", "Остановлено.")
    except Exception as e:
        if ch["epoch"] == epoch:
            say(ch, "e", f"Ошибка: {e}")
    finally:
        ch["busy"] = False
        ch["cancel"] = False
        same = (ch["epoch"] == epoch)
        if same:
            ch["progress"] = None
        stale = [it for it in ch["items"] if same and it.get("status") not in ("готово", "брак")]
        for it in stale:
            it["status"], it["ok"] = "брак", False
        for it in ch["items"]:
            it.pop("progress", None)
        save_chats()

# ===== МОНИТОРИНГ ЖЕЛЕЗА (с потреблением, Вт) =====

HW = {"ts": 0.0, "data": {"cpu": "", "cpu_temp": None, "cpu_load": None, "cpu_power": None,
                          "ram": None, "gpus": []}}
CPU_TEMP = {"ts": 0.0, "val": None}
CPU_POWER = {"ts": 0.0, "val": None}
CPU_LOAD = {"prev": None, "val": None}

class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

def _cpu_load():
    try:
        import psutil
        v = psutil.cpu_percent(interval=None)
        CPU_LOAD["val"] = round(v, 1)
        return CPU_LOAD["val"]
    except Exception:
        pass
    if os.name == "nt":
        i, k, u = _FILETIME(), _FILETIME(), _FILETIME()
        try:
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
                f = lambda t: (t.hi << 32) | t.lo
                cur = (f(i), f(k), f(u))
                prev = CPU_LOAD["prev"]
                CPU_LOAD["prev"] = cur
                if prev:
                    di, dk, du = cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2]
                    tot = dk + du
                    if tot > 0:
                        CPU_LOAD["val"] = round(max(0.0, min(100.0, (tot - di) * 100.0 / tot)), 1)
        except Exception:
            pass
    return CPU_LOAD["val"]

def _sh(cmd, timeout=10):
    kw = {"capture_output": True, "text": True, "timeout": timeout}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    try:
        return subprocess.run(cmd, **kw).stdout or ""
    except Exception:
        return ""

def _num(x):
    try:
        return round(float(str(x).strip().replace(",", ".")), 1)
    except Exception:
        return None

def _cpu_name():
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    return platform.processor() or "CPU"

def _gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        for p in (r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(p):
                exe = p
                break
    if not exe:
        return []
    out = _sh([exe, "--query-gpu=name,temperature.gpu,memory.total,memory.used,utilization.gpu,power.draw",
               "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 5:
            gpus.append({"name": p[0], "temp": _num(p[1]), "mem_total": _num(p[2]),
                         "mem_used": _num(p[3]), "util": _num(p[4]),
                         "power": _num(p[5]) if len(p) >= 6 else None})
    return gpus

def _ram():
    try:
        import psutil
        v = psutil.virtual_memory()
        return {"total": round(v.total / 2**30, 1), "used": round((v.total - v.available) / 2**30, 1)}
    except Exception:
        pass
    if os.name == "nt":
        class MSX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MSX()
        m.dwLength = ctypes.sizeof(MSX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                t = m.ullTotalPhys / 2**30
                return {"total": round(t, 1), "used": round(t - m.ullAvailPhys / 2**30, 1)}
        except Exception:
            pass
    return None

def _ps():
    return shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

def _cpu_temp():
    now = time.time()
    if now - CPU_TEMP["ts"] < 10:
        return CPU_TEMP["val"]
    CPU_TEMP["ts"] = now
    val = None
    if os.name == "nt":
        for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
            out = _sh([_ps(), "-NoProfile", "-NonInteractive", "-Command",
                       "Get-CimInstance -Namespace " + ns + " -ClassName Sensor -ErrorAction SilentlyContinue | "
                       "Where-Object {$_.SensorType -eq 'Temperature' -and $_.Identifier -match 'cpu'} | "
                       "Select-Object -ExpandProperty Value"], timeout=12)
            vals = [v for v in (_num(x) for x in out.split()) if v is not None and 0 < v < 120]
            if vals:
                val = max(vals)
                break
        if val is None:
            out = _sh([_ps(), "-NoProfile", "-NonInteractive", "-Command",
                       "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                       "-ErrorAction SilentlyContinue | Select-Object -First 1).CurrentTemperature"], timeout=12)
            v = _num(out)
            if v and v > 1000:
                val = round(v / 10 - 273.15, 1)
    if val is None:
        CPU_TEMP["ts"] = now + 40
    CPU_TEMP["val"] = val
    return val

def _cpu_power():
    now = time.time()
    if now - CPU_POWER["ts"] < 10:
        return CPU_POWER["val"]
    CPU_POWER["ts"] = now
    val = None
    if os.name == "nt":
        for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
            out = _sh([_ps(), "-NoProfile", "-NonInteractive", "-Command",
                       "Get-CimInstance -Namespace " + ns + " -ClassName Sensor -ErrorAction SilentlyContinue | "
                       "Where-Object {$_.SensorType -eq 'Power' -and $_.Identifier -match 'cpu'} | "
                       "Select-Object -ExpandProperty Value"], timeout=12)
            vals = [v for v in (_num(x) for x in out.split()) if v is not None and 0 < v < 2000]
            if vals:
                val = max(vals)   # CPU Package
                break
    if val is None:
        CPU_POWER["ts"] = now + 40
    CPU_POWER["val"] = val
    return val

def collect_hw():
    now = time.time()
    if now - HW["ts"] < 2 and HW["data"]["gpus"]:
        return HW["data"]
    HW["ts"] = now
    if not HW["data"]["cpu"]:
        HW["data"]["cpu"] = _cpu_name()
    HW["data"]["gpus"] = _gpus()
    HW["data"]["ram"] = _ram()
    HW["data"]["cpu_temp"] = _cpu_temp()
    HW["data"]["cpu_power"] = _cpu_power()
    HW["data"]["cpu_load"] = _cpu_load()
    return HW["data"]

# ===== ВЕБ-ХЕНДЛЕРЫ =====

async def h_root(r):
    ideas = get_ideas()
    html = HTML.replace("{{IDEAS}}", json.dumps(ideas, ensure_ascii=False))
    return web.Response(text=html, content_type="text/html")

async def h_ideas_regen(r):
    mode = None
    try:
        mode = ((await r.json()) or {}).get("mode")
    except Exception:
        pass
    modes = globals().get("IDEA_MODES") or {}
    if mode not in modes:
        mode = globals().get("IDEA_MODE", "реализм")
    import inspect
    try:
        takes_mode = "mode" in inspect.signature(generate_ideas).parameters
    except (TypeError, ValueError):
        takes_mode = False
    ideas = await (generate_ideas(mode) if takes_mode else generate_ideas())
    if ideas:
        return web.json_response({"ok": True, "ideas": ideas, "mode": mode})
    return web.json_response({"ok": False, "error": "LM Studio не ответил", "mode": mode})

async def h_state(r):
    ch = CHATS.get(r.query.get("id")) or next(iter(CHATS.values()))
    return web.json_response({
        "id": ch["id"], "title": ch["title"], "busy": ch["busy"],
        "messages": ch["messages"], "items": ch["items"],
        "progress": ch["progress"], "stage": ch["dialog"]["stage"],
        "chats": [{"id": c["id"], "title": c["title"], "busy": c["busy"]} for c in CHATS.values()]})

async def h_chat_create(r):
    d = {}
    try:
        d = await r.json()
    except Exception:
        pass
    ch = make_chat((d.get("title") or "").strip() or None)
    save_chats()
    return web.json_response({"ok": True, "id": ch["id"], "title": ch["title"]})

async def h_chat_delete(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    if not ch:
        return web.json_response({"ok": False})
    ch["cancel"] = True
    t = ch.get("task")
    if t and not t.done():
        t.cancel()
    ch["epoch"] += 1
    for pid in [p for p, i in PID_MAP.items() if i["chat"] is ch]:
        PID_MAP.pop(pid, None)
    del CHATS[ch["id"]]
    if not CHATS:
        make_chat("Чат 1")
    save_chats()
    return web.json_response({"ok": True})

async def h_chat_rename(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    t = (d.get("title") or "").strip()
    if ch and t:
        ch["title"] = t[:60]
        save_chats()
    return web.json_response({"ok": bool(ch and t)})

async def h_stop(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    if ch:
        ch["cancel"] = True
        t = ch.get("task")
        if t and not t.done():
            t.cancel()
        if HTTP:
            try:
                async with HTTP.post(COMFY_URL + "/interrupt",
                                     timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    await resp.read()
            except Exception:
                pass
    return web.json_response({"ok": bool(ch)})

async def h_reset(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    if not ch:
        return web.json_response({"ok": False})
    ch["epoch"] += 1
    ch["cancel"] = True
    t = ch.get("task")
    if t and not t.done():
        t.cancel()
    ch["messages"], ch["items"], ch["turns"] = [], [], []
    ch["dialog"] = {"stage": "idle", "theme": "", "history": [], "refs": []}
    ch["last"] = {"theme": "", "shots": [], "ts": 0}
    ch["progress"] = None
    for pid in [p for p, i in PID_MAP.items() if i["chat"] is ch]:
        PID_MAP.pop(pid, None)
    save_chats()
    return web.json_response({"ok": True})

async def h_new(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    if not ch:
        return web.json_response({"ok": False})
    clear_last(ch)
    ch["dialog"] = {"stage": "idle", "theme": "", "history": [], "refs": []}
    say(ch, "b", "Прошлый кадр забыт. Следующий запрос будет как новая тема.")
    return web.json_response({"ok": True})

async def h_upload(r):
    if Image is None:
        return web.json_response({"ok": False, "error": "нужен Pillow: pip install pillow"}, status=500)
    try:
        reader = await r.multipart()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"не multipart: {e}"}, status=400)
    out = []
    async for part in reader:
        if part.name != "file":
            continue
        blob = await part.read(decode=False)
        try:
            im = Image.open(io.BytesIO(blob))
            im.thumbnail((2048, 2048), Image.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "transparency" in im.info else "RGB")
            fn = "src-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".png"
            im.save(ref_path(fn), "PNG")
            out.append({"file": fn, "url": "/uploads/" + fn, "w": im.width, "h": im.height})
            print("[upload]", fn, f"{im.width}x{im.height}")
        except Exception as e:
            out.append({"error": str(e)[:120]})
    return web.json_response({"ok": any("file" in o for o in out), "files": out})

async def h_ref_file(r):
    p = ref_path(r.match_info["name"])
    return web.FileResponse(p) if os.path.isfile(p) else web.Response(status=404)

async def h_chat(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    t = (d.get("text") or "").strip()
    refs = []
    for im in (d.get("images") or [])[:6]:
        fn = os.path.basename(str((im or {}).get("file") or ""))
        if not fn or not os.path.isfile(ref_path(fn)):
            continue
        role = str((im or {}).get("role") or "референс").lower()
        refs.append({"file": fn, "role": role if role in REF_ROLES else "референс"})
    if not ch or (not t and not refs):
        return web.json_response({"ok": False})
    if ch["busy"]:
        return web.json_response({"ok": False, "busy": True})
    if not t:
        t = "Сделай картинку по приложенным исходникам."
    if not ch["turns"] and re.match(r"^Чат \d+$", ch["title"] or ""):
        ch["title"] = t[:42] + ("…" if len(t) > 42 else "")
    say(ch, "u", t, images=refs)

    # ответы на вопросы — классификатор не нужен, это продолжение диалога
    if ch["dialog"]["stage"] == "wait":
        if t.lower() in ("генерируй", "пропусти", "skip", "давай", "сам"):
            t = "Уточнения пропускаю — прими все решения сам."
        launch(ch, run_series(ch, t, refs=refs))
        return web.json_response({"ok": True})

    # 1) готовый теговый промпт — ловим СТРУКТУРНО (формат, не слова-триггеры), без LLM
    if extract_prompt(t) and looks_like_prompt(t):
        launch(ch, run_series(ch, t, direct=True, refs=refs, intent="new"))
        return web.json_response({"ok": True})

    # 2) смысл сообщения понимает локальная модель
    cls = None
    try:
        cls = await asyncio.wait_for(classify_intent(HTTP, t, bool(last_shot(ch))), timeout=30)
    except Exception as e:
        print("[intent] классификатор сбоил, fallback:", e)
    if cls is None:                      # отказ/таймаут — консервативно, без триггеров
        if last_shot(ch) and not refs:
            launch(ch, run_series(ch, t, refs=refs, intent="edit"))
        else:
            launch(ch, run_clarify(ch, t, refs=refs))
        return web.json_response({"ok": True})

    is_edit = bool(last_shot(ch)) and cls["edit"] and not cls["new_topic"]

    if cls["raw_prompt"] and extract_prompt(t):      # «используй как есть» + теги внутри
        launch(ch, run_series(ch, t, direct=True, refs=refs, intent="new"))
    elif cls["no_questions"] or cls["freeform"]:     # «без вопросов» / «придумай сам»
        launch(ch, run_series(ch, t, no_questions=True, refs=refs,
                                       intent="edit" if is_edit else "new"))
    elif is_edit:                                    # правка готового кадра
        launch(ch, run_series(ch, t, refs=refs, intent="edit"))
    else:                                            # новая тема → уточнения
        launch(ch, run_clarify(ch, t, refs=refs))
    return web.json_response({"ok": True})

async def h_cfg_get(r):
    if r.query.get("refresh") or not ASSETS["loras"] or not ASSETS["upscales"]:
        ASSETS.update(ckpts=[], loras=[], upscales=[], ipa=False)
        await fetch_assets()
    return web.json_response({"config": CONFIG, "assets": ASSETS,
                              "resolutions": list(RESOLUTIONS.keys())})

async def h_cfg_set(r):
    d = await r.json()
    for k in ("ckpt", "resolution", "upscaler", "steps", "cfg", "max_images", "use_critic",
              "sampler", "scheduler", "clip_skip", "quality_on",
              "hires_on", "hires_scale", "hires_denoise", "hires_steps", "hires_cfg",
              "hires_sampler", "hires_scheduler", "hires_max_px", "save_dir", "feedback_on",
              "ref_mode", "ref_denoise", "ref_auto_size", "ref_cutout", "ref_ipadapter", "ref_ipa_weight"):
        if k in d:
            CONFIG[k] = d[k]
    if "fw_fh" in d and isinstance(d["fw_fh"], list) and len(d["fw_fh"]) == 2:
        CONFIG["fw_fh"] = [int(d["fw_fh"][0]), int(d["fw_fh"][1])]
    if "loras" in d and isinstance(d["loras"], list):
        CONFIG["loras"] = d["loras"][:10]
        while len(CONFIG["loras"]) < 10:
            CONFIG["loras"].append({"name": "", "strength": 0.8, "on": False})
    save_config(CONFIG)
    return web.json_response({"ok": True})

async def h_img(r):
    p = img_path(r.match_info["name"])
    return web.FileResponse(p) if os.path.isfile(p) else web.Response(status=404)

async def h_hw(r):
    data = await asyncio.get_running_loop().run_in_executor(None, collect_hw)
    return web.json_response(data)

# ===== HTML =====

HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>HERETIC · KoloVokPove$ilsa</title>
<style>
:root{
--ink900:#08050f;--ink850:#0d0819;--ink800:#130c22;--ink750:#18102c;
--ink700:#1e1636;--ink600:#2a1f4a;--line:#362a60;--soft:#241b42;
--txt:#f4ecff;--dim:#b7a9d6;--mute:#8a7ab0;
--mag:#ff2d95;--cyan:#22e0ff;--lime:#b6ff3d;--amber:#ffb020;--violet:#8b5cff;
--ok:#3ddc84;--warn:#ffb020;--bad:#ff4d5e;--info:#5aa9ff;
--hot:linear-gradient(100deg,#ff2d95 0%,#8b5cff 48%,#22e0ff 100%);
--cool:linear-gradient(100deg,#22e0ff 0%,#b6ff3d 100%);
--warm:linear-gradient(100deg,#ffb020 0%,#ff2d95 100%);
--top:60px;--chat:460px;--rail:334px;
--mono:ui-monospace,"Cascadia Mono",Consolas,"SF Mono",monospace;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--ink900);color:var(--txt);overflow:hidden;
font:14px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
body.dragging{user-select:none;cursor:col-resize}
body.dragging img,body.dragging .log,body.dragging .grid{pointer-events:none}
.bg{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.aur{position:absolute;border-radius:50%;filter:blur(80px);opacity:.42;mix-blend-mode:screen}
.a1{width:52vw;height:52vw;left:-12vw;top:-16vw;background:#ff2d95;animation:drift1 34s ease-in-out infinite}
.a2{width:46vw;height:46vw;right:-10vw;top:6vh;background:#22e0ff;animation:drift2 41s ease-in-out infinite}
.a3{width:40vw;height:40vw;left:34vw;bottom:-20vw;background:#8b5cff;animation:drift3 47s ease-in-out infinite}
@keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(9vw,7vh) scale(1.14)}}
@keyframes drift2{0%,100%{transform:translate(0,0) scale(1.05)}50%{transform:translate(-8vw,10vh) scale(.9)}}
@keyframes drift3{0%,100%{transform:translate(0,0) scale(.95)}50%{transform:translate(-6vw,-8vh) scale(1.12)}}
.grain{position:absolute;inset:0;opacity:.05;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
*{scrollbar-width:thin;scrollbar-color:#6b3fb8 var(--ink850)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:var(--ink850);border-left:1px solid var(--soft)}
::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#8b5cff,#ff2d95);border:2px solid var(--ink850);border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:linear-gradient(180deg,#22e0ff,#b6ff3d)}
.top{position:relative;z-index:20;height:var(--top);display:flex;align-items:center;gap:12px;
padding:0 14px;background:rgba(13,8,25,.86);backdrop-filter:blur(14px);
border-bottom:1px solid var(--line);animation:drop .5s cubic-bezier(.2,.8,.2,1) both}
.top:after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:1px;background:var(--hot);opacity:.75}
@keyframes drop{from{opacity:0;transform:translateY(-14px)}to{opacity:1;transform:none}}
.brand{display:flex;align-items:center;gap:10px;flex:0 0 auto}
.mark{width:34px;height:34px;border-radius:10px;background:var(--hot);display:grid;place-items:center;
font-weight:900;font-size:18px;color:#0b0714;box-shadow:0 0 22px rgba(255,45,149,.45)}
.bt b{display:block;font-size:15px;letter-spacing:.22em;line-height:1.1}
.bt span{display:block;font-size:9.5px;letter-spacing:.3em;color:var(--mute);text-transform:uppercase}
.status{display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:999px;
background:var(--ink750);border:1px solid var(--line);font-size:12px;color:var(--dim);flex:0 0 auto}
.status i{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 10px var(--ok)}
.status.busy{border-color:#6a3a8f;color:#ffd9f0}
.status.busy i{background:var(--mag);box-shadow:0 0 12px var(--mag);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}
.spacer{flex:1 1 auto;min-width:4px}
.hw{display:flex;gap:8px;align-items:stretch;overflow-x:auto;max-width:52vw;padding:2px 0;scrollbar-width:none}
.hw::-webkit-scrollbar{height:4px}
.chip{flex:0 0 auto;min-width:104px;padding:4px 9px;border-radius:9px;background:var(--ink750);
border:1px solid var(--soft);font-size:10.5px;line-height:1.35;position:relative;overflow:hidden}
.chip b{display:block;font-size:11px;font-weight:600;color:var(--txt);white-space:nowrap}
.chip s{display:block;text-decoration:none;color:var(--mute);white-space:nowrap;font-family:var(--mono)}
.chip u{position:absolute;left:0;bottom:0;height:2px;background:var(--cool);transition:width .5s ease}
.chip.hot u{background:var(--warm)}
.chip .t-ok{color:var(--lime)}.chip .t-warn{color:var(--amber)}.chip .t-bad{color:var(--bad)}
.chip.prog{min-width:170px;border-color:#3f6b23}
.chip.prog b{color:var(--lime)}
.icobtn{width:34px;height:34px;flex:0 0 auto;border-radius:9px;background:var(--ink750);color:var(--dim);
border:1px solid var(--line);cursor:pointer;font-size:15px;display:grid;place-items:center;transition:.18s}
.icobtn:hover{color:var(--txt);border-color:var(--mag);box-shadow:0 0 14px rgba(255,45,149,.3)}
.icobtn.on{color:var(--lime);border-color:#4c7a2a}
#railBtn{display:grid}
.panes{position:relative;z-index:10;display:flex;height:calc(100% - var(--top));overflow:hidden}
.pane{display:flex;flex-direction:column;min-width:0;background:rgba(19,12,34,.82);position:relative}
.chat{width:var(--chat);flex:0 0 auto;border-right:1px solid var(--line);animation:rise .6s .05s cubic-bezier(.2,.8,.2,1) both}
.gallery{flex:1 1 auto;min-width:200px;animation:rise .6s .13s cubic-bezier(.2,.8,.2,1) both}
.rail{width:var(--rail);flex:0 0 auto;border-left:1px solid var(--line);background:rgba(13,8,25,.9);
animation:rise .6s .21s cubic-bezier(.2,.8,.2,1) both}
@keyframes rise{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:none}}
.phead{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--soft);
background:linear-gradient(180deg,rgba(42,31,74,.55),transparent);flex:0 0 auto;min-height:44px}
.phead h2{margin:0;font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:var(--dim);font-weight:700}
.pill{font-size:10.5px;padding:3px 9px;border-radius:999px;background:var(--ink700);border:1px solid var(--line);
color:var(--mute);font-family:var(--mono);white-space:nowrap}
.pill.live{color:var(--lime);border-color:#3f6b23}
.pill.dirty{color:var(--amber);border-color:#7a5514}
.mini{background:var(--ink700);border:1px solid var(--line);color:var(--dim);border-radius:7px;
padding:4px 10px;font-size:11px;cursor:pointer;transition:.16s}
.mini:hover{color:var(--txt);border-color:var(--cyan)}
.mini.on{color:var(--lime);border-color:#4c7a2a}
.chattabs{display:flex;gap:4px;padding:6px 8px;border-bottom:1px solid var(--soft);
overflow-x:auto;flex:0 0 auto;background:rgba(8,5,15,.35);scrollbar-width:none}
.chattabs::-webkit-scrollbar{height:3px}
.ctab{flex:0 0 auto;display:flex;align-items:center;gap:6px;max-width:190px;
background:var(--ink750);border:1px solid var(--soft);color:var(--mute);border-radius:8px;
padding:4px 8px;font-size:11.5px;cursor:pointer;transition:.15s}
.ctab span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px}
.ctab:hover{color:var(--txt);border-color:var(--violet)}
.ctab.on{background:var(--ink600);color:var(--txt);border-color:var(--mag)}
.ctab em{font-style:normal;opacity:.55;font-size:13px;padding:0 2px;border-radius:4px}
.ctab em:hover{opacity:1;color:var(--bad);background:rgba(255,77,94,.15)}
.ctab.add{font-size:14px;font-weight:700;color:var(--cyan);padding:4px 11px}
.bdot{width:6px;height:6px;border-radius:50%;background:var(--mag);flex:0 0 auto;animation:pulse 1s infinite}
.grip{flex:0 0 auto;width:8px;cursor:col-resize;background:var(--ink850);display:grid;place-items:center;
border-right:1px solid var(--soft);transition:background .18s}
.grip i{width:2px;height:44px;border-radius:2px;background:var(--line);transition:.18s}
.grip:hover,.grip.act{background:linear-gradient(180deg,#8b5cff,#ff2d95)}
.grip:hover i,.grip.act i{background:#fff;height:70px}
.log{flex:1 1 auto;overflow-y:auto;overflow-x:hidden;padding:14px 12px 6px;scroll-behavior:auto}
.m{margin:0 0 10px;padding:9px 12px;border-radius:12px;font-size:13px;
white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
animation:pop .32s cubic-bezier(.2,.8,.2,1) both;position:relative;border:1px solid transparent}
@keyframes pop{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.m .who{display:block;font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;margin-bottom:4px;opacity:.75}
.m.u{background:linear-gradient(135deg,rgba(255,45,149,.18),rgba(139,92,255,.14));border-color:#6d2a63;margin-left:26px}
.m.u .who{color:var(--mag)}
.m.b{background:var(--ink750);border-color:var(--soft);margin-right:18px}
.m.b .who{color:var(--cyan)}
.m.e{background:rgba(255,77,94,.13);border-color:#7d2733}
.m.e .who{color:var(--bad)}
.m .thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
.m .thumbs .th{width:96px}
.m .thumbs img{width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid var(--line);cursor:zoom-in;display:block}
.m .thumbs s{display:block;text-decoration:none;font-size:9px;color:var(--mute);
text-transform:uppercase;letter-spacing:.08em;text-align:center;margin-top:2px}.logwrap{flex:1 1 auto;min-height:0;position:relative;display:flex}
.log{flex:1 1 auto;min-width:0;overflow-y:auto;overflow-x:hidden;padding:14px 12px 6px;scroll-behavior:auto}
.logempty{position:absolute;inset:0;display:none;padding:14px;overflow-y:auto;overscroll-behavior:contain}
.logempty.show{display:block}
.emptywrap{display:flex;flex-direction:column;justify-content:center;gap:12px;
width:100%;max-width:470px;margin:0 auto;min-height:100%}
.emptyhead{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center}
.emptyhead h3{margin:0;font-size:13px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;
background:var(--hot);-webkit-background-clip:text;background-clip:text;color:transparent}
.emptyhead p{margin:0;max-width:352px;color:var(--dim);font-size:12px;line-height:1.65}
.ideapanel{flex:0 1 auto;min-height:0;display:flex;flex-direction:column;gap:9px;padding:10px;
border-radius:14px;border:1px solid var(--line);
background:linear-gradient(165deg,rgba(42,31,74,.6),rgba(13,8,25,.78));
box-shadow:0 14px 40px rgba(0,0,0,.38)}
.ideatop{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.seg{display:flex;flex-wrap:wrap;gap:3px;padding:3px;border-radius:11px;
background:rgba(8,5,15,.6);border:1px solid var(--soft);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.seg button{display:flex;align-items:center;gap:6px;background:none;border:0;border-radius:8px;
color:var(--mute);font:600 11.5px/1 "Segoe UI",system-ui,sans-serif;padding:7px 9px;cursor:pointer;
transition:.18s;white-space:nowrap}
.seg button svg{width:13px;height:13px;flex:0 0 auto;opacity:.85}
.seg button:hover{color:var(--txt);background:var(--ink700)}
.seg button:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.seg button.on{color:#0b0714;box-shadow:0 3px 14px rgba(0,0,0,.45)}
.seg button.on svg{opacity:1}
.seg button[data-m="реализм"].on{background:linear-gradient(100deg,#b6ff3d,#22e0ff)}
.seg button[data-m="аниме"].on{background:linear-gradient(100deg,#ff2d95,#8b5cff);color:#fff}
.seg button[data-m="фантастика"].on{background:linear-gradient(100deg,#ffb020,#ff2d95)}
.seeds{flex:1 1 auto;min-height:72px;overflow-y:auto;display:flex;flex-direction:column;gap:7px;
padding:2px 6px 2px 2px;scrollbar-width:thin;scrollbar-color:#6b3fb8 rgba(8,5,15,.5)}
.seeds::-webkit-scrollbar{width:8px}
.seeds::-webkit-scrollbar-track{background:rgba(8,5,15,.5);border-radius:8px}
.seeds::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#8b5cff,#ff2d95);border-radius:8px;
border:2px solid rgba(8,5,15,.5)}
.seeds::-webkit-scrollbar-thumb:hover{background:linear-gradient(180deg,#22e0ff,#b6ff3d)}
.idea{display:flex;align-items:stretch;background:var(--ink750);border:1px solid var(--line);
border-radius:12px;overflow:hidden;transition:border-color .18s,transform .18s,background .18s;
animation:pop .3s cubic-bezier(.2,.8,.2,1) both}
.idea:hover{border-color:var(--mag);background:rgba(255,45,149,.10);transform:translateY(-2px)}
.idea.pinned{border-color:#4c7a2a;background:rgba(182,255,61,.07)}
.idea.pinned:hover{border-color:var(--lime);background:rgba(182,255,61,.12)}
.idea .n{flex:0 0 auto;width:26px;display:grid;place-items:center;font:10px/1 var(--mono);
color:var(--mute);background:rgba(8,5,15,.45);border-right:1px solid var(--soft)}
.idea.pinned .n{color:var(--lime)}
.idea .body{flex:1 1 auto;min-width:0;background:none;border:0;color:var(--txt);
text-align:justify;text-justify:inter-word;hyphens:auto;-webkit-hyphens:auto;
overflow-wrap:anywhere;padding:9px 11px;
font:12.5px/1.55 "Segoe UI",system-ui,sans-serif;cursor:pointer;transition:color .18s}
.idea .body:hover{color:#fff}
.idea .body:focus-visible{outline:2px solid var(--cyan);outline-offset:-2px}
.idea .acts{display:flex;flex-direction:column;gap:2px;padding:4px;border-left:1px solid var(--soft);flex:0 0 auto}
.idea .acts button{width:24px;height:22px;display:grid;place-items:center;background:none;border:0;
border-radius:6px;color:var(--mute);cursor:pointer;font-size:11px;line-height:1;transition:.15s}
.idea .acts button:hover{background:var(--ink600);color:var(--txt)}
.idea .acts button.on{color:var(--lime)}
.idea .acts button:focus-visible{outline:2px solid var(--cyan);outline-offset:1px}
.ideastat{flex:0 0 auto;display:flex;align-items:center;gap:8px;font:10.5px/1 var(--mono);color:var(--mute)}
.ideastat .ln{flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent)}
.ideastat em{font-style:normal;color:var(--cyan)}
#regenIdeasBtn{position:relative;z-index:5}
#regenIdeasBtn.stale{border-color:var(--lime);color:var(--lime);box-shadow:0 0 0 3px rgba(182,255,61,.12)}
#regenIdeasBtn[disabled]{opacity:.6;cursor:progress}
#regenIdeasBtn .sp{display:inline-block;animation:spin .9s linear infinite}
#ideaToggle.on{color:var(--amber);border-color:#7a5514}
#ideaToggle[disabled]{opacity:.45;cursor:default}
@keyframes spin{to{transform:rotate(360deg)}}
.logwrap .jump{bottom:14px}
@media (max-height:720px){.emptyhead .art{display:none}}
.qcard{margin:0 0 12px;border-radius:14px;border:1px solid #6b4a1f;overflow:hidden;
background:linear-gradient(160deg,rgba(255,176,32,.10),rgba(139,92,255,.08));
animation:pop .34s cubic-bezier(.2,.8,.2,1) both}
.qhead{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid rgba(255,176,32,.22)}
.qhead b{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--amber)}
.qbody{padding:6px 12px 10px}
.qrow{padding:8px 0;border-bottom:1px dashed rgba(255,255,255,.07)}
.qrow:last-child{border-bottom:0}
.qtext{font-size:12.5px;margin-bottom:6px;overflow-wrap:anywhere}
.qtext em{font-style:normal;color:var(--amber);font-family:var(--mono);margin-right:6px}
.opts{display:flex;flex-wrap:wrap;gap:5px}
.opt{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;
padding:4px 9px;font-size:11.5px;cursor:pointer;transition:.15s;overflow-wrap:anywhere}
.opt:hover{border-color:var(--cyan);color:var(--txt)}
.opt.sel{background:var(--cool);color:#08210f;border-color:transparent;font-weight:600}
.opt em{font-style:normal;opacity:.6;font-family:var(--mono);margin-right:5px}
.qcustom{margin-top:6px;width:100%;background:var(--ink850);border:1px solid var(--line);color:var(--txt);
border-radius:8px;padding:6px 9px;font-size:12px;font-family:inherit}
.qfoot{padding:9px 12px;border-top:1px solid rgba(255,176,32,.22);background:rgba(8,5,15,.4)}
.qprev{font-family:var(--mono);font-size:11.5px;color:var(--lime);min-height:17px;
overflow-wrap:anywhere;word-break:break-word;margin-bottom:8px}
.qacts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.qacts .cnt{font-size:10.5px;color:var(--mute);font-family:var(--mono);margin-left:auto}
.jump{position:absolute;left:50%;transform:translateX(-50%) translateY(8px);bottom:126px;z-index:6;
display:none;align-items:center;gap:7px;background:var(--hot);color:#0b0714;border:0;border-radius:999px;
padding:7px 15px;font-size:12px;font-weight:700;cursor:pointer;box-shadow:0 8px 26px rgba(255,45,149,.4)}
.jump.show{display:flex;animation:pop .25s both}
.jump span{background:rgba(0,0,0,.28);border-radius:999px;padding:1px 7px;font-family:var(--mono)}
.composer{flex:0 0 auto;border-top:1px solid var(--line);padding:10px 12px 11px;
background:linear-gradient(180deg,transparent,rgba(42,31,74,.4))}
.attbar{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px}
.att{position:relative;width:76px;height:76px;border-radius:10px;overflow:hidden;
border:1px solid var(--line);background:var(--ink850)}
.att img{width:100%;height:100%;object-fit:cover;display:block}
.att .role{position:absolute;left:0;right:0;bottom:0;border:0;cursor:pointer;
background:rgba(8,5,15,.78);color:var(--lime);font-size:9px;padding:2px 0;
letter-spacing:.06em;text-transform:uppercase}
.att .role:hover{background:var(--violet);color:#fff}
.att .del{position:absolute;right:2px;top:2px;width:18px;height:18px;border:0;border-radius:5px;
background:rgba(8,5,15,.7);color:#ff9aa5;cursor:pointer;font-size:12px;line-height:1}
.att .del:hover{background:var(--bad);color:#fff}
.chat.drop:after{content:'отпусти — прикреплю как исходник';position:absolute;inset:8px;z-index:40;
border:2px dashed var(--cyan);border-radius:14px;background:rgba(34,224,255,.08);
display:grid;place-items:center;color:var(--cyan);font-size:13px;pointer-events:none}
#inp{width:100%;display:block;resize:none;background:var(--ink850);color:var(--txt);
border:1px solid var(--line);border-radius:11px;padding:9px 12px;
font:13.5px/20px "Segoe UI",system-ui,sans-serif;
min-height:38px;max-height:150px;height:38px;overflow-y:auto;overflow-x:hidden;
white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;transition:border-color .18s,box-shadow .18s}
#inp:focus{outline:0;border-color:var(--mag);box-shadow:0 0 0 3px rgba(255,45,149,.16)}
#inp::placeholder{color:#6d5f92}
.cbar{display:flex;align-items:center;gap:8px;margin-top:8px}
.hint{font-size:10.5px;color:var(--mute)}
.count{font-size:10.5px;color:var(--mute);font-family:var(--mono)}
.send{background:var(--hot);color:#0b0714;border:0;border-radius:9px;padding:8px 16px;font-size:12.5px;
font-weight:800;cursor:pointer;transition:.18s;box-shadow:0 4px 18px rgba(139,92,255,.35)}
.send:hover{filter:brightness(1.12);transform:translateY(-1px)}
.send:disabled{opacity:.4;cursor:not-allowed;transform:none;filter:none}
.stop{background:rgba(255,77,94,.16);color:#ff9aa5;border:1px solid #7d2733;border-radius:9px;
padding:8px 12px;font-size:12px;cursor:pointer;transition:.18s}
.stop:hover{background:var(--bad);color:#fff}
.chips{display:flex;gap:5px}
.chips button{background:var(--ink750);border:1px solid var(--soft);color:var(--mute);border-radius:999px;
padding:4px 11px;font-size:11px;cursor:pointer;transition:.16s}
.chips button:hover{color:var(--txt)}
.chips button.on{background:var(--ink600);color:var(--txt);border-color:var(--violet)}
.grid{flex:1 1 auto;overflow-y:auto;padding:14px;display:grid;gap:14px;align-content:start;
grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--ink800);border:1px solid var(--line);border-radius:14px;overflow:hidden;
animation:pop .4s cubic-bezier(.2,.8,.2,1) both;transition:border-color .2s,transform .2s}
.card:hover{border-color:#5b3f9e;transform:translateY(-2px)}
.chead{display:flex;align-items:center;gap:8px;padding:8px 11px;border-bottom:1px solid var(--soft)}
.badge{font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:999px;font-weight:700}
.badge.wip{background:rgba(90,169,255,.16);color:var(--info)}
.badge.ok{background:rgba(61,220,132,.16);color:var(--ok)}
.badge.bad{background:rgba(255,77,94,.16);color:var(--bad)}
.badge.retry{background:rgba(255,176,32,.16);color:var(--amber)}
.seed{font-family:var(--mono);font-size:10.5px;color:var(--mute);cursor:pointer;border:1px dashed var(--line);
padding:2px 7px;border-radius:6px;background:none}
.seed:hover{color:var(--cyan);border-color:var(--cyan)}
.shot{position:relative;background:var(--ink850);aspect-ratio:16/9;display:grid;place-items:center;overflow:hidden}
.shot img{width:100%;height:100%;object-fit:contain;display:block;cursor:zoom-in}
.skel{position:absolute;inset:0;background:linear-gradient(100deg,#18102c 30%,#2a1f4a 50%,#18102c 70%);
background-size:220% 100%;animation:sweep 1.5s linear infinite}
@keyframes sweep{from{background-position:120% 0}to{background-position:-120% 0}}
.shot .lbl{position:relative;z-index:2;font-size:11px;color:var(--dim);letter-spacing:.12em;text-transform:uppercase;
background:rgba(8,5,15,.55);padding:4px 10px;border-radius:8px;font-family:var(--mono)}
.pbar{position:absolute;left:0;right:0;bottom:0;height:5px;background:rgba(255,255,255,.08);z-index:3}
.pbar i{display:block;height:100%;width:0;background:var(--cool);transition:width .45s ease}
.verdict{padding:8px 11px;font-size:11.5px;color:var(--dim);border-top:1px solid var(--soft);
overflow-wrap:anywhere;white-space:pre-wrap}
.verdict.good{color:#9ff0c4}.verdict.bad{color:#ffb3bb}
.srcs{display:flex;gap:4px;padding:6px 11px;border-top:1px solid var(--soft);align-items:center}
.srcs img{width:40px;height:40px;object-fit:cover;border-radius:6px;border:1px solid var(--soft);cursor:zoom-in}
.srcs s{text-decoration:none;font-size:9.5px;color:var(--mute);text-transform:uppercase;letter-spacing:.1em}
details.tags{border-top:1px solid var(--soft)}
details.tags summary{padding:7px 11px;font-size:11px;color:var(--mute);cursor:pointer;list-style:none;
display:flex;align-items:center;gap:7px}
details.tags summary::-webkit-details-marker{display:none}
details.tags summary:before{content:'▸';color:var(--violet);transition:.2s}
details.tags[open] summary:before{transform:rotate(90deg)}
details.tags summary:hover{color:var(--txt)}
.tagbox{max-height:150px;overflow-y:auto;padding:0 11px 10px;display:flex;flex-wrap:wrap;gap:4px}
.tag{font-family:var(--mono);font-size:10.5px;background:var(--ink750);border:1px solid var(--soft);
color:var(--dim);border-radius:6px;padding:2px 7px;cursor:pointer;transition:.14s}
.tag:hover{border-color:var(--lime);color:var(--lime)}
.cacts{display:flex;gap:6px;padding:9px 11px;border-top:1px solid var(--soft)}
.cacts button,.cacts a{flex:1;text-align:center;text-decoration:none;background:var(--ink750);
border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:6px 4px;font-size:11px;cursor:pointer;transition:.16s}
.cacts button:hover,.cacts a:hover{color:#fff;border-color:var(--cyan);background:rgba(34,224,255,.1)}
.galempty{position:absolute;inset:52px 0 0;display:none;flex-direction:column;align-items:center;
justify-content:center;gap:16px;text-align:center;padding:30px;pointer-events:none}
.galempty.show{display:flex}
.galempty h3{margin:0;font-size:17px;letter-spacing:.16em;text-transform:uppercase;
background:var(--hot);-webkit-background-clip:text;background-clip:text;color:transparent}
.galempty p{margin:0;color:var(--mute);font-size:12.5px;max-width:420px;line-height:1.8}
.railbody{flex:1 1 auto;overflow-y:auto;padding:4px 12px 14px}
.sec{padding:12px 0;border-bottom:1px solid var(--soft)}
.sec:last-child{border-bottom:0}
.sech{font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:var(--violet);font-weight:800;margin-bottom:9px}
.lab{display:block;font-size:11px;color:var(--mute);margin:9px 0 4px}
select,input[type=number]{width:100%;background:var(--ink850);color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:6px 9px;font-size:12px;font-family:inherit}
select:focus,input[type=number]:focus{outline:0;border-color:var(--cyan)}
.info{font-size:10.5px;color:var(--mute);font-family:var(--mono);margin-top:7px;line-height:1.6}
.presets{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.presets button{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;
padding:8px 4px;font-size:11px;cursor:pointer;transition:.16s}
.presets button:hover{color:#0b0714;background:var(--cool);border-color:transparent;font-weight:700}
.stepper2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stp{display:flex;align-items:center;gap:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--ink850)}
.stp button{width:26px;flex:0 0 auto;background:var(--ink700);border:0;color:var(--dim);cursor:pointer;
font-size:14px;padding:6px 0;transition:.15s}
.stp button:hover{background:var(--violet);color:#fff}
.stp input{border:0;border-radius:0;text-align:center;padding:6px 2px;-moz-appearance:textfield}
.stp input::-webkit-outer-spin-button,.stp input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.rng{margin:11px 0}
.rng label{display:flex;justify-content:space-between;font-size:11.5px;color:var(--dim);margin-bottom:6px}
.rng b{font-family:var(--mono);color:var(--lime)}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:5px;border-radius:5px;
background:linear-gradient(90deg,#8b5cff,#ff2d95);outline:0;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;
background:#fff;border:3px solid var(--violet);cursor:pointer;box-shadow:0 0 10px rgba(139,92,255,.7);transition:.15s}
input[type=range]::-webkit-slider-thumb:hover{border-color:var(--cyan);transform:scale(1.15)}
input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;background:#fff;
border:3px solid var(--violet);cursor:pointer}
.switch{display:flex;align-items:center;gap:10px;cursor:pointer;font-size:12px;color:var(--dim)}
.switch input{display:none}
.switch i{width:38px;height:21px;border-radius:999px;background:var(--ink600);border:1px solid var(--line);
position:relative;flex:0 0 auto;transition:.22s}
.switch i:after{content:'';position:absolute;left:2px;top:2px;width:15px;height:15px;border-radius:50%;
background:var(--mute);transition:.22s}
.switch input:checked+i{background:var(--cool);border-color:transparent}
.switch input:checked+i:after{left:19px;background:#08210f}
.lrow{padding:8px 0;border-bottom:1px dashed rgba(255,255,255,.06)}
.lrow:last-child{border-bottom:0}
.ltop{display:grid;grid-template-columns:38px 1fr 34px;gap:7px;align-items:center}
.ltop .num{font-family:var(--mono);font-size:10px;color:var(--mute);text-align:right}
.lrow input[type=range]{margin-top:7px;height:4px}
.lrow.off{opacity:.42}
.railfoot{flex:0 0 auto;display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:10px 12px;
border-top:1px solid var(--line);background:rgba(8,5,15,.55)}
.railfoot .primary{grid-column:1/-1;background:var(--hot);color:#0b0714;border:0;border-radius:9px;
padding:9px;font-size:12.5px;font-weight:800;cursor:pointer;transition:.18s}
.railfoot .primary:hover{filter:brightness(1.12)}
.railfoot .ghost{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;
padding:7px;font-size:11px;cursor:pointer;transition:.16s}
.railfoot .ghost:hover{color:var(--txt);border-color:var(--cyan)}
#railClose{display:block}
@media (min-width:1241px){body.rail-hide .rail{display:none}}
.lb{position:fixed;inset:0;z-index:90;display:none;background:rgba(5,3,10,.93);backdrop-filter:blur(8px)}
.lb.show{display:flex;animation:fade .22s both}
@keyframes fade{from{opacity:0}to{opacity:1}}
.lbimg{flex:1 1 auto;display:grid;place-items:center;padding:20px;min-width:0}
.lbimg img{max-width:100%;max-height:100%;object-fit:contain;border-radius:10px;
box-shadow:0 24px 80px rgba(0,0,0,.7);cursor:zoom-in}
.lbimg img.zoom{max-width:none;max-height:none;cursor:zoom-out}
.lbimg.wrap{overflow:auto;display:block}
.lbside{width:330px;flex:0 0 auto;border-left:1px solid var(--line);background:rgba(13,8,25,.95);
display:flex;flex-direction:column;padding:14px;gap:11px;overflow-y:auto}
.lbside h4{margin:0;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--violet)}
.lbmeta{font-family:var(--mono);font-size:11.5px;color:var(--dim);line-height:1.9}
.lbtags{flex:1 1 auto;overflow-y:auto;display:flex;flex-wrap:wrap;gap:4px;align-content:start;min-height:80px}
.lbacts{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.lbacts button,.lbacts a{background:var(--ink750);border:1px solid var(--line);color:var(--dim);
border-radius:8px;padding:8px;font-size:11.5px;cursor:pointer;text-align:center;text-decoration:none;transition:.16s}
.lbacts button:hover,.lbacts a:hover{color:#fff;border-color:var(--mag)}
.lbclose{position:absolute;right:14px;top:12px;z-index:2}
.lbnav{position:absolute;top:50%;transform:translateY(-50%);width:42px;height:64px;border-radius:10px;
background:rgba(26,16,48,.8);border:1px solid var(--line);color:var(--txt);font-size:20px;cursor:pointer;z-index:2}
.lbnav:hover{background:var(--violet)}
.lbprev{left:12px}.lbnext{right:352px}
.toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:120;display:flex;
flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{padding:9px 18px;border-radius:999px;font-size:12.5px;font-weight:600;color:#0b0714;
background:var(--cool);box-shadow:0 10px 34px rgba(0,0,0,.5);animation:pop .26s both}
.toast.warn{background:var(--warm)}
.toast.bad{background:linear-gradient(100deg,#ff4d5e,#ff2d95);color:#fff}
.tabs{display:none}
@media (max-width:1240px){
#railBtn{display:grid}
.rail{position:absolute;right:0;top:0;bottom:0;z-index:30;transform:translateX(100%);
transition:transform .28s cubic-bezier(.2,.8,.2,1);box-shadow:-20px 0 60px rgba(0,0,0,.6)}
body.rail-open .rail{transform:none}
#railClose{display:block}
.lbnext{right:12px}
.lbside{display:none}
}
@media (max-width:900px){
:root{--top:56px}
.bt span{display:none}
.hw{max-width:34vw}
.panes{height:calc(100% - var(--top) - 52px)}
.grip{display:none}
.chat{width:100%!important;border-right:0}
.pane{display:none}
.panes[data-view=chat] .chat{display:flex}
.panes[data-view=gallery] .gallery{display:flex}
.panes[data-view=rail] .rail{display:flex;position:static;transform:none;width:100%;box-shadow:none}
#railBtn{display:none}
.tabs{display:grid;grid-template-columns:repeat(3,1fr);height:52px;position:relative;z-index:20;
background:rgba(13,8,25,.95);border-top:1px solid var(--line)}
.tabs button{background:none;border:0;color:var(--mute);font-size:12px;cursor:pointer;
letter-spacing:.1em;text-transform:uppercase;position:relative}
.tabs button.on{color:var(--txt)}
.tabs button.on:after{content:'';position:absolute;left:22%;right:22%;top:0;height:2px;background:var(--hot)}
.jump{bottom:132px}
.grid{grid-template-columns:1fr}
}
@media (prefers-reduced-motion:reduce){
*{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style>
</head>
<body>
<div class="bg"><span class="aur a1"></span><span class="aur a2"></span><span class="aur a3"></span><div class="grain"></div></div>
<header class="top">
  <div class="brand"><div class="mark">H</div><div class="bt"><b>HERETIC</b><span>KoloBokPove$ilsa studio</span></div></div>
  <div class="status" id="status"><i></i><span id="statusText">готов</span></div>
  <div class="spacer"></div>
  <div class="chip prog" id="progChip" style="display:none"><b id="progT">генерация</b><s id="progL">—</s><u id="progU" style="width:0%"></u></div>
  <div class="hw" id="hw"></div>
  <button class="icobtn" id="soundBtn" title="Звук по завершении серии">🔕</button>
  <button class="icobtn" id="railBtn" title="Параметры">⚙</button>
</header>
<main class="panes" id="panes" data-view="chat">
  <section class="pane chat" id="chatPane">
  <div class="phead">
    <h2>Диалог</h2><span class="pill" id="modelPill">LM Studio</span>
    <div class="spacer"></div>
    <button class="mini" id="ideaToggle" title="Показать идеи">💡 идеи</button>
    <button class="mini" id="newBtn" title="Забыть последний кадр, начать новый сюжет">новая тема</button>
    <button class="mini" id="resetBtn" title="Полностью очистить этот чат">сброс</button>
  </div>
  <div class="chattabs" id="chatTabs"></div>
  <div class="logwrap">
    <div class="log" id="log"></div>
    <div class="logempty" id="logEmpty">
      <div class="emptywrap">
        <div class="emptyhead">
          <svg class="art" width="64" height="64" viewBox="0 0 72 72" fill="none">
            <defs><linearGradient id="gq" x1="0" y1="0" x2="72" y2="72">
              <stop stop-color="#ff2d95"/><stop offset=".5" stop-color="#8b5cff"/><stop offset="1" stop-color="#22e0ff"/>
            </linearGradient></defs>
            <rect x="7" y="14" width="58" height="40" rx="12" stroke="url(#gq)" stroke-width="2.5"/>
            <path d="M22 54l-4 10 14-10" stroke="url(#gq)" stroke-width="2.5" stroke-linejoin="round"/>
            <circle cx="26" cy="34" r="3.5" fill="#ff2d95"/><circle cx="36" cy="34" r="3.5" fill="#8b5cff"/>
            <circle cx="46" cy="34" r="3.5" fill="#22e0ff"/>
          </svg>
          <h3>С чего начнём</h3>
          <p>Опиши сцену словами или прикрепи исходники — фон и персонажа. Можно взять готовую идею ниже.</p>
        </div>
        <div class="ideapanel">
          <div class="ideatop">
            <div class="seg" id="ideaMode" role="radiogroup" aria-label="Режим генерации идей">
              <button type="button" data-m="реализм" role="radio" aria-checked="true" tabindex="0" title="Обычные люди, правдоподобные места">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 4l9 6.5"/><path d="M5 10v9h14v-9"/><path d="M10 19v-5h4v5"/></svg>
                <span>реализм</span>
              </button>
              <button type="button" data-m="аниме" role="radio" aria-checked="false" tabindex="-1" title="Аниме-повседневность, лёгкая стилизация">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/></svg>
                <span>аниме</span>
              </button>
              <button type="button" data-m="фантастика" role="radio" aria-checked="false" tabindex="-1" title="Фантастика, но один мир на сцену">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="11" r="5"/><path d="M5 14.5c-2.2.9-3.4 1.9-3 2.7.7 1.3 5.3.4 10.3-2s8.6-5.4 7.9-6.7c-.4-.8-2-.8-4.2-.2"/></svg>
                <span>фантастика</span>
              </button>
            </div>
            <button class="mini" id="regenIdeasBtn" title="Сгенерировать идеи в выбранном режиме">🎲 новые идеи</button>
          </div>
          <div class="seeds" id="seedIdeas"></div>
          <div class="ideastat"><span id="ideaCount" aria-live="polite">—</span><i class="ln"></i><em id="ideaStamp"></em></div>
        </div>
      </div>
    </div>
    <button class="jump" id="jumpBtn">↓ новые сообщения <span id="jumpN">0</span></button>
  </div>
  <div class="composer">
    <div class="attbar" id="attBar"></div>
    <textarea id="inp" rows="1" spellcheck="false" placeholder="Опиши сцену… Enter — отправить, Shift+Enter — перенос строки"></textarea>
    <div class="cbar">
      <button class="mini" id="attBtn" title="Исходники: фон, персонаж, референс (drag&drop, Ctrl+V)"><span id="attIco">📎</span><span id="attCount"></span></button>
      <input type="file" id="attInput" accept="image/*" multiple hidden>
      <span class="hint">Enter — отправить · Shift+Enter — перенос</span>
      <div class="spacer"></div>
      <span class="count" id="count">0</span>
      <button class="send" id="sendBtn">Отправить</button>
      <button class="stop" id="stopBtn">Стоп</button>
    </div>
  </div>
</section>
  <div class="grip" id="grip" title="Тяни — ширина чата. Двойной клик — сброс"><i></i></div>
  <section class="pane gallery" id="galleryPane">
    <div class="phead">
      <h2>Серия</h2><span class="pill" id="galCount">0</span>
      <div class="spacer"></div>
      <div class="chips" id="filters">
        <button data-f="all" class="on">все</button>
        <button data-f="wip">в работе</button>
        <button data-f="ok">готово</button>
        <button data-f="bad">брак</button>
      </div>
    </div>
    <div class="grid" id="grid"></div>
    <div class="galempty show" id="galEmpty">
      <svg width="120" height="86" viewBox="0 0 120 86" fill="none">
        <defs><linearGradient id="gg" x1="0" y1="0" x2="120" y2="86">
          <stop stop-color="#b6ff3d"/><stop offset=".55" stop-color="#22e0ff"/><stop offset="1" stop-color="#8b5cff"/>
        </linearGradient></defs>
        <rect x="4" y="10" width="52" height="66" rx="8" stroke="url(#gg)" stroke-width="2.2"/>
        <rect x="64" y="10" width="52" height="30" rx="8" stroke="url(#gg)" stroke-width="2.2" opacity=".65"/>
        <rect x="64" y="46" width="52" height="30" rx="8" stroke="url(#gg)" stroke-width="2.2" opacity=".4"/>
        <path d="M10 62l14-18 10 12 8-9 10 15z" fill="url(#gg)" opacity=".55"/>
        <circle cx="42" cy="26" r="5" fill="#ffb020"/>
      </svg>
      <h3>Холст пуст</h3>
      <p>Здесь появятся кадры серии: статус, seed, вердикт критика и все теги промпта. Во время генерации виден шаг сэмплера и процент.</p>
    </div>
  </section>
  <aside class="pane rail" id="rail">
    <div class="phead">
      <h2>Параметры</h2><span class="pill live" id="saveState">сохранено</span>
      <div class="spacer"></div>
      <button class="mini" id="railClose">×</button>
    </div>
    <div class="railbody">
      <div class="sec">
        <div class="sech">Пресеты</div>
        <div class="presets" id="presets">
          <button data-p="draft">Черновик</button>
          <button data-p="final">Финал</button>
          <button data-p="max">Максимум</button>
        </div>
      </div>
      <div class="sec">
        <div class="sech">Модель</div>
        <label class="lab">Чекпоинт</label>
        <select id="ckpt"></select>
        <div class="info" id="assetsInfo">списки не загружены</div>
      </div>
      <div class="sec">
        <div class="sech">Холст</div>
        <label class="lab">Разрешение генерации (SDXL)</label>
        <select id="res"></select>
        <label class="lab">Финал после апскейла · Ш × В</label>
        <div class="stepper2">
          <div class="stp"><button data-t="fw" data-d="-8">−</button><input type="number" id="fw" min="256" step="8"><button data-t="fw" data-d="8">+</button></div>
          <div class="stp"><button data-t="fh" data-d="-8">−</button><input type="number" id="fh" min="256" step="8"><button data-t="fh" data-d="8">+</button></div>
        </div>
        <label class="lab">Апскейлер</label>
        <select id="up"></select>
      </div>
      <div class="sec">
        <div class="sech">Сэмплер</div>
        <div class="rng"><label>Шаги <b id="stepsV">45</b></label><input type="range" id="steps" min="4" max="150" step="1"></div>
        <div class="rng"><label>CFG scale <b id="cfgV">5.5</b></label><input type="range" id="cfg" min="1" max="15" step="0.1"></div>
        <div class="rng"><label>Картинок в серии <b id="maxV">3</b></label><input type="range" id="max" min="1" max="10" step="1"></div>
      </div>
      <div class="sec">
        <div class="sech">Контроль качества</div>
        <label class="switch"><input type="checkbox" id="crit"><i></i><span>Осмотр картинки: анатомия и замысел</span>label>
      </div>
      <div class="sec">
        <div class="sech">Исходники · img2img</div>
        <label class="lab">Что делать с картинками</label>
        <select id="refMode">
          <option value="auto">авто</option>
          <option value="tags">только описание → теги</option>
          <option value="img2img">img2img (одна основа)</option>
          <option value="composite">коллаж фон+персонаж → img2img</option>
        </select>
        <div class="rng"><label>Сила перерисовки · denoise <b id="refDenoiseV">0.55</b></label>
          <input type="range" id="refDenoise" min="0.15" max="0.95" step="0.05"></div>
        <label class="switch"><input type="checkbox" id="refAutoSize"><i></i><span>Холст по пропорциям исходника</span></label>
        <label class="switch" style="margin-top:8px"><input type="checkbox" id="refCutout"><i></i><span>Вырезать однотонный фон персонажа</span></label>
        <label class="switch" style="margin-top:8px"><input type="checkbox" id="refIpa"><i></i><span>IPAdapter по персонажу</span></label>
        <div class="info" id="ipaInfo">IPAdapter: —</div>
      </div>
      <div class="sec">
        <div class="sech">LoRA · 10 слотов</div>
        <div id="loras"></div>
      </div>
    </div>
    <div class="railfoot">
      <button class="primary" id="saveBtn">Сохранить сейчас</button>
      <button class="ghost" id="refreshBtn">Обновить списки</button>
      <button class="ghost" id="revertBtn">Отменить правки</button>
      <button class="ghost" id="exportBtn">Экспорт JSON</button>
      <button class="ghost" id="randBtn">Случайный набор</button>
    </div>
  </aside>
</main>
<nav class="tabs" id="tabs">
  <button data-v="chat" class="on">Диалог</button>
  <button data-v="gallery">Серия</button>
  <button data-v="rail">Параметры</button>
</nav>
<div class="lb" id="lb">
  <button class="icobtn lbclose" id="lbClose">✕</button>
  <button class="lbnav lbprev" id="lbPrev">‹</button>
  <button class="lbnav lbnext" id="lbNext">›</button>
  <div class="lbimg" id="lbWrap"><img id="lbImg" alt=""></div>
  <div class="lbside">
    <h4>Кадр</h4>
    <div class="lbmeta" id="lbMeta"></div>
    <h4>Теги</h4>
    <div class="lbtags" id="lbTags"></div>
    <div class="lbacts">
      <button id="lbCopy">Копировать промпт</button>
      <a id="lbDl" download>Скачать</a>
    </div>
  </div>
</div>
<div class="toasts" id="toasts"></div>
<script>
(function(){
'use strict';
var $=function(id){return document.getElementById(id);};
var log=$('log'),grid=$('grid'),inp=$('inp');
var activeId=null;
try{activeId=localStorage.getItem('heretic.chat')||null;}catch(e){}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function hash(s){var h=5381,i;for(i=0;i<s.length;i++){h=((h<<5)+h+s.charCodeAt(i))|0;}return 'k'+(h>>>0);}
function toast(msg,kind){
  var t=document.createElement('div');t.className='toast'+(kind?' '+kind:'');t.textContent=msg;
  $('toasts').appendChild(t);
  setTimeout(function(){t.style.transition='opacity .3s,transform .3s';t.style.opacity='0';t.style.transform='translateY(8px)';
    setTimeout(function(){if(t.parentNode)t.parentNode.removeChild(t);},320);},2400);
}
function copy(text,label){
  var done=function(){toast(label||'Скопировано');};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done,function(){fallback();});
  }else{fallback();}
  function fallback(){
    var ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');done();}catch(e){toast('Не удалось скопировать','bad');}
    document.body.removeChild(ta);
  }
}
function node(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}
function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(function(r){return r.json();});}
/* звук */
var soundOn=false,actx=null;
try{soundOn=localStorage.getItem('heretic.sound')==='1';}catch(e){}
function paintSound(){$('soundBtn').textContent=soundOn?'🔔':'🔕';$('soundBtn').classList.toggle('on',soundOn);}
function chime(){
  if(!soundOn)return;
  try{
    if(!actx)actx=new (window.AudioContext||window.webkitAudioContext)();
    if(actx.state==='suspended')actx.resume();
    var t=actx.currentTime;
    [[880,0],[1318.5,.13]].forEach(function(p){
      var o=actx.createOscillator(),g=actx.createGain();
      o.type='sine';o.frequency.value=p[0];
      g.gain.setValueAtTime(0,t+p[1]);g.gain.linearRampToValueAtTime(.16,t+p[1]+.02);
      g.gain.exponentialRampToValueAtTime(.0001,t+p[1]+.5);
      o.connect(g);g.connect(actx.destination);o.start(t+p[1]);o.stop(t+p[1]+.55);
    });
  }catch(e){}
}
$('soundBtn').addEventListener('click',function(){
  soundOn=!soundOn;try{localStorage.setItem('heretic.sound',soundOn?'1':'0');}catch(e){}
  paintSound();if(soundOn)chime();
});
paintSound();
/* лог */
var rendered=[],pinned=true,unread=0,lockUntil=0,busy=false;
function toBottom(){lockUntil=performance.now()+140;log.scrollTop=log.scrollHeight;}
function updateJump(){
  var b=$('jumpBtn');b.classList.toggle('show',!pinned&&unread>0);$('jumpN').textContent=unread;
}
log.addEventListener('scroll',function(){
  if(performance.now()<lockUntil)return;
  var gap=log.scrollHeight-log.scrollTop-log.clientHeight;
  pinned=gap<48;
  if(pinned){unread=0;}
  updateJump();
},{passive:true});
$('jumpBtn').addEventListener('click',function(){pinned=true;unread=0;updateJump();toBottom();});
function sameMsg(a,b){
  return !!a&&!!b&&a.role===b.role&&a.text===b.text&&
    JSON.stringify(a.images||[])===JSON.stringify(b.images||[]);
}
function syncLog(msgs){
  msgs=Array.isArray(msgs)?msgs:[];
  var prev=rendered,s=prev.length,n=0,k,cnt,ok,i;
  var maxS=Math.min(prev.length,msgs.length);
  for(k=0;k<=maxS;k++){
    cnt=Math.min(prev.length-k,msgs.length);ok=true;
    for(i=0;i<cnt;i++){if(!sameMsg(prev[k+i],msgs[i])){ok=false;break;}}
    if(ok){s=k;n=cnt;break;}
  }
  for(i=0;i<s;i++){if(log.firstChild)log.removeChild(log.firstChild);}
  var add=msgs.slice(n);
  if(add.length){
    var frag=document.createDocumentFragment();
    add.forEach(function(m){frag.appendChild(msgNode(m));});
    log.appendChild(frag);
    if(pinned){toBottom();}else{unread+=add.length;updateJump();}
  }else if(s>0&&pinned){toBottom();}
  rendered=msgs.map(function(m){return{role:m.role,text:m.text,images:m.images||[]};});
  syncEmptyState();
}
var WHO={u:'вы',b:'арт-директор',e:'ошибка',q:'уточнение'};
function msgNode(m){
  if(m.role==='q'){
    var qs=parseQuestions(m.text);
    if(qs)return questionCard(m.text,qs);
  }
  var d=node('div','m '+(m.role||'b'));
  d.appendChild(node('span','who',WHO[m.role]||'система'));
  d.appendChild(document.createTextNode(m.text||''));
  if(m.images&&m.images.length){
    var th=node('div','thumbs');
    m.images.forEach(function(r){
      var w=node('div','th'),im=document.createElement('img');
      im.src='/uploads/'+encodeURIComponent(r.file);im.loading='lazy';
      im.addEventListener('click',function(){window.open(im.src,'_blank');});
      w.appendChild(im);w.appendChild(node('s',null,r.role||''));
      th.appendChild(w);
    });
    d.appendChild(th);
  }
  return d;
}
/* вопросы */
function optsFrom(line){
  var re=/(\d+)\)\s*/g,hits=[],mm;
  while((mm=re.exec(line))!==null){hits.push({n:parseInt(mm[1],10),i:mm.index,e:re.lastIndex});}
  if(hits.length<2)return null;
  var list=hits.map(function(h,k){
    var end=(k+1<hits.length)?hits[k+1].i:line.length;
    return{n:h.n,text:line.slice(h.e,end).trim()};
  }).filter(function(o){return o.text.length>0;});
  return list.length>=2?{opts:list,start:hits[0].i}:null;
}
function parseQuestions(t){
  var lines=String(t||'').split(/\r?\n/),qs=[],cur=null,i;
  for(i=0;i<lines.length;i++){
    var line=lines[i].trim();if(!line)continue;
    var mq=line.match(/^(\d+)\.\s+(.+)$/);
    if(mq){
      var rest=mq[2],o=optsFrom(rest);
      cur={n:parseInt(mq[1],10),q:(o?rest.slice(0,o.start):rest).trim(),options:o?o.opts:[]};
      qs.push(cur);continue;
    }
    var o2=optsFrom(line);
    if(o2&&cur&&!cur.options.length){cur.options=o2.opts;}
  }
  qs=qs.filter(function(q){return q.q;});
  return qs.length>=2?qs:null;
}
var answers={};
function isCustom(txt){return /свой вариант|своё|свое|другое|другой/i.test(txt);}
function buildAnswer(st,qs){
  var parts=[];
  qs.forEach(function(q){
    var c=st.choices[q.n];if(!c)return;
    if(c.custom){var v=(st.text[q.n]||'').trim();if(v)parts.push(q.n+'-'+v);}
    else parts.push(q.n+'-'+c.n);
  });
  return parts.join(', ');
}
function questionCard(text,qs){
  var key=hash(text);
  if(!answers[key])answers[key]={choices:{},text:{}};
  var st=answers[key];
  var card=node('div','qcard');
  var head=node('div','qhead');
  head.appendChild(node('b',null,'Уточнение · '+qs.length+' вопросов'));
  head.appendChild(node('div','spacer'));
  var solo=node('button','mini','Решу сам');head.appendChild(solo);
  card.appendChild(head);
  var body=node('div','qbody');
  var prev=node('div','qprev'),cnt=node('span','cnt');
  qs.forEach(function(q){
    var row=node('div','qrow');
    var qt=node('div','qtext');
    qt.appendChild(node('em',null,q.n+'.'));qt.appendChild(document.createTextNode(q.q));
    row.appendChild(qt);
    var box=node('div','opts'),custom=null;
    if(!q.options.length){
      custom=node('input','qcustom');custom.type='text';custom.placeholder='свой ответ…';
      custom.value=st.text[q.n]||'';
      custom.addEventListener('input',function(){
        st.text[q.n]=custom.value;
        st.choices[q.n]=custom.value.trim()?{custom:true}:undefined;
        if(!custom.value.trim())delete st.choices[q.n];
        refresh();
      });
      row.appendChild(custom);
    }
    q.options.forEach(function(o){
      var b=node('button','opt');
      b.appendChild(node('em',null,o.n+')'));
      b.appendChild(document.createTextNode(o.text));
      var cust=isCustom(o.text),field=null;
      if(cust){
        field=node('input','qcustom');field.type='text';field.placeholder='напиши свой вариант…';
        field.value=st.text[q.n]||'';field.style.display='none';
        field.addEventListener('input',function(){st.text[q.n]=field.value;refresh();});
        field.addEventListener('keydown',function(e){e.stopPropagation();});
      }
      b.addEventListener('click',function(){
        var cur=st.choices[q.n];
        if(cur&&cur.n===o.n){delete st.choices[q.n];}
        else{st.choices[q.n]={n:o.n,custom:cust};}
        paint();refresh();if(pinned)toBottom();
      });
      function paint(){
        var sel=st.choices[q.n]&&st.choices[q.n].n===o.n;
        b.classList.toggle('sel',!!sel);
        if(field)field.style.display=sel?'block':'none';
      }
      b._paint=paint;
      box.appendChild(b);
      if(field)row.appendChild(field);
    });
    row.appendChild(box);
    body.appendChild(row);
  });
  card.appendChild(body);
  var foot=node('div','qfoot');
  foot.appendChild(prev);
  var acts=node('div','qacts');
  var toInp=node('button','mini','В поле ввода');
  var go=node('button','send','Отправить ответы');
  acts.appendChild(toInp);acts.appendChild(go);acts.appendChild(cnt);
  foot.appendChild(acts);card.appendChild(foot);
  function refresh(){
    var a=buildAnswer(st,qs);
    prev.textContent=a||'— выбери варианты кликом —';
    cnt.textContent='отвечено '+Object.keys(st.choices).length+' / '+qs.length;
  }
  toInp.addEventListener('click',function(){
    var a=buildAnswer(st,qs);if(!a){toast('Сначала выбери варианты','warn');return;}
    inp.value=a;autoGrow();updateCount();inp.focus();toast('Ответы в поле ввода');
  });
  go.addEventListener('click',function(){send(buildAnswer(st,qs)||'генерируй');});
  solo.addEventListener('click',function(){send('генерируй');});
  Array.prototype.forEach.call(body.querySelectorAll('.opt'),function(b){if(b._paint)b._paint();});
  refresh();
  return card;
}
/* исходники */
var atts=[],ROLES=['фон','персонаж','референс'],uploading=0;
function paintAtts(){
  var bar=$('attBar');if(!bar)return;bar.innerHTML='';
  atts.forEach(function(a,i){
    var d=node('div','att');
    var im=document.createElement('img');im.src=a.url;d.appendChild(im);
    var rl=node('button','role',a.role);rl.title='Сменить роль';
    rl.addEventListener('click',function(){a.role=ROLES[(ROLES.indexOf(a.role)+1)%ROLES.length];paintAtts();});
    var x=node('button','del','×');x.title='Убрать';
    x.addEventListener('click',function(){atts.splice(i,1);paintAtts();});
    d.appendChild(rl);d.appendChild(x);bar.appendChild(d);
  });
  $('attBtn').classList.toggle('on',atts.length>0);
  $('attCount').textContent=atts.length||'';
  $('attIco').textContent=uploading?'⏳':'📎';
}
async function uploadFiles(files){
  var list=Array.prototype.filter.call(files||[],function(f){return /^image\//.test(f.type);});
  if(!list.length)return;
  for(var i=0;i<list.length;i++){
    var fd=new FormData();fd.append('file',list[i],list[i].name||'paste.png');
    uploading++;paintAtts();
    try{
      var j=await (await fetch('/api/upload',{method:'POST',body:fd})).json();
      if(j.error)toast('Загрузка: '+j.error,'bad');
      (j.files||[]).forEach(function(f){
        if(!f.file){toast('Не принят: '+(f.error||'?'),'bad');return;}
        atts.push({file:f.file,url:f.url,
          role:atts.length===0?'фон':(atts.length===1?'персонаж':'референс')});
      });
    }catch(e){toast('Загрузка не удалась','bad');}
    finally{uploading--;paintAtts();}
  }
  if(atts.length)toast('Исходников: '+atts.length+' · роль меняется кликом');
}
$('attBtn').addEventListener('click',function(){$('attInput').click();});
$('attInput').addEventListener('change',function(){uploadFiles(this.files);this.value='';});
inp.addEventListener('paste',function(e){
  var f=e.clipboardData&&e.clipboardData.files;
  if(f&&f.length){e.preventDefault();uploadFiles(f);}
});
['dragenter','dragover'].forEach(function(ev){$('chatPane').addEventListener(ev,function(e){
  e.preventDefault();$('chatPane').classList.add('drop');});});
['dragleave','drop'].forEach(function(ev){$('chatPane').addEventListener(ev,function(e){
  e.preventDefault();$('chatPane').classList.remove('drop');});});
$('chatPane').addEventListener('drop',function(e){
  if(e.dataTransfer&&e.dataTransfer.files)uploadFiles(e.dataTransfer.files);});
window.addEventListener('dragover',function(e){e.preventDefault();});
window.addEventListener('drop',function(e){e.preventDefault();});
paintAtts();
/* композер */
function autoGrow(){
  inp.style.height='auto';
  var h=Math.min(inp.scrollHeight,150);
  inp.style.height=Math.max(38,h)+'px';
}
function updateCount(){$('count').textContent=inp.value.length;}
inp.addEventListener('input',function(){autoGrow();updateCount();});
inp.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey&&!e.ctrlKey&&!e.altKey){e.preventDefault();send();}
});
autoGrow();
async function send(txt){
  var t=(txt!=null?String(txt):inp.value).trim();
  if(!t&&!atts.length)return;
  if(busy){toast('Этот чат занят — дождись ответа или открой другой чат','warn');return;}
  if(!activeId){toast('Чат не выбран','bad');return;}
  if(uploading){toast('Дождись загрузки файлов','warn');return;}
  var imgs=atts.map(function(a){return{file:a.file,role:a.role};});
  if(txt==null){inp.value='';autoGrow();updateCount();}
  pinned=true;unread=0;updateJump();
  try{
    var j=await post('/api/chat',{id:activeId,text:t,images:imgs});
    if(!j.ok){toast('Не принято: идёт генерация','warn');}
    else{atts=[];paintAtts();tick();}
  }catch(e){toast('Нет связи с мостом','bad');}
}
$('sendBtn').addEventListener('click',function(){send();});
$('stopBtn').addEventListener('click',async function(){
  try{await post('/api/stop',{id:activeId});toast('Останавливаю после текущего кадра','warn');}catch(e){toast('Нет связи','bad');}
});
$('resetBtn').addEventListener('click',async function(){
  try{
    var j=await post('/api/reset',{id:activeId});
    if(j.ok){
      rendered=[];log.innerHTML='';items=[];galSig='';grid.innerHTML='';answers={};
      atts=[];paintAtts();
      pinned=true;unread=0;updateJump();tick();toast('Чат очищен');
    }
  }catch(e){toast('Нет связи','bad');}
});
$('newBtn').addEventListener('click',async function(){
  try{await post('/api/new',{id:activeId});toast('Начинаем новый сюжет');tick();}catch(e){toast('Нет связи','bad');}
});
/* чаты: вкладки */
function clearChatView(){
  rendered=[];log.innerHTML='';items=[];galSig='';galN=0;grid.innerHTML='';
  atts=[];paintAtts();
  pinned=true;unread=0;updateJump();wasBusy=false;
}
function switchChat(id){
  activeId=id;
  try{localStorage.setItem('heretic.chat',id);}catch(e){}
  clearChatView();tick();
}
async function newChat(){
  try{
    var j=await post('/api/chats/create',{});
    if(j.ok){switchChat(j.id);toast('Новый чат создан');}
  }catch(e){toast('Нет связи','bad');}
}
async function delChat(c){
  if(!confirm('Удалить чат «'+(c.title||'')+'»?'))return;
  try{
    var j=await post('/api/chats/delete',{id:c.id});
    if(j.ok){
      if(c.id===activeId){activeId=null;clearChatView();}
      tick();toast('Чат удалён');
    }
  }catch(e){toast('Нет связи','bad');}
}
async function renameChat(c){
  var t=prompt('Название чата:',c.title||'');
  if(t==null)return;t=t.trim();if(!t)return;
  try{await post('/api/chats/rename',{id:c.id,title:t});tick();}catch(e){}
}
function renderTabs(list){
  var box=$('chatTabs');box.innerHTML='';
  (list||[]).forEach(function(c){
    var b=node('button','ctab'+(c.id===activeId?' on':''));
    b.title=c.title||'чат';
    if(c.busy&&c.id!==activeId)b.appendChild(node('i','bdot'));
    b.appendChild(node('span',null,c.title||'чат'));
    var x=node('em',null,'×');x.title='Удалить чат';
    x.addEventListener('click',function(e){e.stopPropagation();delChat(c);});
    b.appendChild(x);
    b.addEventListener('click',function(){if(c.id!==activeId)switchChat(c.id);});
    b.addEventListener('dblclick',function(){renameChat(c);});
    box.appendChild(b);
  });
  var add=node('button','ctab add','+');add.title='Новый чат';
  add.addEventListener('click',newChat);
  box.appendChild(add);
}
/* идеи: режим, закрепление, отправка в поле */
var IDEAS={{IDEAS}};
var IDEA_MKEY='heretic.ideaMode',IDEA_PKEY='heretic.ideaPins';
var IDEA_ORDER=['реализм','аниме','фантастика'];
var ideaMode='реализм',pins=[],listMode=null,regenBusy=false,ideasOpen=false,lastMsgCount=0;
try{var _m=localStorage.getItem(IDEA_MKEY);if(_m&&IDEA_ORDER.indexOf(_m)>=0)ideaMode=_m;}catch(e){}
try{var _p=JSON.parse(localStorage.getItem(IDEA_PKEY)||'[]');
  if(Array.isArray(_p))pins=_p.filter(function(x){return typeof x==='string'&&x.trim();}).slice(0,24);}catch(e){}
function savePins(){try{localStorage.setItem(IDEA_PKEY,JSON.stringify(pins.slice(0,24)));}catch(e){}}
function isPinned(s){return pins.indexOf(s)>=0;}
function syncEmptyState(){
  var box=$('logEmpty');if(!box)return;
  var empty=rendered.length===0;
  if(rendered.length>lastMsgCount)ideasOpen=false;
  lastMsgCount=rendered.length;
  box.classList.toggle('show',empty||ideasOpen);
  var t=$('ideaToggle');
  if(t){
    t.classList.toggle('on',empty||ideasOpen);
    t.disabled=empty;
    t.title=empty?'Идеи показаны — чат пуст':(ideasOpen?'Скрыть идеи':'Показать идеи');
  }
}
function ideaStats(n){
  $('ideaCount').textContent=n?(n+' идей · '+(listMode||'стартовый набор')+' · ★ '+pins.length):'идей нет';
}
function stampNow(){
  $('ideaStamp').textContent='обновлено '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
}
function paintIdeaMode(){
  var box=$('ideaMode');if(!box)return;
  Array.prototype.forEach.call(box.querySelectorAll('button'),function(b){
    var on=b.dataset.m===ideaMode;
    b.classList.toggle('on',on);
    b.setAttribute('aria-checked',on?'true':'false');
    b.tabIndex=on?0:-1;
  });
  var btn=$('regenIdeasBtn');
  btn.title='Сгенерировать идеи · режим «'+ideaMode+'»';
  btn.classList.toggle('stale',!!listMode&&listMode!==ideaMode);
}
function setIdeaMode(m,focus){
  if(IDEA_ORDER.indexOf(m)<0)return;
  ideaMode=m;
  try{localStorage.setItem(IDEA_MKEY,m);}catch(e){}
  paintIdeaMode();
  if(focus){var b=$('ideaMode').querySelector('button[data-m="'+m+'"]');if(b)b.focus();}
}
function renderSeedIdeas(focusIdx){
  var box=$('seedIdeas');if(!box)return;
  var top=box.scrollTop,list=[],seen={};
  pins.concat(IDEAS||[]).forEach(function(s){
    s=String(s||'').trim();
    if(!s||seen[s])return;
    seen[s]=1;list.push(s);
  });
  box.innerHTML='';
  list.forEach(function(s,i){
    var pinned=isPinned(s);
    var row=node('div','idea'+(pinned?' pinned':''));
    row.dataset.i=i;
    row.style.animationDelay=Math.min(i*26,220)+'ms';
    row.appendChild(node('span','n',('0'+(i+1)).slice(-2)));
    var body=node('button','body',s);
    body.type='button';body.title='Подставить в поле ввода';
    body.addEventListener('click',function(){
      inp.value=s;autoGrow();updateCount();
      ideasOpen=false;syncEmptyState();
      inp.focus();toast('Идея в поле ввода');
    });
    var acts=node('div','acts');
    var pin=node('button',pinned?'on':null,pinned?'★':'☆');
    pin.type='button';
    pin.title=pinned?'Открепить':'Закрепить — переживёт новые генерации';
    pin.setAttribute('aria-label',pin.title);
    pin.addEventListener('click',function(){
      var k=pins.indexOf(s);
      if(k>=0){pins.splice(k,1);toast('Откреплено');}
      else{pins.unshift(s);pins=pins.slice(0,24);toast('Закреплено ★');}
      savePins();renderSeedIdeas(i);
    });
    var cp=node('button',null,'⧉');
    cp.type='button';cp.title='Скопировать текст идеи';cp.setAttribute('aria-label',cp.title);
    cp.addEventListener('click',function(){copy(s,'Идея скопирована');});
    acts.appendChild(pin);acts.appendChild(cp);
    row.appendChild(body);row.appendChild(acts);
    box.appendChild(row);
  });
  box.scrollTop=top;
  ideaStats(list.length);
  if(focusIdx!=null){
    var f=box.querySelector('.idea[data-i="'+focusIdx+'"] .acts button');
    if(f)f.focus();
  }
}
$('ideaToggle').addEventListener('click',function(){
  ideasOpen=!ideasOpen;syncEmptyState();
  if(ideasOpen)renderSeedIdeas();
});
$('ideaMode').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  setIdeaMode(b.dataset.m,false);
});
$('ideaMode').addEventListener('keydown',function(e){
  if(['ArrowLeft','ArrowRight','Home','End'].indexOf(e.key)<0)return;
  e.preventDefault();
  var i=IDEA_ORDER.indexOf(ideaMode);
  if(e.key==='ArrowLeft')i=(i-1+IDEA_ORDER.length)%IDEA_ORDER.length;
  else if(e.key==='ArrowRight')i=(i+1)%IDEA_ORDER.length;
  else if(e.key==='Home')i=0;
  else i=IDEA_ORDER.length-1;
  setIdeaMode(IDEA_ORDER[i],true);
});
$('regenIdeasBtn').addEventListener('click',async function(){
  if(regenBusy){toast('Уже генерирую…','warn');return;}
  regenBusy=true;
  var btn=this,html=btn.innerHTML,done=false;
  btn.disabled=true;btn.innerHTML='<span class="sp">◌</span> генерирую…';
  $('ideaCount').textContent='подбираю сцены · '+ideaMode+'…';
  try{
    var r=await fetch('/api/ideas/regen',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:ideaMode})});
    var j=await r.json();
    if(j.ok&&Array.isArray(j.ideas)&&j.ideas.length){
      IDEAS=j.ideas;listMode=j.mode||ideaMode;done=true;
      renderSeedIdeas();stampNow();
      toast('Новые идеи: '+j.ideas.length+' · '+listMode);
    }else{toast('Не удалось: '+(j.error||'пустой ответ'),'bad');}
  }catch(e){toast('Нет связи: '+e,'bad');}
  finally{
    btn.disabled=false;btn.innerHTML=html;regenBusy=false;
    if(!done)renderSeedIdeas();
    paintIdeaMode();
  }
});
document.addEventListener('keydown',function(e){
  if(e.key!=='Escape'||!ideasOpen||rendered.length===0)return;
  if($('lb').classList.contains('show'))return;
  ideasOpen=false;syncEmptyState();
});
paintIdeaMode();
renderSeedIdeas();
syncEmptyState();
/* галерея */
var items=[],filter='all',galSig='',lbIndex=-1,galN=0;
var FLABEL={all:'все',wip:'в работе',ok:'готово',bad:'брак'};
function statusKind(s){
  s=String(s||'');
  if(s==='готово')return 'ok';
  if(s==='брак')return 'bad';
  if(s==='ретрай')return 'retry';
  return 'wip';
}
function tagList(p){return String(p||'').split(',').map(function(x){return x.trim();}).filter(Boolean);}
function passFilter(it){
  var k=statusKind(it.status);
  if(filter==='all')return true;
  if(filter==='wip')return k==='wip'||k==='retry';
  return k===filter;
}
function syncGallery(arr){
  items=Array.isArray(arr)?arr:[];
  $('galCount').textContent=items.length;
  var cnt={all:items.length,wip:0,ok:0,bad:0};
  items.forEach(function(it){var k=statusKind(it.status);if(k==='retry')k='wip';cnt[k]++;});
  Array.prototype.forEach.call($('filters').children,function(b){
    b.textContent=FLABEL[b.dataset.f]+' '+cnt[b.dataset.f];});
  $('galEmpty').classList.toggle('show',items.filter(passFilter).length===0);
  var sig=JSON.stringify(items.map(function(it){
    var c={};for(var k in it)if(k!=='progress')c[k]=it[k];return c;
  }))+'|'+filter;
  if(sig!==galSig){
    galSig=sig;
    var top=grid.scrollTop,grew=items.length>galN;galN=items.length;
    grid.innerHTML='';
    var frag=document.createDocumentFragment();
    items.forEach(function(it,i){if(passFilter(it))frag.appendChild(card(it,i));});
    grid.appendChild(frag);
    grid.scrollTop=grew?grid.scrollHeight:top;
  }
  updateProgress();
}
function updateProgress(){
  items.forEach(function(it){
    var c=grid.querySelector('.card[data-idx="'+it.idx+'"]');if(!c)return;
    var pr=it.progress,lbl=c.querySelector('.lbl'),bar=c.querySelector('.pbar i');
    if(pr){
      if(lbl)lbl.textContent=(pr.stage||'')+(pr.max?(' · '+pr.value+'/'+pr.max+' · '+pr.pct+'%'):'');
      if(bar)bar.style.width=(pr.pct||0)+'%';
    }
  });
}
function card(it,i){
  var c=node('div','card'),kind=statusKind(it.status);
  c.dataset.idx=it.idx;
  var head=node('div','chead');
  head.appendChild(node('span','badge '+kind,'#'+it.idx+' · '+(it.status||'')));
  head.appendChild(node('div','spacer'));
  if(it.mode&&it.mode!=='tags')head.appendChild(node('span','pill',it.mode));
  var sd=node('button','seed','seed '+it.seed);
  sd.title='Скопировать seed';
  sd.addEventListener('click',function(){copy(String(it.seed),'Seed скопирован');});
  head.appendChild(sd);c.appendChild(head);
  var shot=node('div','shot');
  if(it.file){
    var img=document.createElement('img');
    img.src='/img/'+encodeURIComponent(it.file);img.loading='lazy';img.decoding='async';img.alt='кадр '+it.idx;
    img.addEventListener('click',function(){openLb(i);});
    shot.appendChild(img);
  }else{
    shot.appendChild(node('div','skel'));
    shot.appendChild(node('div','lbl',it.status||'ожидание'));
    var pb=node('div','pbar');pb.appendChild(document.createElement('i'));shot.appendChild(pb);
  }
  c.appendChild(shot);
  if(it.src&&it.src.length){
    var sr=node('div','srcs');
    sr.appendChild(node('s',null,'исходники'));
    it.src.forEach(function(f){
      var im=document.createElement('img');
      im.src='/uploads/'+encodeURIComponent(f);im.title=f;im.loading='lazy';
      im.addEventListener('click',function(){window.open(im.src,'_blank');});
      sr.appendChild(im);
    });
    c.appendChild(sr);
  }
  if(it.verdict){
    c.appendChild(node('div','verdict '+(it.ok===false?'bad':(it.ok?'good':'')),it.verdict));
  }
  var tags=tagList(it.prompt);
  if(tags.length){
    var det=document.createElement('details');det.className='tags';
    var sum=document.createElement('summary');sum.textContent='промпт · '+tags.length+' тегов';
    det.appendChild(sum);
    var box=node('div','tagbox');
    tags.forEach(function(t){
      var b=node('button','tag',t);b.title='Скопировать тег';
      b.addEventListener('click',function(){copy(t,'Тег скопирован');});
      box.appendChild(b);
    });
    det.appendChild(box);c.appendChild(det);
  }
  var acts=node('div','cacts');
  var open=node('button',null,'Открыть');open.disabled=!it.file;
  open.addEventListener('click',function(){openLb(i);});
  var cp=node('button',null,'Копировать');
  cp.addEventListener('click',function(){copy(it.prompt||'','Промпт скопирован');});
  acts.appendChild(open);acts.appendChild(cp);
  if(it.file){
    var a=document.createElement('a');a.textContent='Скачать';
    a.href='/img/'+encodeURIComponent(it.file);a.setAttribute('download',it.file);
    acts.appendChild(a);
  }
  c.appendChild(acts);
  return c;
}
$('filters').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  filter=b.dataset.f;
  Array.prototype.forEach.call($('filters').children,function(x){x.classList.toggle('on',x===b);});
  galSig='';syncGallery(items);
});
/* лайтбокс */
function withFile(){return items.map(function(it,i){return i;}).filter(function(i){return !!items[i].file;});}
function openLb(i){
  if(!items[i]||!items[i].file)return;
  lbIndex=i;paintLb();$('lb').classList.add('show');
}
function paintLb(){
  var it=items[lbIndex];if(!it)return;
  var url='/img/'+encodeURIComponent(it.file);
  var im=$('lbImg');im.className='';$('lbWrap').className='lbimg';im.src=url;
  $('lbMeta').textContent='#'+it.idx+' · '+(it.status||'')+'\nseed '+it.seed+'\n'+it.file;
  $('lbMeta').style.whiteSpace='pre-wrap';
  $('lbDl').href=url;$('lbDl').setAttribute('download',it.file);
  var box=$('lbTags');box.innerHTML='';
  tagList(it.prompt).forEach(function(t){
    var b=node('button','tag',t);b.addEventListener('click',function(){copy(t,'Тег скопирован');});
    box.appendChild(b);
  });
}
function closeLb(){$('lb').classList.remove('show');lbIndex=-1;}
function step(d){
  var list=withFile();if(!list.length)return;
  var pos=list.indexOf(lbIndex);if(pos<0)pos=0;
  pos=(pos+d+list.length)%list.length;lbIndex=list[pos];paintLb();
}
$('lbClose').addEventListener('click',closeLb);
$('lbPrev').addEventListener('click',function(){step(-1);});
$('lbNext').addEventListener('click',function(){step(1);});
$('lbCopy').addEventListener('click',function(){if(items[lbIndex])copy(items[lbIndex].prompt||'','Промпт скопирован');});
$('lbWrap').addEventListener('click',function(e){if(e.target===$('lbWrap'))closeLb();});
$('lbImg').addEventListener('click',function(){
  var z=$('lbImg').classList.toggle('zoom');$('lbWrap').classList.toggle('wrap',z);
});
document.addEventListener('keydown',function(e){
  if(!$('lb').classList.contains('show'))return;
  if(e.key==='Escape')closeLb();
  else if(e.key==='ArrowLeft')step(-1);
  else if(e.key==='ArrowRight')step(1);
});
/* настройки */
var CFG={},ASSETS={ckpts:[],loras:[],upscales:[],ipa:false},RESLIST=[],saveTimer=null,loaded=false;
function optsHtml(arr,val,empty){
  arr=Array.isArray(arr)?arr:[];
  var h='<option value="">'+esc(empty)+'</option>';
  if(val&&arr.indexOf(val)<0)h+='<option value="'+esc(val)+'" selected>'+esc(val)+' [нет в ComfyUI]</option>';
  return h+arr.map(function(n){return '<option value="'+esc(n)+'"'+(n===val?' selected':'')+'>'+esc(n)+'</option>';}).join('');
}
function setSel(id,arr,val,empty){var e=$(id);if(e)e.innerHTML=optsHtml(arr,val,empty);}
function resWH(k){var m=String(k||'').match(/(\d+)\s*x\s*(\d+)/);return m?[parseInt(m[1],10),parseInt(m[2],10)]:null;}
function applyCfg(c){
  c=c||{};
  setSel('ckpt',ASSETS.ckpts,c.ckpt,'— выбери чекпоинт —');
  var rs=$('res'),list=RESLIST.length?RESLIST:(c.resolution?[c.resolution]:[]);
  rs.innerHTML=list.map(function(k){return '<option value="'+esc(k)+'"'+(k===c.resolution?' selected':'')+'>'+esc(k)+'</option>';}).join('');
  if(c.resolution)rs.value=c.resolution;
  var ff=(Array.isArray(c.fw_fh)&&c.fw_fh.length===2)?c.fw_fh:[2000,1000];
  $('fw').value=ff[0];$('fh').value=ff[1];
  $('steps').value=(c.steps==null?45:c.steps);$('stepsV').textContent=$('steps').value;
  $('cfg').value=(c.cfg==null?5.5:c.cfg);$('cfgV').textContent=Number($('cfg').value).toFixed(1);
  $('max').value=(c.max_images==null?3:c.max_images);$('maxV').textContent=$('max').value;
  setSel('up',ASSETS.upscales,c.upscaler||'','— без апскейла —');
  $('crit').checked=!!c.use_critic;
  $('refMode').value=c.ref_mode||'auto';
  $('refDenoise').value=(c.ref_denoise==null?0.55:c.ref_denoise);
  $('refDenoiseV').textContent=Number($('refDenoise').value).toFixed(2);
  $('refAutoSize').checked=c.ref_auto_size!==false;
  $('refCutout').checked=!!c.ref_cutout;
  $('refIpa').checked=!!c.ref_ipadapter;
  $('ipaInfo').textContent='IPAdapter: '+(ASSETS.ipa?'ноды найдены':'нод нет (ComfyUI_IPAdapter_plus)');
  loraRows(c.loras);
  $('assetsInfo').textContent='ComfyUI: чекпоинтов '+ASSETS.ckpts.length+' · LoRA '+ASSETS.loras.length+' · апскейлеров '+ASSETS.upscales.length;
}
function loraRows(list){
  var L=$('loras');L.innerHTML='';
  var arr=(Array.isArray(list)?list:[]).slice(0,10);
  while(arr.length<10)arr.push({name:'',strength:0.8,on:false});
  arr.forEach(function(l,i){
    l=l||{};
    var row=node('div','lrow'+(l.on?'':' off'));
    var top=node('div','ltop');
    var sw=document.createElement('label');sw.className='switch';
    var cb=document.createElement('input');cb.type='checkbox';cb.checked=!!l.on;
    sw.appendChild(cb);sw.appendChild(document.createElement('i'));
    var sel=document.createElement('select');sel.innerHTML=optsHtml(ASSETS.loras,l.name||'','— нет —');
    var num=node('span','num','#'+(i+1));
    top.appendChild(sw);top.appendChild(sel);top.appendChild(num);
    var rng=document.createElement('input');rng.type='range';rng.min='0';rng.max='2';rng.step='0.05';
    rng.value=(l.strength==null?0.8:l.strength);
    num.textContent=Number(rng.value).toFixed(2);
    cb.addEventListener('change',function(){row.classList.toggle('off',!cb.checked);markDirty();});
    sel.addEventListener('change',function(){markDirty();});
    rng.addEventListener('input',function(){num.textContent=Number(rng.value).toFixed(2);markDirty();});
    row.appendChild(top);row.appendChild(rng);L.appendChild(row);
  });
}
function readUI(){
  var rows=Array.prototype.map.call($('loras').querySelectorAll('.lrow'),function(r){
    return{on:r.querySelector('input[type=checkbox]').checked,
           name:r.querySelector('select').value,
           strength:parseFloat(r.querySelector('input[type=range]').value)||0};
  });
  return{ckpt:$('ckpt').value,resolution:$('res').value,
    fw_fh:[parseInt($('fw').value,10)||2000,parseInt($('fh').value,10)||1000],
    steps:parseInt($('steps').value,10)||45,cfg:parseFloat($('cfg').value)||5.5,
    max_images:parseInt($('max').value,10)||1,upscaler:$('up').value,
    use_critic:$('crit').checked,
    ref_mode:$('refMode').value,
    ref_denoise:parseFloat($('refDenoise').value)||0.55,
    ref_auto_size:$('refAutoSize').checked,
    ref_cutout:$('refCutout').checked,
    ref_ipadapter:$('refIpa').checked,
    loras:rows};
}
function setSaveState(kind){
  var e=$('saveState');
  e.className='pill'+(kind==='dirty'?' dirty':(kind==='live'?' live':''));
  e.textContent=kind==='dirty'?'не сохранено':(kind==='err'?'ошибка':'сохранено');
}
function markDirty(){
  if(!loaded)return;
  setSaveState('dirty');
  clearTimeout(saveTimer);saveTimer=setTimeout(function(){save(true);},650);
}
async function save(silent){
  clearTimeout(saveTimer);
  var c=readUI();
  try{
    var r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});
    if(!r.ok)throw new Error('HTTP '+r.status);
    CFG=c;setSaveState('live');if(!silent)toast('Настройки сохранены');
  }catch(e){setSaveState('err');if(!silent)toast('Не сохранено: '+e,'bad');}
}
async function loadCfg(force){
  try{
    var r=await fetch('/api/config'+(force?'?refresh=1':''),{cache:'no-store'});
    var j=await r.json();
    CFG=j.config||{};ASSETS=j.assets||{};RESLIST=Array.isArray(j.resolutions)?j.resolutions:[];
    ASSETS.ckpts=ASSETS.ckpts||[];ASSETS.loras=ASSETS.loras||[];ASSETS.upscales=ASSETS.upscales||[];
    applyCfg(CFG);loaded=true;setSaveState('live');
    if(force)toast('Списки обновлены из ComfyUI');
  }catch(e){$('assetsInfo').textContent='нет связи с мостом';toast('Мост не отвечает','bad');}
}
$('saveBtn').addEventListener('click',function(){save(false);});
$('refreshBtn').addEventListener('click',function(){loadCfg(true);});
$('revertBtn').addEventListener('click',function(){applyCfg(CFG);setSaveState('live');toast('Правки отменены');});
$('exportBtn').addEventListener('click',function(){
  var blob=new Blob([JSON.stringify(readUI(),null,1)],{type:'application/json'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='config.json';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  setTimeout(function(){URL.revokeObjectURL(a.href);},1500);toast('config.json выгружен');
});
$('randBtn').addEventListener('click',function(){
  var rows=$('loras').querySelectorAll('.lrow'),pool=ASSETS.loras.slice();
  Array.prototype.forEach.call(rows,function(r,i){
    var cb=r.querySelector('input[type=checkbox]'),sel=r.querySelector('select'),rng=r.querySelector('input[type=range]');
    var take=i<4&&pool.length;
    cb.checked=!!take;r.classList.toggle('off',!take);
    if(take){var k=Math.floor(Math.random()*pool.length);sel.value=pool.splice(k,1)[0];
      rng.value=(0.4+Math.random()*0.5).toFixed(2);r.querySelector('.num').textContent=Number(rng.value).toFixed(2);}
  });
  markDirty();toast('Случайный набор LoRA');
});
$('presets').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  var p=b.dataset.p;
  if(p==='draft'){$('steps').value=20;$('cfg').value=5;$('max').value=3;$('up').value='';$('crit').checked=false;}
  if(p==='final'){$('steps').value=45;$('cfg').value=5.5;$('max').value=3;
    if(ASSETS.upscales.length)$('up').value=ASSETS.upscales[0];$('crit').checked=true;}
  if(p==='max'){$('steps').value=64;$('cfg').value=6;$('max').value=5;
    if(ASSETS.upscales.length)$('up').value=ASSETS.upscales[0];$('crit').checked=true;}
  $('stepsV').textContent=$('steps').value;
  $('cfgV').textContent=Number($('cfg').value).toFixed(1);
  $('maxV').textContent=$('max').value;
  markDirty();toast('Пресет применён');
});
['steps','cfg','max'].forEach(function(id){
  $(id).addEventListener('input',function(){
    $(id+'V').textContent=(id==='cfg')?Number($(id).value).toFixed(1):$(id).value;
    markDirty();
  });
});
['ckpt','up','refMode','refAutoSize','refCutout','refIpa'].forEach(function(id){
  $(id).addEventListener('change',markDirty);
});
$('refDenoise').addEventListener('input',function(){
  $('refDenoiseV').textContent=Number(this.value).toFixed(2);markDirty();
});
$('crit').addEventListener('change',markDirty);
$('fw').addEventListener('input',markDirty);
$('fh').addEventListener('input',markDirty);
$('res').addEventListener('change',function(){
  var wh=resWH($('res').value);if(!wh)return;
  var k=2000/Math.max(wh[0],wh[1]);
  $('fw').value=Math.round(wh[0]*k/8)*8;$('fh').value=Math.round(wh[1]*k/8)*8;
  markDirty();
});
Array.prototype.forEach.call(document.querySelectorAll('.stp button'),function(b){
  b.addEventListener('click',function(){
    var t=$(b.dataset.t),d=parseInt(b.dataset.d,10);
    t.value=Math.max(256,(parseInt(t.value,10)||1024)+d);markDirty();
  });
});
/* ширина чата */
var grip=$('grip'),chatPane=$('chatPane');
function setChatW(w){
  var max=Math.max(320,Math.min(760,window.innerWidth-560));
  w=Math.max(300,Math.min(Math.round(w),max));
  document.documentElement.style.setProperty('--chat',w+'px');
  chatPane.style.width=w+'px';
  try{localStorage.setItem('heretic.chatW',String(w));}catch(e){}
  if(pinned)toBottom();
}
try{var sw=parseInt(localStorage.getItem('heretic.chatW'),10);if(sw)setChatW(sw);}catch(e){}
grip.addEventListener('pointerdown',function(e){
  grip.setPointerCapture(e.pointerId);grip.classList.add('act');document.body.classList.add('dragging');
  e.preventDefault();
});
grip.addEventListener('pointermove',function(e){
  if(!grip.classList.contains('act'))return;
  setChatW(e.clientX-chatPane.getBoundingClientRect().left);
});
function endDrag(e){
  if(!grip.classList.contains('act'))return;
  grip.classList.remove('act');document.body.classList.remove('dragging');
  try{grip.releasePointerCapture(e.pointerId);}catch(err){}
}
grip.addEventListener('pointerup',endDrag);
grip.addEventListener('pointercancel',endDrag);
grip.addEventListener('dblclick',function(){setChatW(460);});
window.addEventListener('resize',function(){
  if(window.innerWidth>900)setChatW(chatPane.getBoundingClientRect().width);
  if(pinned)toBottom();
});
/* рейл / табы */
function railIsOpen(){
  var w=window.innerWidth;
  if(w<=900)return $('panes').dataset.view==='rail';
  if(w<=1240)return document.body.classList.contains('rail-open');
  return !document.body.classList.contains('rail-hide');
}
function railSet(open){
  var w=window.innerWidth;
  if(w<=900){
    $('panes').dataset.view=open?'rail':'chat';
    Array.prototype.forEach.call($('tabs').children,function(x){x.classList.toggle('on',x.dataset.v===(open?'rail':'chat'));});
    if(!open&&pinned)toBottom();
    return;
  }
  if(w<=1240){document.body.classList.toggle('rail-open',open);return;}
  document.body.classList.toggle('rail-hide',!open);
}
$('railBtn').addEventListener('click',function(){railSet(!railIsOpen());});
$('railClose').addEventListener('click',function(){railSet(false);});
$('tabs').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  $('panes').dataset.view=b.dataset.v;
  Array.prototype.forEach.call($('tabs').children,function(x){x.classList.toggle('on',x===b);});
  if(b.dataset.v==='chat'&&pinned)toBottom();
});
/* телеметрия */
function shortCpu(s){return String(s||'CPU').replace(/\(R\)|\(TM\)|CPU|Processor|\d+-Core/ig,'').replace(/@.*$/,'').replace(/\s+/g,' ').trim()||'CPU';}
function shortGpu(s){return String(s||'GPU').replace(/^NVIDIA\s+(GeForce\s+)?/i,'').replace(/\s+/g,' ').trim()||'GPU';}
function tCls(v){return v==null?'':(v<62?'t-ok':(v<78?'t-warn':'t-bad'));}
function tempStr(v){return v==null?'t н/д':Math.round(v)+'°C';}
function gb(v){return (v==null||isNaN(v))?'н/д':(Number(v)/1024).toFixed(1);}
function chipEl(title,line,pct,hot){
  var c=node('div','chip'+(hot?' hot':''));
  c.appendChild(node('b',null,title));
  var s=document.createElement('s');s.innerHTML=line;c.appendChild(s);
  var u=document.createElement('u');u.style.width=Math.max(0,Math.min(100,pct||0))+'%';c.appendChild(u);
  return c;
}
async function hwTick(){
  var box=$('hw');
  try{
    var h=await (await fetch('/api/hw',{cache:'no-store'})).json();
    box.innerHTML='';
    var parts=[];
    if(h.cpu_temp!=null)parts.push('<span class="'+tCls(h.cpu_temp)+'">'+Math.round(h.cpu_temp)+'°C</span>');
    if(h.cpu_load!=null)parts.push(Math.round(h.cpu_load)+'%');
    if(h.cpu_power!=null)parts.push(Math.round(h.cpu_power)+' Вт');
    box.appendChild(chipEl(shortCpu(h.cpu),parts.join(' · ')||'н/д',h.cpu_load!=null?h.cpu_load:0,h.cpu_temp!=null&&h.cpu_temp>=78));
    if(h.ram){
      var p=h.ram.total?h.ram.used/h.ram.total*100:0;
      box.appendChild(chipEl('RAM',Number(h.ram.used).toFixed(1)+' / '+Number(h.ram.total).toFixed(1)+' ГБ · '+Math.round(p)+'%',p,p>85));
    }
    (h.gpus||[]).forEach(function(g,i){
      var vp=g.mem_total?g.mem_used/g.mem_total*100:0;
      var l1='<span class="'+tCls(g.temp)+'">'+tempStr(g.temp)+'</span> · '+(g.util!=null?Math.round(g.util)+'%':'н/д');
      if(g.power!=null)l1+=' · '+Math.round(g.power)+' Вт';
      var line=l1+'<br>'+gb(g.mem_used)+' / '+gb(g.mem_total)+' ГБ';
      box.appendChild(chipEl('GPU'+i+' '+shortGpu(g.name),line,Math.max(vp,g.util||0),g.temp!=null&&g.temp>=78));
    });
    if(!(h.gpus||[]).length)box.appendChild(chipEl('GPU','nvidia-smi не ответил',0,true));
  }catch(e){
    box.innerHTML='';box.appendChild(chipEl('Телеметрия','нет ответа /api/hw',0,true));
  }
}
/* опрос состояния */
var ticking=false,wasBusy=false;
async function tick(){
  if(ticking)return;ticking=true;
  try{
    var s=await (await fetch('/api/state?id='+encodeURIComponent(activeId||''),{cache:'no-store'})).json();
    var ids=(s.chats||[]).map(function(c){return c.id;});
    if(!activeId||ids.indexOf(activeId)<0){
      activeId=s.id;
      try{localStorage.setItem('heretic.chat',activeId);}catch(e){}
      clearChatView();
    }
    renderTabs(s.chats);
    busy=!!s.busy;
    $('status').classList.toggle('busy',busy);
    var pr=s.progress;
    if(pr&&pr.active){
      $('progChip').style.display='';
      $('progT').textContent='Кадр #'+(pr.idx||1)+' · '+(pr.stage||'генерация');
      $('progL').textContent=(pr.max?('шаг '+pr.value+'/'+pr.max+' · '+pr.pct+'%'):'подготовка')
        +(pr.overall!=null?' · всего '+pr.overall+'%':'');
      $('progU').style.width=(pr.overall||0)+'%';
    }else{$('progChip').style.display='none';}
    $('statusText').textContent=busy?((pr&&pr.active&&pr.overall!=null)?'генерация '+pr.overall+'%':'генерация…'):'готов';
    $('sendBtn').disabled=busy;
    syncLog(s.messages);
    syncGallery(s.items);
    if(wasBusy&&!busy){chime();toast('Серия завершена');}
    wasBusy=busy;
  }catch(e){
    $('statusText').textContent='нет связи';
    $('status').classList.remove('busy');
  }
  ticking=false;
}
loadCfg(false);
tick();setInterval(tick,1200);
hwTick();setInterval(hwTick,3000);
setTimeout(function(){inp.focus();},400);
})();
</script>
</body>
</html>"""

async def _generate_ideas_bg():
    await asyncio.sleep(3)
    cur = load_ideas()
    if cur is None:
        print("[ideas] файла нет — генерирую...")
        await generate_ideas()
        return
    good = [x for x in cur if idea_ok(x, IDEA_MODE, [])]
    if len(good) < max(3, len(cur) // 2):
        print(f"[ideas] кэш мусорный ({len(good)}/{len(cur)}) — перегенерирую")
        await generate_ideas()
    else:
        print("[ideas] идеи из файла:", len(cur))

async def on_start(app):
    global HTTP
    HTTP = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600))
    prune_uploads()
    app["ws_task"] = asyncio.create_task(ws_loop())
    asyncio.create_task(_generate_ideas_bg())

async def on_cleanup(app):
    app["ws_task"].cancel()
    if HTTP:
        await HTTP.close()

if __name__ == "__main__":
    if Image is None:
        print("!!! Pillow не установлен — исходники/коллаж работать не будут: pip install pillow")
    if not os.path.isdir(CONFIG["output_dir"]):
        print("!!! output_dir не существует:", CONFIG["output_dir"])
    print("!!! Запускай ComfyUI так: python main.py --cuda-device 1 --highvram")
    print("!!! LM Studio ограничь GPU 0 (Hardware -> выключи 3070 Ti, или CUDA_VISIBLE_DEVICES=0)")
    print("Исходники:", UPLOAD_DIR)
    app = web.Application(client_max_size=64 * 1024**2)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", h_root),
        web.post("/api/ideas/regen", h_ideas_regen),
        web.get("/api/state", h_state),
        web.post("/api/chat", h_chat),
        web.post("/api/upload", h_upload),
        web.get("/uploads/{name}", h_ref_file),
        web.post("/api/stop", h_stop),
        web.post("/api/reset", h_reset),
        web.post("/api/new", h_new),
        web.post("/api/chats/create", h_chat_create),
        web.post("/api/chats/delete", h_chat_delete),
        web.post("/api/chats/rename", h_chat_rename),
        web.get("/api/config", h_cfg_get),
        web.post("/api/config", h_cfg_set),
        web.get("/img/{name}", h_img),
        web.get("/api/hw", h_hw)])
    print(f"Открой http://127.0.0.1:{WEB_PORT}")
    web.run_app(app, host="127.0.0.1", port=WEB_PORT)