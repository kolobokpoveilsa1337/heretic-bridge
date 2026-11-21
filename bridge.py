# bridge.py — Qwen3.5-9B (90HX, GPU0) -> ComfyUI (3070 Ti, GPU1) -> VLM-критик
# мультичат · прогресс шагов через WS · телеметрия с потреблением (Вт)
import asyncio, base64, ctypes, glob, json, os, platform, random, re, shutil, subprocess, time, uuid
import aiohttp
from aiohttp import web

# ===== ПУТИ И БАЗА =====
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MEM_FILE   = os.path.join(BASE_DIR, "memory.json")
CFG_FILE   = os.path.join(BASE_DIR, "config.json")
LLM_DEBUG  = os.path.join(BASE_DIR, "llm_debug.json")
CHATS_FILE = os.path.join(BASE_DIR, "chats.json")
CHAT_FILE  = os.path.join(BASE_DIR, "chat_history.json")   # только для миграции
LAST_FILE  = os.path.join(BASE_DIR, "last_shot.json")      # только для миграции
LM_URL     = "http://127.0.0.1:1234"
COMFY_URL  = "http://127.0.0.1:8188"
VLM_MMPROJ = "mmproj-F16.gguf"
VLM_GPU    = "0"          # критик на GPU0 (рядом с LM Studio), НЕ на карте ComfyUI!
VLM_PORT   = 8189
LLAMA_SERVER = ""
WEB_PORT   = 7860
MIN_TOKENS = 3072
HIST_TURNS = 12
CLIP_LEN   = 1500

IDEA_FILE    = os.path.join(BASE_DIR, "ideas.json")
DEFAULT_IDEAS = [
    "зеленоволосая кошечка-библиотекарь в очках и вязаном жилете тянется к верхней полке старой библиотеки, вокруг лестница и стопки фолиантов, тёплый закатный свет из витражного окна, средний план",
    "девушка-механик с медным каре и веснушками лежит под капотом маслкара в гараже, верстак с инструментами и канистры вокруг, оранжевый закат через ворота, низкий ракурс",
    "белая фурри-волчица в меховой накидке сидит у костра на заснеженной поляне, искры летят к звёздному небу, вокруг тёмные ели и следы на снегу, тёплый свет огня на морде, крупный план",
    "андроид-девушка с неоновыми прядями стоит под дождём в киберпанк-переулке, вывески отражаются в лужах, пар из решёток, холодный сине-розовый свет, взгляд в камеру, поясной план",
]
CACHED_IDEAS = None

IDEA_PROMPT = (
    "Ты креативный ассистент аниме-арт-директора.\n"
    "Придумай 8 развёрнутых идей для аниме-картинок в стиле Illustrious XL (SDXL).\n"
    "Каждая идея — 2-3 предложения на русском, 25-45 слов, конкретная сцена: "
    "кто персонаж (волосы, глаза, одежда), где он (локация + 2-3 детали окружения), "
    "что делает и в какой позе, время суток и характер света, настроение, насколько крупно показан.\n"
    "Разнообразь: разные персонажи (девушки, парни, фурри, роботы, монстры), разные локации, "
    "разное время суток, разное настроение.\n"
    "Пример формата: 'зеленоволосая кошечка-библиотекарь в очках и вязаном жилете тянется "
    "к верхней полке старой библиотеки, вокруг лестница и стопки фолиантов, тёплый закатный свет "
    "из витражного окна, уютная тишина, средний план'.\n"
    "НЕ повторяй пример — придумай новые.\n"
    "Ответь ТОЛЬКО валидным JSON массивом из 8 строк, без пояснений и markdown:\n"
    '["идея1", "идея2", "идея3", "идея4", "идея5"]'
)

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
        json.dump(ideas, open(IDEA_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception as e:
        print("[ideas] не сохранилось:", e)

async def generate_ideas():
    """Генерирует 8 идей через LM Studio и сохраняет в ideas.json."""
    global CACHED_IDEAS
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            txt = await llm(s, [
                {"role": "system",
                 "content": "Ты креативный ассистент. Отвечай только валидным JSON."},
                {"role": "user", "content": IDEA_PROMPT}
            ], 0.9, 1600, want_json=True)
            parsed = parse_json(txt)
            if isinstance(parsed, list) and len(parsed) >= 4:
                ideas = [str(x).strip() for x in parsed if str(x).strip()][:12]
                if ideas:
                    CACHED_IDEAS = ideas
                    save_ideas(ideas)
                    print(f"[ideas] сгенерировано {len(ideas)} идей")
                    return ideas
    except Exception as e:
        print("[ideas] генерация не удалась:", e)
    return None

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
    "vision_backend": "lmstudio",
    "sampler": "euler_ancestral", "scheduler": "normal", "clip_skip": 2,
    "quality_on": True, "solo": True,
    "hires_on": True, "hires_scale": 1.5, "hires_denoise": 0.4,
    "hires_steps": 20, "hires_cfg": 5.0,
    "hires_sampler": "dpmpp_2m_sde", "hires_scheduler": "karras",
    "hires_max_px": 2600000,
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
          "dialog": {"stage": "idle", "theme": "", "history": []},
          "busy": False, "cancel": False, "progress": None, "epoch": 0}
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
                              "dialog": {"stage": "idle", "theme": "", "history": []},
                              "busy": False, "cancel": False, "progress": None, "epoch": 0}
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

def say(ch, role, text):
    ch["messages"].append({"role": role, "text": text})
    del ch["messages"][:-200]
    save_chats()

def chat_add(ch, role, content):
    c = str(content or "").strip()
    if not c: return
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

def detect_intent(ch, text):
    t = (text or "").lower().strip()
    if not last_shot(ch):
        return "new"
    if any(w in t for w in NEW_WORDS):
        return "new"
    return "edit"   # есть кадр → всё остальное = корректировка

DIRECT_WORDS = ("с этим промтом", "с этим промптом", "по этому промпту", "готовый промпт",
                "готовым промптом", "вот промпт", "вот промт", "мой промпт", "этот промпт",
                "этим промптом", "используй промпт", "используй этот промпт", "ничего не меняй",
                "не переписывай", "как есть", "больше ничего не надо", "ничего не надо делать",
                "промт:", "промпт:", "prompt:")
NOQ_WORDS = ("без вопросов", "не задавай вопросов", "не спрашивай", "без уточнений",
             "просто сгенерируй", "сразу генерируй", "генерируй сразу", "не надо вопросов")

DIRECT_WORDS = ("с этим промтом ", "с этим промптом ", "по этому промпту ", "готовый промпт ",
                "готовым промптом ", "вот промпт ", "вот промт ", "мой промпт ", "этот промпт ",
                "этим промптом ", "используй промпт ", "используй этот промпт ", "ничего не меняй ",
                "не переписывай ", "как есть ", "больше ничего не надо ", "ничего не надо делать ",
                "промт: ", "промпт: ", "prompt: ")

NEW_WORDS = ("новая тема", "новая картинка", "новый сюжет", "забудь", "начни заново",
             "с нуля", "другая тема", "другой сюжет", "новая сцена", "новый кадр",
             "новая серия", "забудь прошлое", "новая генерация", "новая история",
             "new topic", "forget", "reset")

NOQ_WORDS = ("без вопросов ", "не задавай вопросов ", "не спрашивай ", "без уточнений ",
             "просто сгенерируй ", "сразу генерируй ", "генерируй сразу ", "не надо вопросов ")

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

def detect_direct(text):
    t = str(text or "").lower()
    if extract_prompt(text) and (looks_like_prompt(text) or any(w in t for w in DIRECT_WORDS)):
        return "raw"
    if any(w in t for w in NOQ_WORDS) or any(w in t for w in DIRECT_WORDS):
        return "skip"
    return ""

# ===== ПРОМПТЫ =====
QUALITY = ("masterpiece, best quality, very aesthetic, absurdres, newest, "
           "detailed, sharp focus, clean lineart")
NEGATIVE = (
    "lowres, worst quality, low quality, bad quality, jpeg artifacts, blurry, "
    "motion blur, chromatic aberration, watermark, signature, username, artist name, "
    "text, logo, error, "
    "bad anatomy, bad proportions, bad hands, bad feet, missing fingers, extra fingers, "
    "fused fingers, fewer digits, extra digits, mutated hands, malformed limbs, "
    "extra limbs, missing limbs, extra arms, extra legs, long neck, long body, "
    "multiple heads, extra head, deformed face, asymmetrical eyes, "
    "realistic, photorealistic, photo, 3d, cgi, western cartoon, "
    "monochrome, greyscale, sketch, unfinished "
    # Убедитесь, что здесь НЕТ слов: nsfw, nude, naked, censored, mosaic
)
SOLO_NEG = "multiple girls, 2girls, 3girls, multiple boys, 2boys, crowd, group of people, clone, twins"

SYS = (
    "Ты — арт-директор генерации аниме-картинок на Illustrious XL (SDXL).\n"
    "Пользователь даёт тему. Придумай серию сцен-вариаций и для каждой напиши промпт ТЕГАМИ.\n"
    'Отвечай ТОЛЬКО валидным JSON: {"plan":"1-2 предложения по-русски","images":[{"prompt":"...","seed":null}]}\n'
    "\nЖЁСТКИЕ ПРАВИЛА поля prompt:\n"
    "1) Только английские danbooru-теги через запятую, нижний регистр, пробелы вместо подчёркиваний, одна строка.\n"
    "2) РОВНО 45-65 тегов. Меньше 45 — мало детали. Больше 65 — CLIP размывает внимание, картинка мылится.\n"
    "3) Никаких русских слов, предложений, пояснений, markdown, кавычек внутри строки.\n"
    "4) НЕ пиши masterpiece, best quality, very aesthetic, absurdres, highres, newest — они добавляются автоматически.\n"
    "5) НЕ пиши выдуманные и мета-теги: 8k, uhd, hdr, award-winning, trending on pixiv, professional, "
    "perfect composition, noise-free, artifact-free, ultra-detailed, intricate details. Это мусор, он не работает.\n"
    "6) Вес (tag:1.2) разрешён максимум на 3 тегах и не выше 1.3.\n"
    "7) Никаких отрицаний (no ..., without ...) — им место в negative.\n"
    "8) Никаких противоречий: nude вместе с clothes, standing вместе с sitting, "
    "from behind вместе с looking at viewer — запрещены.\n\n"
    "ПОРЯДОК БЛОКОВ (CLIP сильнее весит начало, порядок соблюдай):\n"
    "1. solo + 1girl/1boy/1other + персонаж и серия, если узнаваемы (2-4 тега)\n"
    "2. вид/тело/кожа: 4-6 (anthro, furry female, slender, wide hips, pale skin)\n"
    "3. волосы: 3-5 (long hair, green hair, messy hair, hair over one eye)\n"
    "4. глаза/лицо: 3-4 (green eyes, slit pupils, long eyelashes, sharp eyes)\n"
    "5. одежда ИЛИ детали обнажённого тела: 5-9\n"
    "6. выражение/взгляд: 2-4 (half-closed eyes, blush, looking at viewer, parted lips)\n"
    "7. поза/действие: 4-7 (sitting, legs crossed, arm up, holding book, contrapposto)\n"
    "8. камера: 2-3 (cowboy shot, from side, from below, dutch angle, close-up)\n"
    "9. фон/предметы: 5-8, КОНКРЕТНО (library, bookshelf, wooden floor, ladder, scattered books, window)\n"
    "10. свет/время суток: 3-5 (sunset, orange sky, backlighting, sunbeam, soft shadows)\n"
    "11. стиль/настроение: 2-4 (anime coloring, cel shading, vibrant colors, depth of field, serene)\n\n"
    "NSFW: прямые теги без эвфемизмов (nude, nipples, pussy, penis, cum, sex, vaginal, spread legs), "
    "если тема этого требует. Не смешивай одежду и полную наготу.\n"
    "Сцены серии должны отличаться позой, ракурсом и светом, а не только фоном."
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
    'ФОРМАТ: только валидный JSON {"plan":"что изменил, 1-2 предложения по-русски","images":[{"prompt":"...","seed":<тот же seed>}]}\n'
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
         'Ответь только JSON: {{"prompt":"..."}}')
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
    '{{ "ok":true, "defects":[], "comment": "кратко по-русски", '
    '"description": "подробно по-русски: кто изображён, волосы, глаза, одежда или нагота, поза, '
    'ракурс, фон, свет, стиль, качество линий и мелких деталей" }}'
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
    '{"questions":[{"q":"кто на картинке?","options":["девушка","парень","фурри","робот","животное","свой вариант"]}]}\n'
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

# ===== ОБЩЕЕ СОСТОЯНИЕ =====
ASSETS = {"ckpts": [], "loras": [], "upscales": []}
ASSET_LOCK = asyncio.Lock()
MODEL_ID = None
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
    objs, depth, start = [], 0, None
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}" and depth:
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

async def llm(s, messages, temp, mt, want_json=False):
    await ensure_model(s)
    budget = max(mt, MIN_TOKENS)
    variants = [
        ("effort_none", LM_URL + "/v1/chat/completions",
         {"model": MODEL_ID, "messages": messages, "temperature": temp, "stream": False,
          "max_tokens": budget, "reasoning_effort": "none",
          "chat_template_kwargs": {"enable_thinking": False}}),
        ("native_off", LM_URL + "/api/v1/chat",
         {"model": MODEL_ID, "temperature": temp, "stream": False, "store": False,
          "max_output_tokens": budget, "reasoning": "off",
          "system_prompt": "\n".join(m["content"] for m in messages if m["role"] == "system"),
          "input": "\n\n".join(m["content"] for m in messages if m["role"] != "system")}),
        ("big_budget", LM_URL + "/v1/chat/completions",
         {"model": MODEL_ID, "messages": messages, "temperature": temp, "stream": False,
          "max_tokens": budget * 2}),
    ]
    err = ""
    for tag, url, payload in variants:
        print(f"[llm] пробую {tag}...")
        try:
            async with s.post(url, json=payload) as r:
                j = await r.json()
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
        if want_json:
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
            elif isinstance(o, dict):                 # на будущее: options-объекты
                v = o.get("content") or o.get("name") or o.get("value")
                if v:
                    out.append(str(v))
        return out
    return []

async def fetch_assets(s=None):
    own = s is None
    if own:
        s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
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
    except Exception as e:
        print("[assets] ошибка:", e)
    finally:
        if own:
            await s.close()
    print("[assets] ckpts:", len(ASSETS["ckpts"]),
          "| loras:", len(ASSETS["loras"]),
          "| upscales:", len(ASSETS["upscales"]))
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
    for root in (save_dir(), CONFIG["output_dir"]):
        p = os.path.join(root, n)
        if os.path.isfile(p):
            return p
    return os.path.join(CONFIG["output_dir"], n)

def safe_name(t, n=30):
    t = re.sub(r"[^0-9A-Za-zА-Яа-яЁё _-]+", "", str(t or ""))
    t = t.strip().replace(" ", "")
    return t[:n] or "series"

def archive(fin, base, theme, seed):
    d = save_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = safe_name(theme)
    res = {"final": fin, "base": base}
    for kind, name in (("final", fin), ("base", base)):
        if not name: continue
        src = os.path.join(CONFIG["output_dir"], os.path.basename(name))
        if not os.path.isfile(src): continue
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

# ===== ГРАФ =====
def build_graph(content, seed, pref_final="heretic", pref_base="heretic_base"):
    cfg = CONFIG
    w, h = RESOLUTIONS.get(cfg["resolution"], (1408, 704))
    fw, fh = cfg.get("fw_fh") or [2000, 1000]
    loras = [l for l in cfg["loras"] if l.get("on") and l.get("name")]
    g = {"ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": cfg["ckpt"]}}}
    m, c, v = ["ckpt", 0], ["ckpt", 1], ["ckpt", 2]
    for i, l in enumerate(loras):
        if l["name"] not in ASSETS["loras"]: continue
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
        pos += ", " + QUALITY
    neg = NEGATIVE + (", " + SOLO_NEG if cfg.get("solo", True) else "")
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": c, "text": pos}}
    g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": c, "text": neg}}
    g["latent"] = {"class_type": "EmptyLatentImage",
                   "inputs": {"width": w, "height": h, "batch_size": 1}}
    g["ks1"] = {"class_type": "KSampler", "inputs": {
        "model": m, "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
        "seed": seed, "steps": int(cfg["steps"]), "cfg": float(cfg["cfg"]),
        "sampler_name": cfg.get("sampler", "euler_ancestral"),
        "scheduler": cfg.get("scheduler", "normal"), "denoise": 1.0}}
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
                                                             "value": 0, "max": info["steps1"], "pct": 0, "overall": 0})
                                pr["stage"] = "апскейл" if node in ("up2", "sc2", "up1", "sc1") else "подготовка"
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

async def comfy_generate(ch, s, content, seed, item):
    tag = str(random.randint(100000000, 999999999))
    pref_final, pref_base = "heretic_" + tag, "heretic_base_" + tag
    graph = build_graph(content, seed, pref_final, pref_base)
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
    print(f"[comfy] prompt {pid} | final={pref_final} base={pref_base}")
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

# ===== VLM-КРИТИК =====
def _shrink(path, max_side=1280):
    try:
        from PIL import Image
        import io
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
    print(f"[critique] режим: {CONFIG.get('vision_backend', 'local')} | файл: {os.path.basename(path)}")
    text = CRIT_T.format(theme=theme, prompt=prompt)
    raw, fmt = _shrink(path)
    b64 = base64.b64encode(raw).decode()
    content = [{"type": "text", "text": text},
               {"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64," + b64}}]
    if CONFIG.get("vision_backend", "local") == "lmstudio":
        try:
            await ensure_model(s)
            payload = {"model": MODEL_ID, "temperature": 0.2, "max_tokens": 1000,
                       "stream": False, "messages": [{"role": "user", "content": content}]}
            async with s.post(LM_URL + "/v1/chat/completions", json=payload) as r:
                j = await r.json()
            if j.get("error"):
                raise RuntimeError(str(j["error"])[:200])
            v = parse_json(_msg_text((j.get("choices") or [{}])[0].get("message", {})))
            print(f"[critique] LM Studio посмотрела за {time.time() - t0:.0f} с")
            return dict(empty, **{k: v[k] for k in empty if k in v})
        except Exception as e:
            print(f"[critique] LM Studio ошибка: {e}")
            return dict(empty, comment=f"LM Studio не смогла посмотреть: {e}")
    if not LLAMA_SERVER or not os.path.isfile(os.path.join(VLM_DIR, VLM_MODEL)):
        return dict(empty, comment="зрительная модель не настроена")
    print("[critique] запускаю локальный VLM (llama-server, Qwen3-VL-4B) на GPU", VLM_GPU)
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = VLM_GPU      # "0" — НЕ трогаем карту ComfyUI
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    proc = subprocess.Popen(
        [LLAMA_SERVER, "-m", os.path.join(VLM_DIR, VLM_MODEL),
         "--mmproj", os.path.join(VLM_DIR, VLM_MMPROJ),
         "-ngl", "99", "-c", "8192", "--host", "127.0.0.1", "--port", str(VLM_PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            await asyncio.sleep(1)
            try:
                async with s.get(f"http://127.0.0.1:{VLM_PORT}/health") as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
        else:
            return dict(empty, comment="зрительная модель не запустилась")
        payload = {"temperature": 0.2, "max_tokens": 1000,
                   "messages": [{"role": "user", "content": content}]}
        async with s.post(f"http://127.0.0.1:{VLM_PORT}/v1/chat/completions", json=payload) as r:
            j = await r.json()
        v = parse_json((j.get("choices") or [{}])[0].get("message", {}).get("content", ""))
        print(f"[critique] локальный VLM ответил за {time.time() - t0:.0f} с")
        return dict(empty, **{k: v[k] for k in empty if k in v})
    except Exception as e:
        return dict(empty, comment=f"осмотр сбоил: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

async def feedback(s, theme, prompt, v, path=None):
    desc = (v or {}).get("description") or ""
    if not desc and not path:
        return ""
    text = FB_T.format(theme=theme, prompt=prompt,
                       desc=desc or "(описания нет, смотри на картинку сам)",
                       defects=", ".join((v or {}).get("defects") or []) or "нет")
    if CONFIG.get("vision_backend", "local") == "lmstudio" and path and os.path.isfile(path):
        try:
            await ensure_model(s)
            raw, fmt = _shrink(path)
            b64 = base64.b64encode(raw).decode()
            payload = {"model": MODEL_ID, "temperature": 0.5, "max_tokens": 700,
                       "stream": False, "messages": [{"role": "user", "content": [
                           {"type": "text", "text": text},
                           {"type": "image_url",
                            "image_url": {"url": f"data:image/{fmt};base64," + b64}}]}]}
            async with s.post(LM_URL + "/v1/chat/completions", json=payload) as r:
                j = await r.json()
            if not j.get("error"):
                t = _msg_text((j.get("choices") or [{}])[0].get("message", {}))
                if t:
                    return t
        except Exception as e:
            print("[feedback] картинка не отправилась:", e)
    try:
        return await llm(s, [{"role": "user", "content": text}], 0.5, 700)
    except Exception as e:
        return f"(оценка не получена: {e})"

# ===== СЕРИЯ / УТОЧНЕНИЯ (на чат) =====
async def run_clarify(ch, user_text):
    ch["busy"] = True
    epoch = ch["epoch"]
    dlg = ch["dialog"]
    dlg["theme"] = user_text
    dlg["history"] = [{"role": "user", "content": user_text}]
    dlg["stage"] = "idle"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as s:
            say(ch, "b", "Готовлю вопросы...")
            mem = load_mem()
            msgs = [{"role": "system", "content": ASK_SYS}]
            if mem.get("summary"):
                msgs.append({"role": "user", "content": "[ПРОШЛЫЙ ОПЫТ]\n" + mem["summary"]
                             + "\n(это фон, спрашивай только про новую тему)"})
            msgs += chat_msgs(ch, 6)
            msgs += dlg["history"]
            txt = await llm(s, msgs, 0.7, 4096, True)
            if ch["epoch"] != epoch:
                return
            raw = parse_json(txt).get("questions") or []
            if isinstance(raw, dict):
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
                    lines.append("    " + "    ".join(f"{j}) {o}" for j, o in enumerate(opts, 1)))
            if not clean:
                raise RuntimeError("модель не задала вопросов, попробуй ещё раз")
            dlg["history"].append({"role": "assistant",
                                   "content": json.dumps({"questions": clean}, ensure_ascii=False)})
            dlg["stage"] = "wait"
            chat_add(ch, "user", user_text)
            chat_add(ch, "assistant", "Задал уточняющие вопросы: " +
                     "; ".join(q["q"] for q in clean)[:800])
            say(ch, "q", f"Вопросов: {len(clean)}\n\n" + "\n".join(lines)
                + "\n\nОтветь номерами: «1-2, 2-5, 3-1» (вопрос-вариант). Можно не на все."
                + "\nМожно ответить словами. «генерируй» — решу сам.")
    except Exception as e:
        if ch["epoch"] == epoch:
            say(ch, "e", f"Ошибка: {e}")
        dlg["stage"] = "idle"
    finally:
        ch["busy"] = False

async def run_series(ch, user_text, direct=False, no_questions=False):
    ch["busy"] = True
    ch["cancel"] = False
    epoch = ch["epoch"]
    dlg = ch["dialog"]
    last = ch["last"]
    mem = load_mem()
    answers, intent = None, "new"
    if dlg["stage"] == "wait":
        answers = user_text
        user_text = dlg["theme"] or user_text
        dlg["stage"] = "idle"
    else:
        intent = detect_intent(ch, user_text)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3600)) as s:
            await fetch_assets(s)
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
                        {"role": "system", "content": EDIT_SYS}] + history + [
                       {"role": "user", "content": EDIT_T.format(
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
                msgs = [{"role": "system", "content": SYS},
                        {"role": "user", "content": memctx + "\n[ЗАПРОС]\n" + user_text}]
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
                plan = parse_json(await llm(s, msgs, 0.5 if intent == "edit" else 0.7, 1200, True))
            if ch["epoch"] != epoch:
                return
            if not plan.get("images"):
                qs = plan.get("questions") or []
                if isinstance(qs, str):
                    qs = [qs]
                if qs and intent == "new" and not no_questions:
                    dlg["stage"] = "wait"
                    dlg["theme"] = user_text
                    dlg["history"].append({"role": "user", "content": answers or user_text})
                    dlg["history"].append({"role": "assistant",
                                           "content": json.dumps(qs, ensure_ascii=False)})
                    say(ch, "q", "\n".join(f"{i}. {q}" for i, q in enumerate(qs, 1)))
                    return
            dlg["history"], dlg["theme"] = [], ""
            imgs = plan.get("images", [])
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
                        "file": None, "verdict": "", "ok": None, "retry": False}
                ch["items"].append(item)
                fin, base_img = await comfy_generate(ch, s, p, seed, item)
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
                        prompt=p, defects=", ".join(v.get("defects", [])))}], 0.4, 600, True)).get("prompt", p)
                    seed += 1
                    fin, base_img = await comfy_generate(ch, s, p, seed, item)
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
    except Exception as e:
        if ch["epoch"] == epoch:
            say(ch, "e", f"Ошибка: {e}")
    finally:
        ch["busy"] = False
        ch["cancel"] = False
        same = (ch["epoch"] == epoch)
        if same: ch["progress"] = None
        stale = [it for it in ch["items"] if same and it.get("status") not in ("готово", "брак")]
        for it in stale: it["status"], it["ok"] = "брак", False
        for it in ch["items"]: it.pop("progress", None)
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
    ideas = await generate_ideas()
    if ideas:
        return web.json_response({"ok": True, "ideas": ideas})
    return web.json_response({"ok": False, "error": "LM Studio не ответил"})

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
    return web.json_response({"ok": bool(ch)})

async def h_reset(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    if not ch:
        return web.json_response({"ok": False})
    ch["epoch"] += 1                 # обрываем текущую задачу чата
    ch["cancel"] = True
    ch["messages"], ch["items"], ch["turns"] = [], [], []
    ch["dialog"] = {"stage": "idle", "theme": "", "history": []}
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
    ch["dialog"] = {"stage": "idle", "theme": "", "history": []}
    say(ch, "b", "Прошлый кадр забыт. Следующий запрос будет как новая тема.")
    return web.json_response({"ok": True})

async def h_chat(r):
    d = await r.json()
    ch = CHATS.get(d.get("id"))
    t = (d.get("text") or "").strip()
    if not ch or not t:
        return web.json_response({"ok": False})
    if ch["busy"]:
        return web.json_response({"ok": False, "busy": True})
    if not ch["turns"] and re.match(r"^Чат \d+$", ch["title"] or ""):
        ch["title"] = t[:42] + ("…" if len(t) > 42 else "")
    say(ch, "u", t)
    if ch["dialog"]["stage"] == "wait":
        if t.lower() in ("генерируй", "пропусти", "skip", "давай", "сам"):
            t = "Уточнения пропускаю — прими все решения сам."
        asyncio.create_task(run_series(ch, t))
        return web.json_response({"ok": True})
    mode = detect_direct(t)
    if mode == "raw":
        asyncio.create_task(run_series(ch, t, direct=True))
    elif mode == "skip":
        asyncio.create_task(run_series(ch, t, no_questions=True))
    elif detect_intent(ch, t) == "edit":
        asyncio.create_task(run_series(ch, t))
    else:
        asyncio.create_task(run_clarify(ch, t))
    return web.json_response({"ok": True})

async def h_cfg_get(r):
    if r.query.get("refresh") or not ASSETS["loras"] or not ASSETS["upscales"]:
        ASSETS.update(ckpts=[], loras=[], upscales=[])
        await fetch_assets()
    return web.json_response({"config": CONFIG, "assets": ASSETS,
                              "resolutions": list(RESOLUTIONS.keys())})

async def h_cfg_set(r):
    d = await r.json()
    for k in ("ckpt", "resolution", "upscaler", "steps", "cfg", "max_images", "use_critic",
              "sampler", "scheduler", "clip_skip", "quality_on", "solo",
              "hires_on", "hires_scale", "hires_denoise", "hires_steps", "hires_cfg",
              "hires_sampler", "hires_scheduler", "hires_max_px", "save_dir", "feedback_on", "vision_backend"):
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
/* вкладки чатов */
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
.logempty{position:absolute;left:0;right:0;top:92px;bottom:120px;display:none;flex-direction:column;
align-items:center;justify-content:center;gap:14px;padding:24px;text-align:center;pointer-events:none}
.logempty.show{display:flex}
.logempty p{margin:0;color:var(--mute);font-size:12.5px;max-width:330px;line-height:1.7}
.seeds{display:flex;flex-direction:column;align-items:stretch;gap:7px;width:100%;max-width:430px;
max-height:38vh;overflow-y:auto;padding-right:4px;pointer-events:auto}
.seeds button{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:12px;
padding:8px 12px;font-size:11.5px;line-height:1.45;text-align:left;cursor:pointer;transition:.18s}
.seeds button:hover{color:#fff;border-color:var(--mag);background:rgba(255,45,149,.14);transform:translateY(-2px)}
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
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#fff;
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
<button class="mini" id="newBtn" title="Забыть последний кадр, начать новый сюжет">новая тема</button>
<button class="mini" id="resetBtn" title="Полностью очистить этот чат">сброс</button>
</div>
<div class="chattabs" id="chatTabs"></div>
<div class="log" id="log"></div>
<div class="logempty" id="logEmpty">
<svg width="72" height="72" viewBox="0 0 72 72" fill="none">
<defs><linearGradient id="gq" x1="0" y1="0" x2="72" y2="72">
<stop stop-color="#ff2d95"/><stop offset=".5" stop-color="#8b5cff"/><stop offset="1" stop-color="#22e0ff"/>
</linearGradient></defs>
<rect x="7" y="14" width="58" height="40" rx="12" stroke="url(#gq)" stroke-width="2.5"/>
<path d="M22 54l-4 10 14-10" stroke="url(#gq)" stroke-width="2.5" stroke-linejoin="round"/>
<circle cx="26" cy="34" r="3.5" fill="#ff2d95"/><circle cx="36" cy="34" r="3.5" fill="#8b5cff"/>
<circle cx="46" cy="34" r="3.5" fill="#22e0ff"/>
</svg>
<p>Опиши сцену словами. Модель сама задаст уточняющие вопросы — на них можно отвечать кликом по вариантам. Вкладки сверху — отдельные чаты, «+» — новый.</p>
<div class="seeds" id="seedIdeas"></div>
<button class="mini" id="regenIdeasBtn" title="Сгенерировать новые идеи через LM Studio"
style="margin-top:10px;pointer-events:auto;position:relative;z-index:5;">🎲 новые идеи</button>
</div>
<button class="jump" id="jumpBtn">↓ новые сообщения <span id="jumpN">0</span></button>
<div class="composer">
<textarea id="inp" rows="1" spellcheck="false" placeholder="Опиши сцену… Enter — отправить, Shift+Enter — перенос строки"></textarea>
<div class="cbar">
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
<label class="switch"><input type="checkbox" id="crit"><i></i><span>VLM-критик: анатомия и замысел</span></label>
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
function sameMsg(a,b){return !!a&&!!b&&a.role===b.role&&a.text===b.text;}
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
rendered=msgs.map(function(m){return{role:m.role,text:m.text};});
$('logEmpty').classList.toggle('show',msgs.length===0);
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
if(!t)return;
if(busy){toast('Этот чат занят — дождись ответа или открой другой чат','warn');return;}
if(!activeId){toast('Чат не выбран','bad');return;}
if(txt==null){inp.value='';autoGrow();updateCount();}
pinned=true;unread=0;updateJump();
try{
var j=await post('/api/chat',{id:activeId,text:t});
if(!j.ok){toast('Не принято: идёт генерация','warn');}
else{tick();}
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

var IDEAS={{IDEAS}};

function renderSeedIdeas(){
    var box=$('seedIdeas');
    if(!box){console.warn('[ideas] #seedIdeas не найден');return;}
    box.innerHTML='';
    var arr=IDEAS||[];
    console.log('[ideas] рисую',arr.length,'идей:',arr);
    arr.forEach(function(s){
        var b=node('button',null,s);
        b.addEventListener('click',function(){
            inp.value=s; autoGrow(); updateCount(); inp.focus();
        });
        box.appendChild(b);
    });
}

var regenBusy=false;
$('regenIdeasBtn').addEventListener('click',async function(){
    if(regenBusy){toast('Уже генерирую...','warn');return;}
    regenBusy=true;
    var btn=this;
    btn.textContent='⏳ генерирую...';
    try{
        var r=await fetch('/api/ideas/regen',{method:'POST'});
        var j=await r.json();
        console.log('[ideas] ответ:',j);
        if(j.ok && Array.isArray(j.ideas) && j.ideas.length){
            IDEAS=j.ideas;
            renderSeedIdeas();
            toast('Новые идеи: '+IDEAS.length);
        }else{
            toast('Не удалось: '+(j.error||'пустой ответ'),'bad');
        }
    }catch(e){
        console.error('[ideas] ошибка:',e);
        toast('Нет связи: '+e,'bad');
    }finally{
        btn.textContent='🎲 новые идеи';
        regenBusy=false;
    }
});

renderSeedIdeas();

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
var CFG={},ASSETS={ckpts:[],loras:[],upscales:[]},RESLIST=[],saveTimer=null,loaded=false;
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
use_critic:$('crit').checked,loras:rows};
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
['ckpt','up'].forEach(function(id){$(id).addEventListener('change',markDirty);});
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

/* телеметрия: температура + загрузка + потребление (Вт) */
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

def find_llama():
    if LLAMA_SERVER:
        return LLAMA_SERVER
    c = glob.glob(os.path.expanduser("~/.lmstudio/extensions/backends/**/llama-server.exe"), recursive=True)
    return max(c, key=os.path.getmtime) if c else ""

async def _generate_ideas_bg():
    await asyncio.sleep(3)          # дадим серверу и LM Studio стартовать
    if load_ideas() is None:
        print("[ideas] файл идей не найден — генерирую через LM Studio...")
        await generate_ideas()
    else:
        print("[ideas] идеи загружены из файла:", len(load_ideas()))

async def on_start(app):
    app["ws_task"] = asyncio.create_task(ws_loop())
    asyncio.create_task(_generate_ideas_bg())

async def on_cleanup(app):
    app["ws_task"].cancel()

if __name__ == "__main__":
    LLAMA_SERVER = find_llama()
    print("llama-server:", LLAMA_SERVER or "НЕ НАЙДЕН — локальная критика будет пропущена")
    if not os.path.isdir(CONFIG["output_dir"]):
        print("!!! output_dir не существует:", CONFIG["output_dir"])
    print("!!! Запускай ComfyUI так: python main.py --cuda-device 1 --highvram")
    print("!!! LM Studio ограничь GPU 0 (Hardware -> выключи 3070 Ti, или CUDA_VISIBLE_DEVICES=0)")
    app = web.Application()
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", h_root),
        web.post("/api/ideas/regen", h_ideas_regen),
        web.get("/api/state", h_state),
        web.post("/api/chat", h_chat),
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