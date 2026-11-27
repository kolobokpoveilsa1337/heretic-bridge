# bridge.py v2 — Qwen (LM Studio, 90HX) -> сценарий -> seed-теги -> TIPO (llama-server, 90HX) -> Illustrious XL (3070 Ti)
# 4 режима: сюжет / исходник / промт / вопросы. Без ролей исходников, без словарей тегов, без data/.
import asyncio, base64, ctypes, glob, io, json, os, platform, random, re, shutil, subprocess, time, uuid
import aiohttp
from aiohttp import web
try:
    from PIL import Image
except Exception:
    Image = None

# ===== ПУТИ =====
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MEM_FILE   = os.path.join(BASE_DIR, "memory.json")
CFG_FILE   = os.path.join(BASE_DIR, "config.json")
LLM_DEBUG  = os.path.join(BASE_DIR, "llm_debug.json")
CHATS_FILE = os.path.join(BASE_DIR, "chats.json")
IDEAS_FILE = os.path.join(BASE_DIR, "ideas.json")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

LM_URL    = "http://127.0.0.1:1234"     # LM Studio (Qwen3.5): сценарист + критик
COMFY_URL = "http://127.0.0.1:8188"     # ComfyUI: теггер (Ollama-нода) + Illustrious
WEB_PORT  = 7860
MIN_TOKENS = 2048
HIST_TURNS = 12
CLIP_LEN   = 2200
SDXL_BUCKETS = [(1216,832),(832,1216),(1344,768),(768,1344),(1536,640),
                (1024,1024),(1152,896),(896,1152),(1280,768),(768,1280)]

# ============================================================================
# ПРОМПТЫ. Над каждым — что это, кто вызывает, вход/выход, связи.
# ============================================================================

# --- IDEA_SYS ----------------------------------------------------------------
# Генератор «Идей». Вызывается generate_ideas() при старте и по кнопке «новые идеи».
# Вход: режим (реализм/аниме | фантастика) + короткие тизеры прошлых идей.
# Выход: JSON {ideas:[{short, full}]} через грамматику S_IDEAS.
# Связи: short показывается в панели «Идеи»; full вставляется в поле ввода чата,
#        где пользователь правит его и отправляет в режиме «сюжет» (run_scene).
#        Примеров НЕТ специально — модель придумывает сама, а не копирует шаблоны.
IDEA_SYS = (
    "Ты придумываешь сцены для аниме-картинок. Режим: {mode_line}\n"
    "Придумай {n} РАЗНЫХ сцен. Для каждой верни два поля:\n"
    "short — живой тизер, 1-3 предложения до 30 слов, без мелких деталей;\n"
    "full — ТОТ ЖЕ сюжет, полный сценарий по-русски, каждый блок с новой строки:\n"
    "Место: / Персонаж: (пол, возраст, рост, телосложение, грудь и бёдра, кожа, ногти и тд) / "
    "Внешность: (волосы, глаза, украшения и тд) / Одежда: (слои, ткани, цвета, состояние, обувь и тд) / "
    "Поза и действие: (одно физически возможное действие, чем занят каждый рука, контакт со средой) / "
    "Настроение: (эмоция, мимика, взгляд) / Фон: (10-22 конкретных предметов, материалы) / "
    "Атмосфера: (воздух, частицы, блики, тактильные детали) / "
    "Время и свет: (время суток, погода, источники и направление света, тени) / "
    "Камера: (план, ракурс, где фокус) / Откровенность:\n"
    "Правила: персонаж девушка; один человек, одно действие, одно место; ты всегда пишеш NSFW/сексуальный/порнографические сцены; "
    "свет, погода, одежда и поза согласованы; без английских тегов, без вариантов и списков; "
    "откровенность - всегда спрашиваеш какая должна быть, если режим не требует иного; темы не повторяй.\n"
    'Ответь ТОЛЬКО JSON: {{"ideas":[{{"short":"...","full":"..."}}]}}'
)
IDEA_MODES = {
    "real":    "реализм или аниме-повседневность: обычные люди, быт, работа, дом, улица. "
               "Лёгкая аниме-стилизация допустима (необычный цвет волос, кошачьи ушки). Без магии, монстров, роботов.",
    "fantasy": "фантастика: фэнтези, киберпанк, космос, постапокалипсис. Один мир на сцену, "
               "внутренняя логика мира соблюдена, не смешивай магию с бытовой техникой.",
}

# --- SCENE_SYS ---------------------------------------------------------------
# Сценарист (режимы 1 и 4). Вызывается run_scene()/answer-веткой run_ask().
# Вход: свободный текст пользователя (+ ответы анкеты в режиме 4) + саммари памяти.
# Выход: ПОЛНЫЙ сценарий по-русски в 12 блоках (plain text, без JSON — быстрее).
# Связи: результат показывается карточкой «сценарий» (роль 's') и ждёт согласия
#        (stage='approve'). После «генерируй» уходит в make_tags() -> теггер ComfyUI.
SCENE_SYS = (
    "Ты сценарист кадров для аниме-генерации. Из запроса пользователя собери ОДИН цельный кадр "
    "и додумай всё, что он не указал. Сцена 18+ если пользователь не требует иного.\n"
    "Верни ТОЛЬКО текст сценария по-русски, строго блоками, каждый с новой строки:\n"
    "Место: конкретная локация\n"
    "Персонаж: пол, возраст, рост, телосложение, грудь и бёдра, кожа, ногти и тд\n"
    "Внешность: волосы (длина, цвет, причёска), глаза (цвет, форма), украшения\n"
    "Одежда: слои, ткани, цвета, состояние, обувь — или нагота и тд\n"
    "Поза и действие: одно физически возможное действие, чем занят каждый рука, контакт со средой\n"
    "Настроение: эмоция, мимика, взгляд\n"
    "Фон: 10-22 конкретных предметов, материалы, глубина\n"
    "Атмосфера: воздух, частицы, блики, тактильные детали\n"
    "Время и свет: время суток, погода, источники и направление света, тени\n"
    "Камера: план (крупный/поясной/по колени/общий), ракурс (сбоку/снизу/сверху/через плечо), где фокус\n"
    "Стиль: flat anime, линии, палитра\n"
    "Откровенность: ровно та, что просил пользователь; без запроса — нагота на 90% из 100%\n"
    "Правила: один персонаж по умолчанию; свет, погода, одежда и поза не противоречат друг другу; "
    "никаких тегов, вариантов и вопросов; 180-320 слов. Если запрос уже похож на готовый сценарий — "
    "сохрани формулировки и дополни только недостающее."
)

# --- SCENE_EDIT_SYS ----------------------------------------------------------
# Правка сценария. Вызывается, когда пользователь в stage='approve' пишет правки,
# или в режиме 1 короткой правкой правит прошлый кадр, или в режиме 2 правит прошлый исходник без новой картинки.
# Вход: текущий полный сценарий + просьба. Выход: полный обновлённый сценарий в тех же блоках.
# Связи: результат снова proponируется (stage='approve') либо сразу генерируется (режим 2).
SCENE_EDIT_SYS = (
    "Тебе дан текущий сценарий кадра и правки пользователя. Верни ПОЛНЫЙ обновлённый сценарий "
    "в тех же блоках (Место / Персонаж / Внешность / Одежда / Поза и действие / Настроение / Фон / "
    "Атмосфера / Время и свет / Камера / Стиль / Откровенность).\n"
    "Меняй ТОЛЬКО то, о чём просили, остальное оставляй дословно. Несколько просьб — выполни все. "
    "Если правка противоречит старому — побеждает правка, старое противоречие убери. "
    "Вопросов не задавай. Только текст сценария, без пояснений."
)

# --- SRC_SYS -----------------------------------------------------------------
# Режим 2 «Исходник». Вызывается run_source() с картинками (VLM-вызов Qwen).
# Вход: 1-4 изображения + текст правок. Выход: полный сценарий в тех же блоках.
# Связи: сценарий -> make_tags() -> Illustrious. Если включён src_img2img, первая
#        картинка ещё и уходит в ComfyUI как img2img-основа (denoise=src_denoise).
SRC_SYS = (
    "На изображениях — исходники пользователя, которые он хочет переделать. "
    "Опиши ВСЁ, что реально видно, максимально подробно и собери ОДИН сценарий по-русски блоками:\n"
    "Место / Персонаж (пол, возраст, рост, телосложение, грудь и бёдра, кожа, ногти и тд) / "
    "Внешность (волосы, глаза, украшения и тд) / Одежда (слои, ткани, цвета, состояние, обувь и тд) / "
    "Поза и действие / Настроение (мимика, взгляд) / Фон (10-22 предметов, материалы) / "
    "Атмосфера / Время и свет / Камера (план, ракурс, фокус) / Стиль / Откровенность.\n"
    "Затем примени правки пользователя. Что правок нет — оставь КАК НА ИСХОДНИКЕ: "
    "фон, предметы, внешность, одежду, свет, ракурс. Не выдумывай невидимое. Только текст сценария."    
)

# --- SEED_SYS ----------------------------------------------------------------
# Переводчик сценария в «ядро» тегов. Вызывается make_tags(), шаг 1 (Qwen3.5 на CMP 90HX).
# Вход: полный русский сценарий. Выход: JSON {tags} — 50-100 английских danbooru-тегов.
# Связи: ядро уходит в TIPO-200M-ft, который достраивает качество/мету/рейтинг и детали.
#        Поэтому quality/meta/artist-теги здесь ЗАПРЕЩЕНЫ — их добавит TIPO.
SEED_SYS = (
    "Translate the Russian scene into CORE danbooru tags for an anime checkpoint. "
    'Output ONLY JSON {"tags":"..."}: 50-100 lowercase english tags, one line, commas. '
    "Include: solo + 1girl/1boy; body/skin; hair; eyes/face; clothes layer by layer OR explicit nude tags; "
    "expression/gaze; pose/action; camera (upper body, cowboy shot, full body, from side, from below); "
    "10-22 background objects; time/light; mood; flat color, cel shading. "
    "Translate EVERY concrete object and state (wet white shirt, torn black pantyhose). "
    "Do NOT add: masterpiece, best quality, newest, absurdres, rating tags, artist names, weights, negations. "
    "Do NOT invent anything not in the scene. If the scene asks nudity/sex use direct tags "
    "(nude, nipples, pussy, spread legs, sex)."
)


# --- TIPO_TEMPLATE -----------------------------------------------------------
# Формат входа TIPO по карточке KBlueLeaf/TIPO: seed-теги + плейсхолдеры meta/rating,
# которые модель заполняет сама (quality + rating). Если карточка твоей версии GGUF
# показывает другой шаблон — поправь эту строку.
TIPO_TEMPLATE = "{seed},\n<|meta|>, <|rating|>"

# --- FALLBACK_SYS ------------------------------------------------------------
# Аварийная достройка тегов Qwen'ом, если llama-server с TIPO не ответил.
# Вход: seed-теги. Выход: одна строка полных тегов. Связи: тот же clean_tags() -> KSampler.
FALLBACK_SYS = (
    "Extend these core danbooru tags into a full Illustrious XL prompt: 60-160 lowercase english tags, "
    "one line, commas. Keep every given tag and fact, add background objects, materials, lighting, "
    "camera and style tags. No sentences, no markdown, no negations, no contradictions. "
    "Do NOT write masterpiece/best quality/newest/absurdres."
)


# --- ASK_SYS -----------------------------------------------------------------
# Анкета (режим 4). Вызывается run_ask(). Вход: тема пользователя.
# Выход: JSON {questions:[{q,options}]} (S_QUESTIONS). Рендерится карточкой вопросов в UI.
# Связи: ответы «номер-вариант» возвращаются в run_scene() как доп. контекст сценариста.
ASK_SYS = (
    "Ты помогаешь придумать картинку. Пользователь дал тему. Промпт писать НЕЛЬЗЯ.\n"
    "Задай 10-30 уточняющих вопросов по теме. Язык простой, бытовой, вопрос до 8 слов.\n"
    "Запрещённые слова: ракурс, кадрирование, композиция, промпт, теги, лора, рендер, шейдинг, "
    "контровой свет, глубина резкости, анатомия, референс, палитра, данbooru.\n"
    "К каждому вопросу 5-7 готовых вариантов ответа, 1-4 слова, последний всегда «свой вариант».\n"
    "Темы: кто на картинке, возраст, телосложение, волосы, глаза, одежда или нагота, степень откровенности, "
    "что делает, поза, где, что вокруг, время суток, погода, свет, настроение, цвета, насколько крупно человек, стиль.\n"
    "Не спрашивай о том, что пользователь уже написал.\n"
    'Ответь ТОЛЬКО JSON: {"questions":[{"q":"кто на картинке?","options":["девушка","парень","фурри","свой вариант"]}]}'
)

# --- CRIT_T ------------------------------------------------------------------
# Зрительный критик (Qwen VLM). Вызывается critique() после генерации.
# Вход: картинка (base64) + сцена + теги. Выход: JSON S_CRIT {ok,defects,comment,description}.
# Связи: ok=False -> ретрай с FIX_T; description -> FB_T (оценка) и в last.shots для будущих правок.
CRIT_T = (
    "Ты строгий зрительный критик аниме-изображений.\n"
    "ЗАМЫСЕЛ (сценарий):\n{theme}\n\nТЕГИ ПРОМПТА: {prompt}\n"
    "Описывай ТОЛЬКО то, что реально видно. Не выдумывай детали.\n"
    "Если в сценарии запрошена нагота или эротика — это НЕ дефект; оценивай только качество анатомии и поз.\n"
    "Проверь: 1) анатомия — число персонажей, головы, руки/ноги, пальцы, позы; "
    "2) соответствие сценарию и тегам; 3) стиль flat anime, не фотореализм; 4) композиция, свет, фон.\n"
    'Ответь ТОЛЬКО JSON: {{"ok":true,"defects":[],"comment":"кратко по-русски",'
    '"description":"подробно по-русски: кто, волосы, глаза, одежда или нагота, поза, ракурс, фон, свет, качество линий"}}'
)

# --- FIX_T -------------------------------------------------------------------
# Ретрай по дефектам. Вызывается в run_generate(), когда критик ok=False.
# Вход: текущие теги + дефекты. Выход: JSON {prompt} (S_FIX). Сценарий НЕ меняется, seed+1.
FIX_T = ("Промпт аниме-генерации: {prompt}\nКритик нашёл дефекты: {defects}\n"
         "Перепиши теги, чтобы убрать дефекты. Сохрани тему и стиль. "
         'Ответь только JSON: {{"prompt":"..."}}')

# --- FB_T --------------------------------------------------------------------
# Итоговая оценка кадра (стримится в чат). Вызывается feedback().
# Вход: сцена + теги + описание критика + картинка. Выход: 5-8 строк текста.
# Связи: пишется в item.verdict, sidecar .txt и память; влияет на будущие сценарии через саммари.
FB_T = ("Ты арт-директор. Сценарий и промпт придумал ты, картинка сгенерирована.\n"
        "СЦЕНАРИЙ:\n{theme}\nТЕГИ: {prompt}\n"
        "ЧТО ВИДНО НА КАРТИНКЕ (описание критика): {desc}\nДЕФЕКТЫ: {defects}\n\n"
        "Дай честную оценку по-русски, 5-8 строк, без воды:\n"
        "1) удалась ли картинка и соответствует ли сценарию — да/частично/нет\n"
        "2) что получилось хорошо, конкретно\n3) что не так, конкретно\n"
        "4) какие 3-6 тегов добавить или убрать в следующий раз\n"
        "Опирайся только на описание и картинку. Обычный текст, без JSON и markdown.")

# --- SUM_T -------------------------------------------------------------------
# Саммари памяти. Вызывается в конце серии. Вход: старое саммари + последние записи.
# Выход: текст до 300 токенов. Связи: подмешивается сценаристу как «прошлый опыт».
SUM_T = ("Старое саммари работы: {old}\nНовые генерации: {new}\n"
         "Обнови саммари (до 300 токенов, по-русски): какие темы рисовали, что получалось, какие дефекты повторялись. "
         "Ответь только текстом саммари.")

# --- QUALITY / NEGATIVE / META_TAGS -------------------------------------------
# QUALITY — авто-префикс положительного промпта (мост добавляет сам, теггеру писать запрещено).
# NEGATIVE — фиксированный негативный промпт (анатомия/лишние люди/фотореализм), отдельный CLIPTextEncode.
# META_TAGS — мусор-мета-теги, вырезаются clean_tags() из ответа теггера.
QUALITY = ("masterpiece, best quality, very aesthetic, newest, absurdres, "
           "beautiful detailed eyes, detailed face, detailed hair, detailed fabric texture, "
           "clean lines, crisp lineart, anime coloring, cel shading, sharp focus")
NEGATIVE = ("multiple people, two people, three people, multiple girls, 2girls, 3girls, multiple boys, 2boys, "
            "crowd, group of people, extra person, background people, clone, duplicate character, twins, "
            "two heads, multiple heads, extra head, missing head, headless, fused heads, floating head, "
            "two faces, extra face, multiple faces, deformed face, ugly face, poorly drawn face, "
            "asymmetrical face, poorly drawn eyes, asymmetric eyes, uneven eyes, cross-eyed, extra eyes, "
            "deformed iris, deformed pupils, extra ears, missing ears, deformed ears, extra nose, missing nose, "
            "extra mouth, missing mouth, deformed mouth, bad teeth, extra teeth, long neck, twisted neck, "
            "(bad anatomy:1.2), bad proportions, gross proportions, anatomical nonsense, mutated, mutation, "
            "deformed body, disfigured, malformed body, malformed limbs, (extra limbs:1.2), missing limbs, "
            "disconnected limbs, floating limbs, long body, elongated torso, missing arms, extra arms, third arm, "
            "missing legs, extra legs, third leg, fused legs, poorly drawn legs, twisted legs, backwards legs, "
            "backwards hands, extra joints, broken wrist, twisted wrist, missing hands, extra hands, third hand, "
            "fused hands, poorly drawn hands, mutated hands, (bad hands:1.2), missing fingers, (extra fingers:1.3), "
            "six fingers, too many fingers, (fused fingers:1.3), fewer fingers, malformed fingers, long fingers, "
            "extra thumb, missing thumb, extra digits, missing toes, extra toes, fused toes, bad feet, "
            "poorly drawn feet, extra feet, realistic, photorealistic, photo, photograph, live action, 3d, cgi, "
            "3d render, western cartoon")
META_TAGS = {"8k","uhd","hdr","masterpiece","best quality","ultra detailed","intricate details",
             "perfect composition","award winning","very aesthetic","absurdres","newest"}

# ============================================================================
# КОНФИГ
# ============================================================================
RESOLUTIONS = {
    "1216x832 (3:2 альбом)":  (1216,832),  "832x1216 (2:3 портрет)": (832,1216),
    "1344x768 (16:9)":        (1344,768),  "768x1344 (9:16)":        (768,1344),
    "1536x640 (2.4:1 кино)":  (1536,640),  "1024x1024 (квадрат)":    (1024,1024),
    "1408x704 (2:1 лежачий)": (1408,704),  "704x1408 (2:1 стоячий)": (704,1408),
    "1280x720 (HD)":          (1280,720),  "1920x1080 (FullHD)":     (1920,1080),
    "1024x1536 (2:3 портрет)":(1024,1536), "1536x1024 (3:2 альбом)": (1536,1024),
    "custom":                 None,   # свой размер — custom_wh
}
DETAIL_SLOTS = [
    {"key":"face","title":"лицо","on":True,"model":"","denoise":0.35,"steps":20,"cfg":5.0,"guide":512},
    {"key":"eyes","title":"глаза","on":True,"model":"","denoise":0.30,"steps":16,"cfg":4.5,"guide":512},
    {"key":"hands","title":"руки","on":True,"model":"","denoise":0.40,"steps":20,"cfg":5.0,"guide":512},
    {"key":"custom","title":"свой","on":False,"model":"","denoise":0.35,"steps":20,"cfg":5.0,"guide":512},
]
DET_HINTS = {"face":("face",),"eyes":("eye",),"hands":("hand",)}
DEFAULT_CONFIG = {
    "output_dir": r"E:\ComfyUI_windows_portable\ComfyUI\output",
    "ckpt": "Diving-Illustrious Flat Anime Paradigm Shift_v8.0+VAE.safetensors",
    "resolution": "1408x704 (2:1 лежачий)", "custom_wh": [1408,704],
    "fw_fh": [2000,1000],
    "steps": 45, "cfg": 5.5, "max_images": 1,
    "use_critic": True, "feedback_on": True,
    "upscaler": "4x-AnimeSharp.pth", "save_dir": "",
    "sampler": "dpmpp_2m", "scheduler": "karras", "clip_skip": 2,
    "seed": 0, "seed_mode": "randomize", "denoise": 1.0,
    "quality_on": True, "artist": "",
    "hires_on": True, "hires_scale": 1.5, "hires_denoise": 0.4,
    "hires_steps": 20, "hires_cfg": 5.0,
    "hires_sampler": "dpmpp_2m_sde", "hires_scheduler": "karras", "hires_max_px": 2600000,
    # исходник (режим 2)
    "src_img2img": True, "src_denoise": 0.55, "src_auto_size": True,
    # теггер TIPO (llama-server, автозапуск мостом)
    "tag_model": "TIPO-200M-ft.Q8_0.gguf",   # имя файла, справочно
    "tag_url": "http://127.0.0.1:8080",       # адрес llama-server
    "tag_temp": 0.4, "tipo_tokens": 600, "tag_fallback": True, "tipo_on": True,
    "tipo_autostart": True,
    "tipo_exe": r"C:\121\llama\llama-server.exe",
    "tipo_model_path": r"C:\121\TIPO-200M-ft.Q8_0.gguf",
    "tipo_device": "0",   # индекс GPU как в nvidia-smi -L (CMP 90HX); "cpu" = без GPU
    "loras": [
        {"name":"rendering_detailer_base10-000400.safetensors","strength":0.8,"on":True},
        {"name":"sexy_details_v5.safetensors","strength":0.6,"on":True},
        {"name":"ATRex_style-12V2Rev.safetensors","strength":0.5,"on":True},
        {"name":"omi-s2-illustriousxl-lora-nochekaiser.safetensors","strength":0.6,"on":True},
        {"name":"anima-highres-aesthetic-boost.safetensors","strength":0.7,"on":True},
    ] + [{"name":"","strength":0.8,"on":False}]*5,
    "detail_on": True, "detail_feather": 8, "detail_max_size": 1024,
    "detail_seed_mode": "same", "detailers": [dict(d) for d in DETAIL_SLOTS],
}
def norm_detailers(lst):
    base = [dict(d) for d in DETAIL_SLOTS]; saved = {}
    for d in (lst or []):
        if isinstance(d, dict) and d.get("key"): saved[str(d["key"])] = d
    for d in base:
        s = saved.get(d["key"]) or {}
        for k in ("on","model","denoise","steps","cfg","guide"):
            if k in s: d[k] = s[k]
    return base
def load_config():
    d = dict(DEFAULT_CONFIG)
    if os.path.isfile(CFG_FILE):
        try:
            c = json.load(open(CFG_FILE, encoding="utf-8"))
            for k in DEFAULT_CONFIG:
                if k in c: d[k] = c[k]
        except Exception: pass
    d["detailers"] = norm_detailers(d.get("detailers"))
    if not isinstance(d.get("loras"), list) or len(d["loras"]) < 10:
        d["loras"] = [dict(x) for x in DEFAULT_CONFIG["loras"]]
    return d
def save_config(c):
    json.dump(c, open(CFG_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
CONFIG = load_config()

# ============================================================================
# JSON / СХЕМЫ / LLM
# ============================================================================
def _repair_json(t):
    in_str = esc = False; stack = []
    for ch in t:
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        elif ch == '"': in_str = True
        elif ch in "{[": stack.append(ch)
        elif ch in "}]" and stack: stack.pop()
    s = t + ('"' if in_str else "")
    s = re.sub(r"[,:]\s*$", "", s)
    s += "".join("}" if o == "{" else "]" for o in reversed(stack))
    try: return json.loads(s)
    except Exception: return None
def parse_json(t):
    t = re.sub(r"```[a-zA-Z]*", "", t or "").replace("`","")
    try: return json.loads(t)
    except Exception: pass
    v = _repair_json(t)
    if v is not None: return v
    for opener, closer in (("{","}"),("[","]")):
        objs, depth, start = [], 0, None
        for i, ch in enumerate(t):
            if ch == opener:
                if depth == 0: start = i
                depth += 1
            elif ch == closer and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(t[start:i+1]); start = None
        for cand in reversed(objs):
            try: return json.loads(cand)
            except Exception: continue
    raise RuntimeError(f"модель не выдала JSON: {t[:200]!r}")
def rf(name, schema):
    return {"type":"json_schema","json_schema":{"name":name,"strict":True,"schema":schema}}
S_IDEAS = rf("ideas", {"type":"object","required":["ideas"],"properties":{"ideas":{"type":"array",
    "minItems":5,"maxItems":10,"items":{"type":"object","required":["short","full"],
    "properties":{"short":{"type":"string"},"full":{"type":"string"}}}}}})
S_QUESTIONS = rf("questions", {"type":"object","required":["questions"],"properties":{"questions":{"type":"array",
    "minItems":1,"maxItems":20,"items":{"type":"object","required":["q","options"],"properties":{
    "q":{"type":"string"},"options":{"type":"array","minItems":2,"maxItems":8,"items":{"type":"string"}}}}}}})
S_FIX = rf("fix", {"type":"object","required":["prompt"],"properties":{"prompt":{"type":"string"}}})
S_CRIT = rf("critique", {"type":"object","required":["ok","defects","comment","description"],"properties":{
    "ok":{"type":"boolean"},"defects":{"type":"array","items":{"type":"string"}},
    "comment":{"type":"string"},"description":{"type":"string"}}})
S_SEED = rf("seed", {"type":"object","required":["tags"],"properties":{"tags":{"type":"string"}}})

ASSETS = {"ckpts":[],"loras":[],"upscales":[],"detectors":[],"impact":False,
          "tipo":False,"samplers":[],"schedulers":[]}
SAMPLER_NAMES = ["euler","euler_cfg_pp","euler_ancestral","euler_ancestral_cfg_pp","heun","heunpp2",
    "dpm_2","dpm_2_ancestral","lms","dpm_fast","dpm_adaptive","dpmpp_2s_ancestral","dpmpp_sde","dpmpp_sde_gpu",
    "dpmpp_2m","dpmpp_2m_cfg_pp","dpmpp_2m_sde","dpmpp_2m_sde_gpu","dpmpp_3m_sde","dpmpp_3m_sde_gpu",
    "ddpm","lcm","ipndm","ipndm_v","deis","res_multistep","gradient_estimation","er_sde","ddim","uni_pc","uni_pc_bh2"]
SCHEDULER_NAMES = ["normal","karras","exponential","sgm_uniform","simple","ddim_uniform","beta","linear_quadratic","kl_optimal"]

MODEL_ID = None; HTTP = None; DEBUG_LOG = []
LM_TIMEOUT = 300
def _msg_text(msg):
    for k in ("content","reasoning_content","reasoning"):
        v = msg.get(k)
        if isinstance(v,str) and v.strip(): return v.strip()
    return ""
def _dump(tag,payload,resp):
    DEBUG_LOG.append({"attempt":tag,"request":payload,"response":resp}); del DEBUG_LOG[:-6]
    try: json.dump(DEBUG_LOG, open(LLM_DEBUG,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception: pass
async def post_json(s,url,payload):
    async with s.post(url,json=payload) as r: return await r.json()
async def ensure_model(s):
    global MODEL_ID
    if not MODEL_ID:
        models = (await (await s.get(LM_URL+"/v1/models")).json())["data"]
        MODEL_ID = next((m["id"] for m in models if "qwen" in m["id"].lower()), models[0]["id"])
        print("[llm] модель:", MODEL_ID)
async def llm(s, messages, temp, mt, want_json=False, schema=None, timeout=LM_TIMEOUT):
    await ensure_model(s)
    need_json = bool(want_json or schema)
    budget = max(int(mt), MIN_TOKENS) if need_json else int(mt)
    base = {"model":MODEL_ID,"messages":messages,"temperature":temp,"stream":False,
            "max_tokens":budget,"reasoning_effort":"none",
            "chat_template_kwargs":{"enable_thinking":False}}
    native = {"model":MODEL_ID,"temperature":temp,"stream":False,"store":False,
              "max_output_tokens":budget,"reasoning":"off",
              "system_prompt":"\n".join(m["content"] for m in messages if m["role"]=="system" and isinstance(m["content"],str)),
              "input":"\n\n".join(m["content"] for m in messages if m["role"]!="system" and isinstance(m["content"],str))}
    if schema: base["response_format"] = schema; native["response_format"] = schema
    variants = [("schema_off",LM_URL+"/v1/chat/completions",base),
                ("native_off",LM_URL+"/api/v1/chat",native)]
    if need_json:
        plain = dict(base); plain.pop("response_format",None); plain["max_tokens"]=max(budget,1500)
        variants.append(("plain_retry",LM_URL+"/v1/chat/completions",plain))
    err = ""
    for tag,url,payload in variants:
        try:
            j = await asyncio.wait_for(post_json(s,url,payload), timeout)
        except asyncio.CancelledError: raise
        except Exception as e:
            err = f"{tag}: {e}"; continue
        _dump(tag,payload,j)
        if j.get("error"): err = f"{tag}: {j['error']}"; continue
        if tag == "native_off":
            txt = "\n".join(o.get("content","") for o in j.get("output",[]) if o.get("type") in ("message","text")).strip()
        else:
            txt = _msg_text((j.get("choices") or [{}])[0].get("message",{}))
        if not txt: err = f"{tag}: пустой content"; continue
        if need_json:
            try: parse_json(txt)
            except Exception as e: err = f"{tag}: {e}"; continue
        return txt
    raise RuntimeError(f"LLM не дала пригодный ответ. {err} | см. llm_debug.json")
async def llm_stream(s, messages, temp, mt, on_delta=None, timeout=LM_TIMEOUT):
    await ensure_model(s)
    payload = {"model":MODEL_ID,"messages":messages,"temperature":temp,"stream":True,
               "max_tokens":int(mt),"reasoning_effort":"none",
               "chat_template_kwargs":{"enable_thinking":False}}
    buf = []
    async def _read():
        async with s.post(LM_URL+"/v1/chat/completions", json=payload) as r:
            if r.status != 200: raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")
            async for raw in r.content:
                raw = raw.strip()
                if not raw.startswith(b"data:"): continue
                data = raw[5:].strip()
                if data == b"[DONE]": break
                try: d = json.loads(data)
                except Exception: continue
                if d.get("error"): raise RuntimeError(str(d["error"])[:200])
                t = ((d.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if t:
                    buf.append(t)
                    if on_delta: await on_delta(t)
    await asyncio.wait_for(_read(), timeout)
    txt = "".join(buf).strip()
    if not txt: raise RuntimeError("пустой поток")
    return txt
async def vlm(s, text, images_b64, schema, temp=0.2, mt=1400, timeout=240):
    """Один VLM-вызов Qwen: текст + картинки, ответ по схеме."""
    await ensure_model(s)
    content = [{"type":"text","text":text}]
    for b64, fmt in images_b64:
        content.append({"type":"image_url","image_url":{"url":f"data:image/{fmt};base64,"+b64}})
    payload = {"model":MODEL_ID,"temperature":temp,"max_tokens":mt,"stream":False,
               "reasoning_effort":"none","chat_template_kwargs":{"enable_thinking":False},
               "messages":[{"role":"user","content":content}]}
    if schema: payload["response_format"] = schema
    j = await asyncio.wait_for(post_json(s, LM_URL+"/v1/chat/completions", payload), timeout)
    if j.get("error"): raise RuntimeError(str(j["error"])[:200])
    txt = _msg_text((j.get("choices") or [{}])[0].get("message",{}))
    if not txt: raise RuntimeError("пустой ответ VLM")
    return txt

# ============================================================================
# МУЛЬТИЧАТ
# ============================================================================
CHATS = {}; PID_MAP = {}; WS_CLIENT = "heretic-"+uuid.uuid4().hex[:8]
CHAT_MODES = ("scene","source","prompt","ask")
def make_chat(title=None):
    cid = uuid.uuid4().hex[:10]
    ch = {"id":cid,"title":title or ("Чат "+str(len(CHATS)+1)),"created":time.time(),
          "messages":[],"items":[],"turns":[],
          "last":{"theme":"","shots":[],"ts":0},
          "mode":"scene","stage":"idle","draft":None,"ask_theme":"",
          "busy":False,"cancel":False,"progress":None,"epoch":0,"task":None}
    CHATS[cid] = ch; return ch
def save_chats():
    try:
        data = {}
        for cid,ch in CHATS.items():
            data[cid] = {"id":cid,"title":ch["title"],"created":ch["created"],"mode":ch["mode"],
                         "messages":ch["messages"][-200:],
                         "items":[{k:v for k,v in it.items() if k!="progress"} for it in ch["items"][-50:]],
                         "turns":ch["turns"][-80:],"last":ch["last"]}
        json.dump(data, open(CHATS_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e: print("[chats] не сохранилось:", e)
def load_chats():
    if os.path.isfile(CHATS_FILE):
        try:
            d = json.load(open(CHATS_FILE, encoding="utf-8"))
            for cid,c in d.items():
                ch = make_chat(c.get("title") or "Чат"); ch["id"]=cid; CHATS[cid]=ch
                ch["created"]=c.get("created",time.time())
                ch["mode"]=c.get("mode") if c.get("mode") in CHAT_MODES else "scene"
                ch["messages"]=c.get("messages",[])[-200:]
                ch["items"]=c.get("items",[])[-50:]
                ch["turns"]=c.get("turns",[])[-80:]
                ch["last"]=c.get("last") or {"theme":"","shots":[],"ts":0}
        except Exception as e: print("[chats] не загрузились:", e)
    for c in CHATS.values():
        for it in c["items"]:
            if it.get("status") not in ("готово","брак"): it["status"],it["ok"]="брак",False
    if not CHATS: make_chat("Чат 1")
    save_chats()
def launch(ch,coro):
    t = asyncio.create_task(coro); ch["task"]=t
    def _done(task):
        if ch.get("task") is t: ch["task"]=None
    t.add_done_callback(_done); return t
def say(ch,role,text,images=None):
    m = {"role":role,"text":text}
    if images: m["images"]=images
    ch["messages"].append(m); del ch["messages"][:-200]; save_chats(); return m
def chat_add(ch,role,content):
    c = str(content or "").strip()
    if not c: return
    ch["turns"].append({"role":role,"content":c}); del ch["turns"][:-80]; save_chats()
def chat_msgs(ch,limit=None):
    out = []
    for t in ch["turns"][-(limit or HIST_TURNS):]:
        c = t["content"]
        out.append({"role":t["role"],"content":c if len(c)<=CLIP_LEN else c[:CLIP_LEN]+" …"})
    return out
def last_shot(ch): return ch["last"]["shots"][-1] if ch["last"]["shots"] else None
def clear_last(ch):
    ch["last"]={"theme":"","shots":[],"ts":0}; ch["draft"]=None; ch["stage"]="idle"; save_chats()
load_chats()

FRAME_NUM = {"первый":1,"первого":1,"второй":2,"второго":2,"третий":3,"третьего":3,
             "четвёртый":4,"четвертый":4,"пятый":5,"шестой":6}
def pick_base(ch,text):
    shots = ch["last"]["shots"]
    if not shots: return {}
    t = (text or "").lower(); n = None
    m = re.search(r"кадр\w*\s*(?:№|#)?\s*(\d+)", t)
    if m: n = int(m.group(1))
    else:
        for w,k in FRAME_NUM.items():
            if re.search(r"\b"+w+r"\b",t): n=k; break
    if n:
        for sh in shots:
            if sh.get("idx")==n: return sh
    return shots[-1]
def _lat(s): return sum(c.isascii() and c.isalpha() for c in s)
def _rus(s): sum_ = sum("\u0400"<=c<="\u04ff" for c in s); return sum_
def looks_like_prompt(t):
    t = str(t or "")
    return t.count(",")>=6 and _lat(t)>_rus(t)*2 and _lat(t)>40
def extract_prompt(text):
    t = str(text or "").strip()
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    tags = [l for l in lines if l.count(",")>=3 and _lat(l)>_rus(l)]
    if tags: return ", ".join(tags).strip(" ,")
    if looks_like_prompt(t): return t.strip(" ,")
    return ""
APPROVE_RE = re.compile(r"^(да|ок|окей|ok|okay|го|давай|генерируй|рисуй|поехали|запускай|принято|"
                        r"отлично|супер|класс|годится|подходит|делай|good|yes|y)\b[\s!.,]*$", re.I)
EDIT_RE = re.compile(r"\b(убери|удали|добавь|замени|сделай|пусть|без|больше|меньше|крупнее|мельче|"
                     r"ближе|дальше|ярче|темнее|ночь|день|закат|дождь|снег|сидит|стоит|одень|раздень|"
                     r"поменяй|измени|исправь|перекрась|короче|длиннее)\b", re.I)
NEW_RE = re.compile(r"^(новая тема|новый сюжет|другая тема|заново)[:\s-]*", re.I)
SCENE_LABELS = ("Место:","Персонаж:","Внешность:","Одежда:","Поза","Настроение:","Фон:",
                "Атмосфера:","Время и свет:","Камера:","Стиль:","Откровенность:")
def is_full_scene(t):
    t = str(t or "")
    return len(t) >= 350 and sum(1 for l in SCENE_LABELS if l in t) >= 4
def clean_tags(p):
    """Лёгкая гигиена тегов: дубли, мета-мусор, потолок весов. Без словарей и счётчиков."""
    p = re.sub(r"```[a-zA-Z]*","",str(p or "")).replace("`","")
    p = re.sub(r"<\|[^|>]*\>", " ", p)
    p = " ".join(p.splitlines())
    tags, seen, weights, out = [], set(), 0, []
    for raw in p.split(","):
        raw = raw.strip().strip(".").lower()
        if not raw: continue
        m = re.match(r"^\((.+?):([0-9.]+)\)$", raw)
        key = (m.group(1) if m else raw).strip()
        if key in META_TAGS or key in seen: continue
        if m:
            w = float(m.group(2))
            if weights >= 3 or w > 1.3: raw = key
            else: weights += 1
        seen.add(key); out.append(raw)
    return ", ".join(out[:200])

# ============================================================================
# ПАМЯТЬ
# ============================================================================
def load_mem():
    if os.path.isfile(MEM_FILE):
        try: return json.load(open(MEM_FILE, encoding="utf-8"))
        except Exception: pass
    return {"summary":"","entries":[]}
def save_mem(m): json.dump(m, open(MEM_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
def _memctx(mem):
    lines = [json.dumps({k:e.get(k) for k in ("theme","ok","defects","feedback")}, ensure_ascii=False)
             for e in mem.get("entries",[])[-3:]]
    return f"[САММАРИ]\n{mem.get('summary') or 'пусто'}\n[ПОСЛЕДНИЕ]\n" + "\n".join(lines)

# ============================================================================
# ИДЕИ (Qwen, две версии: short/full)
# ============================================================================
IDEAS_CACHE = None   # {"real":[{short,full}], "fantasy":[...]}
IDEAS_VER = 0
def load_ideas():
    global IDEAS_CACHE
    if IDEAS_CACHE is not None: return IDEAS_CACHE
    if os.path.isfile(IDEAS_FILE):
        try:
            d = json.load(open(IDEAS_FILE, encoding="utf-8"))
            if isinstance(d, dict) and any(isinstance(d.get(k),list) and d[k] for k in IDEA_MODES):
                IDEAS_CACHE = {k:[x for x in (d.get(k) or []) if isinstance(x,dict) and x.get("short") and x.get("full")]
                               for k in IDEA_MODES}
                return IDEAS_CACHE
        except Exception: pass
    return None
def save_ideas(d):
    global IDEAS_VER
    try:
        json.dump(d, open(IDEAS_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
        IDEAS_VER += 1
    except Exception as e: print("[ideas] не сохранилось:", e)

def _words(s): return set(re.findall(r"[а-яёa-z]{3,}", str(s).lower()))
def idea_ok(it, taken):
    short, full = str(it.get("short") or ""), str(it.get("full") or "")
    if not (8 <= len(short.split()) <= 45): return False
    if len(full.split()) < 90: return False
    if sum(1 for l in SCENE_LABELS if l in full) < 5: return False
    if re.search(r"\b(школ|подрост|малолет|лоли|ребёнк|ребенк)\w*", full.lower()): return False
    cur = _words(short)
    for prev in taken:
        old = _words(prev.get("short") if isinstance(prev,dict) else prev)
        if cur and old and len(cur & old)/float(len(cur | old)) > 0.6: return False
    return True
async def generate_ideas(mode):
    mode = mode if mode in IDEA_MODES else "real"
    data = load_ideas() or {k:[] for k in IDEA_MODES}
    seen = data.get(mode) or []
    out = []
    for attempt in range(2):
        hist = "\n".join("- "+x["short"] for x in (seen+out)[-8:]) or "—"
        msgs = [{"role":"system","content":IDEA_SYS.format(mode_line=IDEA_MODES[mode], n=8)},
                {"role":"user","content":"Уже было (не повторяй):\n"+hist}]
        txt = await llm(HTTP, msgs, 0.85 if attempt else 0.75, 4096, True, schema=S_IDEAS, timeout=300)
        raw = parse_json(txt).get("ideas") or []
        for it in raw:
            if isinstance(it,dict) and idea_ok(it, seen+out):
                out.append({"short":" ".join(str(it["short"]).split()),
                            "full":str(it["full"]).strip()})
        if len(out) >= 5: break
    if not out: raise RuntimeError("модель не дала пригодных идей")
    out = out[:8]; random.shuffle(out)
    data[mode] = out; IDEAS_CACHE = data; save_ideas(data)
    print(f"[ideas] {mode}: {len(out)} шт")
    return out

# ============================================================================
# COMFYUI: ассеты, загрузка, архив
# ============================================================================
def _combo_list(oi,node,field):
    try: spec = oi[node]["input"]["required"][field]
    except Exception: return []
    if spec and isinstance(spec[0],list): return [str(x) for x in spec[0]]
    if len(spec)>1 and isinstance(spec[1],dict):
        out = []
        for o in spec[1].get("options") or []:
            if isinstance(o,str): out.append(o)
            elif isinstance(o,dict):
                v = o.get("content") or o.get("name") or o.get("value")
                if v: out.append(str(v))
        return out
    return []
def pick_detector(models,key):
    hints = DET_HINTS.get(key) or ()
    for m in models:
        low = str(m).lower()
        if low.startswith("bbox/") and any(h in low for h in hints): return m
    for m in models:
        if any(h in str(m).lower() for h in hints): return m
    return ""
def auto_pick_detectors():
    models = ASSETS.get("detectors") or []
    for d in CONFIG.get("detailers") or []:
        if d.get("key")=="custom" or (d.get("model") and d["model"] in models): continue
        got = pick_detector(models,d.get("key"))
        if got: d["model"]=got
def active_detailers(cfg):
    if not (cfg.get("detail_on",True) and ASSETS.get("impact")): return []
    have = ASSETS.get("detectors") or []
    return [d for d in (cfg.get("detailers") or []) if d.get("on") and d.get("model") and d["model"] in have]

# ============================================================================
# TIPO: автозапуск llama-server вместе с мостом (GPU = CMP 90HX)
# ============================================================================
TIPO_PROC = None
TIPO_LOG_F = None

def _tipo_port():
    m = re.search(r":(\d+)", CONFIG.get("tag_url","") or "")
    return int(m.group(1)) if m else 8080

async def tipo_alive(s):
    try:
        async with s.get(CONFIG["tag_url"]+"/health",
                         timeout=aiohttp.ClientTimeout(total=2)) as r:
            await r.read()
            return r.status == 200
    except Exception:
        return False

def start_tipo():
    global TIPO_PROC, TIPO_LOG_F
    if not CONFIG.get("tipo_autostart", True):
        print("[tipo] автозапуск выключен (tipo_autostart=False)"); return
    exe = CONFIG.get("tipo_exe") or ""
    model = CONFIG.get("tipo_model_path") or ""
    if not os.path.isfile(exe):
        print("[tipo] не найден llama-server:", exe or "(tipo_exe пуст)"); return
    if not os.path.isfile(model):
        print("[tipo] не найден gguf:", model or "(tipo_model_path пуст)"); return
    dev = str(CONFIG.get("tipo_device","0")).strip()
    env = dict(os.environ)
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"   # индексы GPU как в nvidia-smi
    cmd = [exe, "-m", model, "--host","127.0.0.1", "--port",str(_tipo_port()),
           "-c","2048","-t","6"]
    if dev.lower() in ("cpu","-1",""):
        env["CUDA_VISIBLE_DEVICES"] = ""      # CPU-режим
    else:
        env["CUDA_VISIBLE_DEVICES"] = dev     # только выбранная GPU (90HX)
        cmd += ["-ngl","99"]
    TIPO_LOG_F = open(os.path.join(BASE_DIR,"tipo_server.log"),"ab")
    kw = {"stdout":TIPO_LOG_F, "stderr":TIPO_LOG_F, "env":env}
    if os.name == "nt": kw["creationflags"] = 0x08000000   # без окна консоли
    try:
        TIPO_PROC = subprocess.Popen(cmd, **kw)
        print(f"[tipo] llama-server pid {TIPO_PROC.pid} · GPU {dev or 'CPU'} · порт {_tipo_port()}")
    except Exception as e:
        print("[tipo] не запустился:", e)

async def ensure_tipo():
    """Старт моста: сервер уже жив — используем; нет — запускаем и ждём до 30 с."""
    if await tipo_alive(HTTP):
        ASSETS["tipo"] = True
        print("[tipo] уже запущен, использую существующий")
        return
    start_tipo()
    if TIPO_PROC is None:
        ASSETS["tipo"] = False; return
    for _ in range(60):
        await asyncio.sleep(0.5)
        if TIPO_PROC.poll() is not None:
            print("[tipo] процесс упал, код", TIPO_PROC.returncode, "— смотри tipo_server.log")
            break
        if await tipo_alive(HTTP):
            ASSETS["tipo"] = True
            print("[tipo] готов")
            return
    ASSETS["tipo"] = False
    print("[tipo] не ответил — теги достроит Qwen (если включён fallback)")

def stop_tipo():
    global TIPO_PROC, TIPO_LOG_F
    if TIPO_PROC and TIPO_PROC.poll() is None:
        try:
            TIPO_PROC.terminate(); TIPO_PROC.wait(timeout=5)
        except Exception:
            try: TIPO_PROC.kill()
            except Exception: pass
        print("[tipo] llama-server остановлен")
    TIPO_PROC = None
    if TIPO_LOG_F:
        try: TIPO_LOG_F.close()
        except Exception: pass
        TIPO_LOG_F = None

async def fetch_assets(s=None):
    own = s is None and HTTP is None
    if s is None: s = HTTP or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
    try:
        if not ASSETS["ckpts"]:
            oi = await (await s.get(COMFY_URL+"/object_info/CheckpointLoaderSimple")).json()
            ASSETS["ckpts"] = _combo_list(oi,"CheckpointLoaderSimple","ckpt_name")
        if not ASSETS["loras"]:
            oi = await (await s.get(COMFY_URL+"/object_info/LoraLoader")).json()
            ASSETS["loras"] = _combo_list(oi,"LoraLoader","lora_name")
        if not ASSETS["upscales"]:
            oi = await (await s.get(COMFY_URL+"/object_info/UpscaleModelLoader")).json()
            ASSETS["upscales"] = _combo_list(oi,"UpscaleModelLoader","model_name")
        if not ASSETS["samplers"]:
            oi = await (await s.get(COMFY_URL+"/object_info/KSampler")).json()
            ASSETS["samplers"] = _combo_list(oi,"KSampler","sampler_name") or SAMPLER_NAMES
            ASSETS["schedulers"] = _combo_list(oi,"KSampler","scheduler") or SCHEDULER_NAMES
        if not ASSETS["detectors"]:
            oi = await (await s.get(COMFY_URL+"/object_info/UltralyticsDetectorProvider")).json()
            ASSETS["detectors"] = _combo_list(oi,"UltralyticsDetectorProvider","model_name")
            if ASSETS["detectors"]:
                od = await (await s.get(COMFY_URL+"/object_info/FaceDetailer")).json()
                ASSETS["impact"] = "FaceDetailer" in od
            auto_pick_detectors()
        ASSETS["tipo"] = await tipo_alive(s)
    except Exception as e:
        print("[assets] ошибка:", e)
    finally:
        if own: await s.close()
    if ASSETS["ckpts"] and CONFIG["ckpt"] not in ASSETS["ckpts"]:
        CONFIG["ckpt"] = ASSETS["ckpts"][0]; save_config(CONFIG)
def ref_path(name): return os.path.join(UPLOAD_DIR, os.path.basename(str(name or "")))
def pick_canvas(w,h):
    ar = w/max(1,h)
    return min(SDXL_BUCKETS, key=lambda b: abs(b[0]/b[1]-ar))
def _shrink(path,max_side=1280):
    try:
        im = Image.open(path); im.thumbnail((max_side,max_side), Image.LANCZOS)
        if im.mode in ("RGBA","LA","P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB",im.size,(255,255,255)); flat.paste(im,mask=im.split()[-1]); im = flat
        else: im = im.convert("RGB")
        buf = io.BytesIO(); im.save(buf,format="JPEG",quality=92)
        return buf.getvalue(),"jpeg"
    except Exception:
        return open(path,"rb").read(),"png"
async def comfy_upload(s,path,subfolder="heretic_src"):
    with open(path,"rb") as f: blob = f.read()
    fd = aiohttp.FormData()
    fd.add_field("image",io.BytesIO(blob),filename=os.path.basename(path),content_type="image/png")
    fd.add_field("subfolder",subfolder); fd.add_field("type","input"); fd.add_field("overwrite","true")
    async with s.post(COMFY_URL+"/upload/image",data=fd) as r:
        j = await r.json()
        name = j.get("name") or os.path.basename(path); sub = j.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name
def _pick_img(outputs,node_id,prefix):
    n = outputs.get(node_id) or {}
    for im in (n.get("images") or []):
        fn = im.get("filename")
        if fn and im.get("type")!="temp": return os.path.basename(fn)
    for v in outputs.values():
        for im in (v.get("images") or []):
            fn = im.get("filename") or ""
            if im.get("type")!="temp" and os.path.basename(fn).startswith(prefix):
                return os.path.basename(fn)
    return None
def _pick_any_img(outputs,node_id,prefix):
    n = outputs.get(node_id) or {}
    for im in (n.get("images") or []):
        if im.get("filename"): return im
    for v in outputs.values():
        for im in (v.get("images") or []):
            if os.path.basename(im.get("filename") or "").startswith(prefix): return im
    return None
async def comfy_view_save(s,im,name):
    params = {"filename":im["filename"],"type":im.get("type") or "output","subfolder":im.get("subfolder") or ""}
    async with s.get(COMFY_URL+"/view",params=params) as r:
        if r.status!=200: raise RuntimeError(f"/view HTTP {r.status}: {im}")
        blob = await r.read()
    os.makedirs(CONFIG["output_dir"],exist_ok=True)
    dst = os.path.join(CONFIG["output_dir"],os.path.basename(name))
    with open(dst,"wb") as f: f.write(blob)
    return os.path.basename(dst)
def _disk_img(prefix):
    files = [f for f in glob.glob(os.path.join(CONFIG["output_dir"],prefix+"*"))
             if f.lower().endswith((".png","jpg",".jpeg",".webp"))]
    return os.path.basename(max(files,key=os.path.getmtime)) if files else None
def _comfy_err(st):
    for m in st.get("messages",[]):
        if m and m[0] in ("execution_error","execution_interrupted"):
            d = m[1] if len(m)>1 else {}
            return f"{d.get('node_type','?')} [{d.get('node_id','?')}]: {d.get('exception_message',m[0])}"
    return "причина не указана"
def save_dir():
    d = CONFIG.get("save_dir") or os.path.join(BASE_DIR,"generated")
    try: os.makedirs(d,exist_ok=True)
    except Exception: d = CONFIG["output_dir"]
    return d
def img_path(name):
    n = os.path.basename(name or "")
    for root in (save_dir(),CONFIG["output_dir"],UPLOAD_DIR):
        p = os.path.join(root,n)
        if os.path.isfile(p): return p
    return os.path.join(CONFIG["output_dir"],n)
def safe_name(t,n=30):
    t = re.sub(r"[^0-9A-Za-zА-Яа-яЁё _-]+","",str(t or "")).strip().replace(" ","_")
    return t[:n] or "series"
def archive(fin,base,theme,seed):
    d = save_dir(); stamp = time.strftime("%Y%m%d-%H%M%S"); slug = safe_name(theme)
    res = {"final":fin,"base":base}
    for kind,name in (("final",fin),("base",base)):
        if not name: continue
        src = os.path.join(CONFIG["output_dir"],os.path.basename(name))
        if not os.path.isfile(src): continue
        ext = os.path.splitext(src)[1] or ".png"
        suf = "" if kind=="final" else "_base"
        dst = os.path.join(d,f"{stamp}_{slug}_{seed}{suf}{ext}"); k=1
        while os.path.exists(dst):
            dst = os.path.join(d,f"{stamp}_{slug}_{seed}{suf}_{k}{ext}"); k+=1
        try: shutil.copy2(src,dst); res[kind]=os.path.basename(dst)
        except Exception as e: print("[archive] не скопировано:",e)
    return res["final"],res["base"]
def sidecar(name,theme,scene,prompt,seed,verdict,fb):
    try:
        p = os.path.join(save_dir(),os.path.splitext(os.path.basename(name))[0]+".txt")
        with open(p,"w",encoding="utf-8") as f:
            f.write(f"тема: {theme}\nseed: {seed}\nвердикт: {verdict}\n\n"
                    f"сценарий:\n{scene}\n\nоценка модели:\n{fb}\n\nтеги:\n{prompt}\n")
    except Exception as e: print("[archive] txt не записан:",e)

# ============================================================================
# ТЕГГЕР: Qwen3.5 (90HX) seed-теги -> TIPO достройка (опционально) -> промпт
# ============================================================================
TIPO_HEAD_RE = re.compile(r"^(aspect[\s_]*ratio|artist|copyrights?|rating|target|short|long|quality|meta|characters|general)\s*:", re.I)
def parse_tipo(txt):
    raw = str(txt or "")
    try:
        with open(os.path.join(BASE_DIR,"tipo_debug.log"),"a",encoding="utf-8") as f:
            f.write("\n"+"="*60+"\n"+raw)
    except Exception: pass
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    for ln in reversed(lines):
        if TIPO_HEAD_RE.match(ln): continue
        if ln.count(",") >= 5 and ". " not in ln:
            return ln
    return ""

async def make_tags(ch, s, scene, precise=False):
    # шаг 1: сценарий -> ядро тегов (Qwen3.5)
    obj = parse_json(await llm(s, [{"role":"system","content":SEED_SYS},
                                   {"role":"user","content":scene}],
                               0.2 if precise else 0.3, 900, True, schema=S_SEED, timeout=180))
    seed_tags = clean_tags(obj.get("tags",""))
    if not seed_tags: raise RuntimeError("Qwen не дала ядро тегов")
    if ch["cancel"]: raise RuntimeError("отменено")
    # шаг 2: TIPO — креативный апсемплер, может уводить сюжет, поэтому seed ВСЕГДА первый.
    # precise (режим 2) и tipo_on=False — достраивает сам Qwen, без фантазий.
    tipo_tags = ""
    if CONFIG.get("tipo_on",True) and not precise:
        payload = {"prompt": TIPO_TEMPLATE.format(seed=seed_tags),
                   "n_predict": int(CONFIG.get("tipo_tokens",600)),
                   "temperature": float(CONFIG.get("tag_temp",0.4)),
                   "top_p": 0.9, "cache_prompt": True, "stream": False}
        try:
            j = await asyncio.wait_for(post_json(s, CONFIG["tag_url"]+"/completion", payload), 120)
            tipo_tags = parse_tipo(j.get("content") or "")
        except asyncio.CancelledError: raise
        except Exception as e:
            say(ch,"b",f"TIPO не ответил ({e}) — достраиваю теги Qwen'ом.")
        if tipo_tags:
            tags = clean_tags(seed_tags + ", " + tipo_tags)
            if tags: return tags
    txt = await llm(s, [{"role":"system","content":FALLBACK_SYS},
                        {"role":"user","content":seed_tags}], 0.4, 1200, timeout=180)
    tags = clean_tags(seed_tags + ", " + txt)
    if not tags: raise RuntimeError("теггер вернул пусто")
    return tags

# ============================================================================
# ГРАФ КАРТИНКИ
# ============================================================================
CLIP_MODE = 2  # 2 = clip skip после LoRA (как раньше), 1 = skip до LoRA и кодировать базовым клипом, 0 = без clip skip
def _clip_broken(e):
    t = str(e)
    return "NoneType" in t and ("clone" in t or "tokenize" in t or "cond_stage" in t)
def canvas_size():
    cfg = CONFIG
    if cfg.get("resolution")=="custom":
        wh = cfg.get("custom_wh") or [1408,704]
        w,h = int(wh[0]),int(wh[1])
        return max(256,w//8*8), max(256,h//8*8)
    return RESOLUTIONS.get(cfg.get("resolution")) or (1408,704)
def next_seed(keep=None):
    """seed как в ComfyUI: fixed / increment / decrement / randomize. Использованный seed запоминается."""
    if keep is not None: return max(1,int(keep))
    mode = str(CONFIG.get("seed_mode","randomize")); cur = int(CONFIG.get("seed") or 0)
    if mode=="fixed" and cur>0: s = cur
    elif mode=="increment" and cur>0: s = cur+1
    elif mode=="decrement" and cur>0: s = max(1,cur-1)
    else: s = random.randint(1,2**32-1)
    CONFIG["seed"] = s; save_config(CONFIG)
    return s
def build_graph(content,seed,pref_final="heretic",pref_base="heretic_base",
                init=None,denoise=None,size=None):
    cfg = CONFIG
    w,h = size or canvas_size()
    fw,fh = cfg.get("fw_fh") or [2000,1000]
    if init and cfg.get("src_auto_size",True):
        k = max(fw,fh)/float(max(w,h))
        fw,fh = max(512,int(w*k)//8*8), max(512,int(h*k)//8*8)
    loras = [l for l in cfg["loras"] if l.get("on") and l.get("name")]
    g = {"ckpt":{"class_type":"CheckpointLoaderSimple","inputs":{"ckpt_name":cfg["ckpt"]}}}
    m,c,v = ["ckpt",0],["ckpt",1],["ckpt",2]
    cs = int(cfg.get("clip_skip",2) or 0)
    c_base = None
    if CLIP_MODE==1:
        if cs>0:
            g["clipskip"] = {"class_type":"CLIPSetLastLayer","inputs":{"clip":c,"stop_at_clip_layer":-cs}}
            c = ["clipskip",0]
        c_base = c
    for i,l in enumerate(loras):
        if l["name"] not in ASSETS["loras"]: continue
        nid = f"lora{i}"; sm = float(l.get("strength",0.8))
        g[nid] = {"class_type":"LoraLoader","inputs":{"model":m,"clip":c,"lora_name":l["name"],
                  "strength_model":sm,"strength_clip":float(l.get("strength_clip",sm))}}
        m,c = [nid,0],[nid,1]
    if cs>0 and CLIP_MODE==2:
        g["clipskip"] = {"class_type":"CLIPSetLastLayer","inputs":{"clip":c,"stop_at_clip_layer":-cs}}
        c = ["clipskip",0]
    if c_base is not None: c = c_base   # режим 1: кодируем базовым клипом, клип LoRA игнорируем
    pos = content.strip().rstrip(",")
    if cfg.get("quality_on",True): pos = QUALITY+", "+pos
    art = str(cfg.get("artist") or "").strip()
    if art: pos = art+", "+pos
    g["pos"] = {"class_type":"CLIPTextEncode","inputs":{"clip":c,"text":pos}}
    g["neg"] = {"class_type":"CLIPTextEncode","inputs":{"clip":c,"text":NEGATIVE}}
    if init:
        g["ref_img"] = {"class_type":"LoadImage","inputs":{"image":init}}
        g["ref_fit"] = {"class_type":"ImageScale","inputs":{"image":["ref_img",0],
                        "upscale_method":"lanczos","width":w,"height":h,"crop":"disabled"}}
        g["latent"] = {"class_type":"VAEEncode","inputs":{"pixels":["ref_fit",0],"vae":v}}
        d1 = max(0.05,min(1.0,float(denoise if denoise is not None else cfg.get("src_denoise",0.55))))
    else:
        g["latent"] = {"class_type":"EmptyLatentImage","inputs":{"width":w,"height":h,"batch_size":1}}
        d1 = max(0.05,min(1.0,float(cfg.get("denoise",1.0))))
    g["ks1"] = {"class_type":"KSampler","inputs":{"model":m,"positive":["pos",0],"negative":["neg",0],
                "latent_image":["latent",0],"seed":seed,"steps":int(cfg["steps"]),"cfg":float(cfg["cfg"]),
                "sampler_name":cfg.get("sampler","dpmpp_2m"),"scheduler":cfg.get("scheduler","karras"),"denoise":d1}}
    g["dec1"] = {"class_type":"VAEDecode","inputs":{"samples":["ks1",0],"vae":v}}
    g["save_base"] = {"class_type":"SaveImage","inputs":{"images":["dec1",0],"filename_prefix":pref_base}}
    up = cfg.get("upscaler"); has_up = bool(up and up in ASSETS["upscales"])
    if has_up:
        g["up_model"] = {"class_type":"UpscaleModelLoader","inputs":{"model_name":up}}
    src = ["dec1",0]
    if cfg.get("hires_on",True):
        hs = float(cfg.get("hires_scale",1.5)); hw,hh = int(w*hs),int(h*hs)
        cap = int(cfg.get("hires_max_px",2600000))
        if hw*hh>cap:
            k = (cap/float(hw*hh))**0.5; hw,hh = int(hw*k),int(hh*k)
        hw,hh = max(512,hw//8*8),max(512,hh//8*8)
        if has_up:
            g["up1"] = {"class_type":"ImageUpscaleWithModel","inputs":{"upscale_model":["up_model",0],"image":src}}
            src = ["up1",0]
        g["sc1"] = {"class_type":"ImageScale","inputs":{"image":src,"upscale_method":"lanczos",
                    "width":hw,"height":hh,"crop":"disabled"}}
        g["enc2"] = {"class_type":"VAEEncode","inputs":{"pixels":["sc1",0],"vae":v}}
        g["ks2"] = {"class_type":"KSampler","inputs":{"model":m,"positive":["pos",0],"negative":["neg",0],
                    "latent_image":["enc2",0],"seed":seed,"steps":int(cfg.get("hires_steps",20)),
                    "cfg":float(cfg.get("hires_cfg",5.0)),"sampler_name":cfg.get("hires_sampler","dpmpp_2m_sde"),
                    "scheduler":cfg.get("hires_scheduler","karras"),"denoise":float(cfg.get("hires_denoise",0.4))}}
        g["dec2"] = {"class_type":"VAEDecode","inputs":{"samples":["ks2",0],"vae":v}}
        src = ["dec2",0]
    for i,d in enumerate(active_detailers(cfg)):
        nid,fid = f"det{i}",f"detail{i}"
        g[nid] = {"class_type":"UltralyticsDetectorProvider","inputs":{"model_name":d["model"]}}
        dseed = seed if cfg.get("detail_seed_mode","same")=="same" else seed+100+i
        g[fid] = {"class_type":"FaceDetailer","inputs":{"image":src,"model":m,"clip":c,"vae":v,
                  "positive":["pos",0],"negative":["neg",0],"bbox_detector":[nid,0],
                  "guide_size":int(d.get("guide",512)),"guide_size_for":True,
                  "max_size":int(cfg.get("detail_max_size",1024)),"seed":int(dseed),
                  "steps":int(d.get("steps",20)),"cfg":float(d.get("cfg",5.0)),
                  "sampler_name":cfg.get("sampler","dpmpp_2m"),"scheduler":cfg.get("scheduler","karras"),
                  "denoise":max(0.05,min(1.0,float(d.get("denoise",0.35)))),
                  "feather":int(cfg.get("detail_feather",8)),"noise_mask":True,"force_inpaint":True,"wildcard":""}}
        src = [fid,0]
    if has_up and (fw>w or fh>h):
        g["up2"] = {"class_type":"ImageUpscaleWithModel","inputs":{"upscale_model":["up_model",0],"image":src}}
        g["sc2"] = {"class_type":"ImageScale","inputs":{"image":["up2",0],"upscale_method":"lanczos",
                    "width":fw,"height":fh,"crop":"disabled"}}
        src = ["sc2",0]
    g["save_final"] = {"class_type":"SaveImage","inputs":{"images":src,"filename_prefix":pref_final}}
    return g

# ============================================================================
# ПРОГРЕСС (WS) + ГЕНЕРАЦИЯ
# ============================================================================
def _progress(ch,info,node,value,mx):
    if ch["epoch"]!=info["epoch"]: return
    item = info["item"]
    if item is None: return
    s1,s2 = info["steps1"],info["steps2"]; total = max(1,s1+s2)
    nd = str(node or "")
    if nd=="ks2": stage,done = "hires",s1+value
    elif nd.startswith("detail"): stage,done = "детализация",s1+s2+value
    else: stage,done = "база",value
    pr = {"active":True,"idx":item.get("idx"),"node":node,"stage":stage,"value":int(value),"max":int(mx),
          "pct":int(round(value*100.0/max(1,mx))),"overall":int(round(done*100.0/total))}
    ch["progress"]=pr; item["progress"]=pr
async def ws_loop():
    url = COMFY_URL.replace("https://","wss://").replace("http://","ws://")+"/ws?clientId="+WS_CLIENT
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(url,heartbeat=20,max_msg_size=0) as ws:
                    print("[ws] подключен к ComfyUI")
                    async for m in ws:
                        if m.type!=aiohttp.WSMsgType.TEXT: continue
                        try: j = json.loads(m.data)
                        except Exception: continue
                        t,d = j.get("type"),(j.get("data") or {})
                        pid = d.get("prompt_id")
                        if not pid and len(PID_MAP)==1: pid = next(iter(PID_MAP))
                        info = PID_MAP.get(pid)
                        if not info: continue
                        ch = info["chat"]
                        if t=="progress":
                            _progress(ch,info,d.get("node") or info.get("node") or "ks1",d.get("value",0),d.get("max",1))
                        elif t=="executing":
                            node = d.get("node"); info["node"]=node
                            if node=="ks1": _progress(ch,info,"ks1",0,info["steps1"])
                            elif node=="ks2": _progress(ch,info,"ks2",0,info["steps2"])
                            elif node and ch["epoch"]==info["epoch"] and ch.get("progress"):
                                pr = dict(ch["progress"]); nd = str(node)
                                pr["stage"] = ("детализация" if nd.startswith("detail")
                                               else "апскейл" if nd in ("up2","sc2","up1","sc1","ref_fit")
                                               else "подготовка")
                                pr["node"]=node; ch["progress"]=pr
        except asyncio.CancelledError: raise
        except Exception as e:
            print("[ws] обрыв:",e); await asyncio.sleep(3)
async def comfy_generate(ch,s,content,seed,item,init=None,denoise=None,size=None):
    tag = str(random.randint(100000000,999999999))
    pref_final,pref_base = "heretic_"+tag,"heretic_base_"+tag
    graph = build_graph(content,seed,pref_final,pref_base,init=init,denoise=denoise,size=size)
    cfg = CONFIG
    steps1 = int(cfg["steps"]); steps2 = int(cfg.get("hires_steps",20)) if cfg.get("hires_on",True) else 0
    async with s.post(COMFY_URL+"/prompt",json={"prompt":graph,"client_id":WS_CLIENT}) as r:
        j = await r.json()
    if "prompt_id" not in j: raise RuntimeError(f"ComfyUI отверг граф: {str(j)[:600]}")
    pid = j["prompt_id"]
    info = {"chat":ch,"item":item,"steps1":steps1,"steps2":steps2,"epoch":ch["epoch"],"node":None}
    PID_MAP[pid] = info
    pr = {"active":True,"idx":item.get("idx"),"node":None,"stage":"подготовка","value":0,
          "max":steps1,"pct":0,"overall":0}
    ch["progress"]=pr; item["progress"]=pr
    try:
        empty = 0
        for _ in range(1800):
            if ch["cancel"]: raise RuntimeError("отменено")
            await asyncio.sleep(1)
            h = await (await s.get(f"{COMFY_URL}/history/{pid}")).json()
            if pid not in h: continue
            entry = h[pid]; st = entry.get("status",{}) or {}
            if st.get("status_str")=="error": raise RuntimeError("ComfyUI: "+_comfy_err(st))
            o = entry.get("outputs",{}) or {}
            fin = _pick_img(o,"save_final",pref_final) or _disk_img(pref_final)
            base = _pick_img(o,"save_base",pref_base) or _disk_img(pref_base)
            if not fin:
                im = _pick_any_img(o,"save_final",pref_final) or _pick_any_img(o,"save_base",pref_base)
                if im: fin = await comfy_view_save(s,im,pref_final+".png")
                elif base: fin = base
            if fin: return fin,base or fin
            empty += 1
            if empty>=15: raise RuntimeError("ComfyUI отчитался, но файлов нет. Проверь output и консоль ComfyUI.")
        raise RuntimeError("таймаут генерации")
    finally:
        PID_MAP.pop(pid,None)
        if ch["epoch"]==info["epoch"]: ch["progress"]=None
        item.pop("progress",None)

async def gen_fb(ch,s,content,seed,item,init=None,denoise=None,size=None):
    """comfy_generate + автоподбор рабочего маршрута CLIP при ошибке NoneType-clip."""
    global CLIP_MODE
    order = [CLIP_MODE]+[m for m in (2,1,0) if m!=CLIP_MODE]
    last = None
    for n,m in enumerate(order):
        CLIP_MODE = m
        try:
            return await comfy_generate(ch,s,content,seed,item,init=init,denoise=denoise,size=size)
        except RuntimeError as e:
            if ch["cancel"] or not _clip_broken(e): raise
            last = e
            if n+1 < len(order):
                say(ch,"b",f"CLP-сбой в режиме {m} ({str(e)[:90]}) — пробую режим {order[n+1]}.")
    raise RuntimeError(f"CLIP сломан во всех режимах: {last} | вероятно, чекпоинт без CLIP "
                       f"или CLIPSetLastLayer удалён из твоего ComfyUI — поставь clip_skip=0 в config.json")

# ============================================================================
# КРИТИК / ОЦЕНКА
# ============================================================================
async def critique(s,path,scene,prompt):
    empty = {"ok":True,"comment":"","defects":[],"description":""}
    if not (CONFIG.get("use_critic",True) or CONFIG.get("feedback_on",True)):
        return dict(empty,comment="осмотр выключен")
    if not os.path.isfile(path): return dict(empty,comment="файл картинки не найден")
    try:
        raw,fmt = _shrink(path); b64 = base64.b64encode(raw).decode()
        txt = await vlm(s, CRIT_T.format(theme=(scene or prompt)[:1800], prompt=prompt),
                        [(b64,fmt)], S_CRIT, temp=0.2, mt=1400, timeout=240)
        v = parse_json(txt)
        if isinstance(v,dict): return dict(empty,**{k:v[k] for k in empty if k in v})
    except Exception as e:
        return dict(empty,comment=f"критик не смог посмотреть: {str(e)[:160]}")
    return dict(empty)
async def feedback(s,theme,prompt,v,path=None,on_delta=None):
    desc = (v or {}).get("description") or ""
    text = FB_T.format(theme=(theme or "")[:1500],prompt=prompt,
                       desc=desc or "(описания нет, смотри на картинку сам)",
                       defects=", ".join((v or {}).get("defects") or []) or "нет")
    msgs = None
    if path and os.path.isfile(path):
        try:
            raw,fmt = _shrink(path); b64 = base64.b64encode(raw).decode()
            msgs = [{"role":"user","content":[{"type":"text","text":text},
                    {"type":"image_url","image_url":{"url":f"data:image/{fmt};base64,"+b64}}]}]
        except Exception: pass
    if msgs is None: msgs = [{"role":"user","content":text}]
    try:
        return await llm_stream(s,msgs,0.5,700,on_delta=on_delta,timeout=180)
    except asyncio.CancelledError: raise
    except Exception:
        try: return await llm(s,[{"role":"user","content":text}],0.5,700,timeout=180)
        except Exception as e: return f"(оценка не получена: {e})"

# ============================================================================
# СЦЕНАРНЫЕ ПОТОКИ
# ============================================================================
def propose(ch,scene,seed=None,sources=None,theme=None):
    ch["draft"] = {"scene":scene,"seed":seed,"sources":sources or [],
                   "theme":theme or scene.split("\n")[0][:60]}
    ch["stage"] = "approve"
    say(ch,"s",scene)
    chat_add(ch,"assistant","Сценарий предложен:\n"+scene[:900])
    save_chats()
async def run_scene(ch,user_text,answers=None):
    """Режим 1/4: текст -> полный сценарий -> согласование."""
    ch["busy"]=True; ch["cancel"]=False; epoch=ch["epoch"]; ch["stage"]="idle"
    try:
        s = HTTP; mem = load_mem()
        text = NEW_RE.sub("",user_text).strip() or user_text
        forced_new = bool(NEW_RE.match(user_text))
        base = pick_base(ch,user_text)
        is_edit = (not forced_new) and base.get("scene") and (EDIT_RE.search(text) or len(text)<160) \
                  and not is_full_scene(text)
        if is_edit:
            say(ch,"b","Правлю сценарий прошлого кадра…")
            msgs = [{"role":"system","content":SCENE_EDIT_SYS},
                    {"role":"user","content":f"[ТЕКУЩИЙ СЦЕНАРИЙ]\n{base['scene']}\n\n[ПРАВКИ]\n{text}"}]
            scene = await llm(s,msgs,0.4,1800,timeout=240)
            if ch["epoch"]!=epoch: return
            propose(ch,scene.strip(),seed=base.get("seed"),sources=base.get("sources") or [],
                    theme=ch["last"].get("theme"))
            return
        if is_full_scene(text):
            propose(ch,text.strip(),theme=text.split("\n")[0][:60]); return
        say(ch,"b","Разворачиваю сюжет в полный сценарий…")
        msgs = [{"role":"system","content":SCENE_SYS}]
        if mem.get("summary"):
            msgs.append({"role":"user","content":"[ПРОШЛЫЙ ОПЫТ — фон, не тема]\n"+mem["summary"]})
        msgs += chat_msgs(ch,6)
        u = "[ЗАПРОС]\n"+text
        if answers: u += "\n\n[ОТВЕТЫ НА УТОЧНЯЮЩИЕ ВОПРОСА]\n"+answers+"\nСопоставь «номер-вариант» со списком вопросов; что не отвечено — реши сам."
        msgs.append({"role":"user","content":u})
        scene = await llm(s,msgs,0.7,1800,timeout=240)
        if ch["epoch"]!=epoch: return
        chat_add(ch,"user",user_text)
        propose(ch,scene.strip())
    except asyncio.CancelledError:
        if ch["epoch"]==epoch: say(ch,"b","Остановлено.")
    except Exception as e:
        if ch["epoch"]==epoch: say(ch,"e",f"Ошибка: {e}")
    finally:
        ch["busy"]=False; save_chats()
async def revise_draft(ch,request):
    """Правки сценария на согласовании."""
    ch["busy"]=True; epoch=ch["epoch"]
    try:
        d = ch["draft"]
        msgs = [{"role":"system","content":SCENE_EDIT_SYS},
                {"role":"user","content":f"[ТЕКУЩИЙ СЦЕНАРИЙ]\n{d['scene']}\n\n[ПРАВКИ]\n{request}"}]
        scene = await llm(HTTP,msgs,0.4,1800,timeout=240)
        if ch["epoch"]!=epoch: return
        chat_add(ch,"user",request)
        propose(ch,scene.strip(),seed=d.get("seed"),sources=d.get("sources"),theme=d.get("theme"))
    except asyncio.CancelledError:
        if ch["epoch"]==epoch: say(ch,"b","Остановлено.")
    except Exception as e:
        if ch["epoch"]==epoch: say(ch,"e",f"Ошибка: {e}")
    finally:
        ch["busy"]=False; save_chats()
async def run_source(ch,user_text,images):
    """Режим 2: исходник + правки -> сценарий -> сразу генерация."""
    ch["busy"]=True; ch["cancel"]=False; epoch=ch["epoch"]; ch["stage"]="idle"
    try:
        s = HTTP
        if images:
            say(ch,"b","Описываю исходник и вношу правки…")
            pics = [_shrink(ref_path(f)) for f in images if os.path.isfile(ref_path(f))]
            if not pics: raise RuntimeError("исходники не найдены")
            pics = [(base64.b64encode(b).decode(),f) for b,f in pics]
            req = user_text or "Опиши как есть, ничего не меняй."
            scene = await vlm(s, SRC_SYS+"\n\n[ПРАВКИ ПОЛЬЗОВАТЕЛЯ]\n"+req, pics, None,
                              temp=0.3, mt=2000, timeout=300)
            seed = next_seed(); sources = images
        else:
            base = last_shot(ch)
            if not (base and base.get("scene")):
                say(ch,"e","Прикрепи исходник-картинку (📎) или напиши правки к прошлому кадру."); return
            say(ch,"b","Правлю сценарий прошлого исходника…")
            msgs = [{"role":"system","content":SCENE_EDIT_SYS},
                    {"role":"user","content":f"[ТЕКУЩИЙ СЦЕНАРИЙ]\n{base['scene']}\n\n[ПРАВКИ]\n{user_text}"}]
            scene = await llm(s,msgs,0.4,1800,timeout=240)
            seed = base.get("seed") or random.randint(1,2**32-1)
            sources = base.get("sources") or []
        if ch["epoch"]!=epoch: return
        scene = scene.strip()
        chat_add(ch,"user",user_text or "(исходник без правок)")
        say(ch,"s",scene)
        ch["draft"] = {"scene":scene,"seed":seed,"sources":sources,"theme":scene.split("\n")[0][:60]}
        await run_generate(ch,skip_busy=True)
    except asyncio.CancelledError:
        if ch["epoch"]==epoch: say(ch,"b","Остановлено.")
    except Exception as e:
        if ch["epoch"]==epoch: say(ch,"e",f"Ошибка: {e}")
    finally:
        ch["busy"]=False; save_chats()
async def run_ask(ch,user_text):
    """Режим 4: анкета -> ответы -> сценарий -> согласование."""
    ch["busy"]=True; epoch=ch["epoch"]; ch["stage"]="idle"; ch["ask_theme"]=user_text
    try:
        say(ch,"b","Готовлю вопросы…")
        msgs = [{"role":"system","content":ASK_SYS}] + chat_msgs(ch,4) + \
               [{"role":"user","content":"[ТЕМА]\n"+user_text}]
        obj = parse_json(await llm(HTTP,msgs,0.7,4096,True,schema=S_QUESTIONS,timeout=300))
        if ch["epoch"]!=epoch: return
        raw = obj.get("questions") or []
        lines,clean = [],[]
        for i,q in enumerate(raw,1):
            if not isinstance(q,dict): continue
            text = str(q.get("q") or "").strip()
            if not text: continue
            opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:8]
            clean.append({"q":text,"options":opts})
            lines.append(f"{i}. {text}")
            if opts: lines.append("    "+"    ".join(f"{j}) {o}" for j,o in enumerate(opts,1)))
        if not clean:
            say(ch,"b","Вопросов не вышло — собираю сценарий сразу.")
            await run_scene(ch,user_text); return
        ch["stage"]="ask"; chat_add(ch,"user",user_text)
        say(ch,"q",f"Вопросов: {len(clean)}\n\n"+"\n".join(lines)+
            "\n\nОтветь номерами: «1-2, 2-5, 3-1». Можно словами. «генерируй» — решу сам.")
    except asyncio.CancelledError:
        if ch["epoch"]==epoch: say(ch,"b","Остановлено.")
    except Exception as e:
        if ch["epoch"]==epoch: say(ch,"e",f"Ошибка: {e}")
    finally:
        ch["busy"]=False; save_chats()
async def run_generate(ch,skip_busy=False,direct_tags=None):
    """Сценарий (или готовые теги) -> теггер ComfyUI -> Illustrious -> критик -> оценка."""
    if not skip_busy: ch["busy"]=True
    ch["cancel"]=False; epoch=ch["epoch"]; ch["stage"]="idle"
    mem = load_mem(); mem.setdefault("summary",""); mem.setdefault("entries",[])
    try:
        s = HTTP; await fetch_assets(s)
        if direct_tags is not None:
            scene = ""; tags = clean_tags(direct_tags)
            theme = tags[:60]; seed = next_seed(); sources = []
            if not tags: say(ch,"e","Пустой промпт."); return
        else:
            d = ch["draft"]
            if not d: say(ch,"e","Нет сценария на генерацию."); return
            scene = d["scene"]; theme = d.get("theme") or scene.split("\n")[0][:60]
            seed = next_seed(keep=d.get("seed")); sources = d.get("sources") or []
        init=None; size=None; denoise=None
        if sources and CONFIG.get("src_img2img",True) and Image is not None:
            src = ref_path(sources[0])
            if os.path.isfile(src):
                try:
                    iw,ih = Image.open(src).size
                    size = pick_canvas(iw,ih) if CONFIG.get("src_auto_size",True) else None
                    denoise = float(CONFIG.get("src_denoise",0.55))
                    init = await comfy_upload(s,src)
                    say(ch,"b",f"Исходник как img2img-основа · denoise {denoise}" +
                        (f" · холст {size[0]}x{size[1]}" if size else ""))
                except Exception as e:
                    print("[src] основа не загружена:",e); init=None
        if direct_tags is None:
            say(ch,"b",f"Qwen даёт ядро тегов → {CONFIG['tag_model']} достраивает промпт…")
            ch["progress"]={"active":True,"idx":None,"stage":"теги (Qwen → TIPO)","value":0,"max":0,"pct":0,"overall":0}
            tags = await make_tags(ch,s,scene,precise=bool(sources))
            ch["progress"]=None
            if ch["epoch"]!=epoch: return
            say(ch,"b",f"Тегов: {len(tags.split(','))}. Генерирую.")
        first = max([int(it.get("idx") or 0) for it in ch["items"]] or [0])+1
        idx = first
        if ch["cancel"] or ch["epoch"]!=epoch: return
        item = {"idx":idx,"status":"генерация","prompt":tags,"scene":scene,"seed":seed,
                "file":None,"verdict":"","ok":None,"retry":False,
                "mode":ch["mode"],"src":sources}
        ch["items"].append(item)
        fin,base_img = await gen_fb(ch,s,tags,seed,item,init=init,denoise=denoise,size=size)
        if ch["epoch"]!=epoch: return
        fin,base_img = archive(fin,base_img,theme,seed)
        item["file"],item["status"] = fin,"осмотр"
        v = await critique(s,img_path(fin),scene,tags)
        if ch["epoch"]!=epoch: return
        if CONFIG.get("use_critic",True) and not v.get("ok",True) and not ch["cancel"]:
            item["retry"],item["status"]=True,"ретрай"
            fixed = parse_json(await llm(s,[{"role":"system","content":"Ты правишь теги аниме-генерации, сохраняя тему и стиль."},
                {"role":"user","content":FIX_T.format(prompt=tags,defects=", ".join(v.get("defects",[])))}],
                0.4,900,True,schema=S_FIX,timeout=120))
            tags = clean_tags(fixed.get("prompt",tags)); seed += 1
            fin,base_img = await gen_fb(ch,s,tags,seed,item,init=init,denoise=denoise,size=size)
            if ch["epoch"]!=epoch: return
            fin,base_img = archive(fin,base_img,theme,seed)
            item.update(file=fin,prompt=tags,seed=seed,status="осмотр 2")
            v = await critique(s,img_path(fin),scene,tags)
            if ch["epoch"]!=epoch: return
        item["ok"] = bool(v.get("ok",True))
        item["verdict"] = ((v.get("comment","") or "")+" "+", ".join(v.get("defects",[]) or [])).strip()
        fb = ""
        if CONFIG.get("feedback_on",True) and not ch["cancel"]:
            item["status"]="оценка"
            head = f"Кадр #{idx} · seed {seed}\n"; msg = say(ch,"b",head); buf={"t":""}
            async def on_delta(t):
                if ch["epoch"]!=epoch: return
                buf["t"]+=t; msg["text"]=head+buf["t"]
            fb = await feedback(s,scene or theme,tags,v,img_path(fin),on_delta=on_delta)
            if ch["epoch"]!=epoch: return
            msg["text"] = head+(fb or "оценку получить не удалось."); save_chats()
        if fb: item["verdict"]=(item["verdict"]+"\n\n"+fb).strip()
        item["status"] = "готово" if item["ok"] else "брак"
        sidecar(fin,theme,scene,tags,seed,item["verdict"],fb)
        last = ch["last"]; last["theme"]=theme; last["ts"]=time.time()
        last["shots"].append({"idx":idx,"prompt":tags,"scene":scene,"seed":seed,"file":fin,
                              "ok":item["ok"],"defects":v.get("defects",[]),
                              "desc":(v.get("description") or "")[:1200],
                              "verdict":(v.get("comment") or "")[:400],
                              "feedback":(fb or "")[:600],"sources":sources})
        del last["shots"][:-6]
        mem["entries"].append({"theme":theme,"scene":scene[:800],"prompt":tags,"seed":seed,
                               "file":fin,"ok":item["ok"],"defects":v.get("defects",[]),
                               "feedback":fb[:600]})
        mem["entries"]=mem["entries"][-200:]; save_mem(mem)
        chat_add(ch,"assistant",f"Кадр #{idx} готов. seed {seed}. файл {fin}. ok={item['ok']}. теги: {tags[:700]}")
        if not ch["cancel"] and ch["epoch"]==epoch and mem["entries"]:
            compact = [{k:e.get(k) for k in ("theme","ok","defects","feedback")} for e in mem["entries"][-6:]]
            mem["summary"] = await llm(s,[{"role":"user","content":SUM_T.format(old=mem["summary"],
                new=json.dumps(compact,ensure_ascii=False))}],0.3,400,timeout=120)
            save_mem(mem)
        if ch["epoch"]==epoch:
            say(ch,"b","Готово. Файлы: "+save_dir()+
                "\nПравки («убери очки», «сделай ночь») — поправлю этот кадр. «новая тема: …» — заново.")
    except asyncio.CancelledError:
        if ch["epoch"]==epoch: say(ch,"b","Остановлено.")
    except Exception as e:
        if ch["epoch"]==epoch: say(ch,"e",f"Ошибка: {e}")
    finally:
        ch["busy"]=False; ch["cancel"]=False
        same = (ch["epoch"]==epoch)
        if same: ch["progress"]=None
        for it in ch["items"]:
            if same and it.get("status") not in ("готово","брак"): it["status"],it["ok"]="брак",False
            it.pop("progress",None)
        save_chats()

# ============================================================================
# ТЕЛЕМЕТРИЯ
# ============================================================================
HW = {"ts":0.0,"data":{"cpu":"","cpu_temp":None,"cpu_load":None,"cpu_power":None,"ram":None,"gpus":[]}}
CPU_TEMP={"ts":0.0,"val":None}; CPU_POWER={"ts":0.0,"val":None}
def _sh(cmd,timeout=10):
    kw = {"capture_output":True,"text":True,"timeout":timeout}
    if os.name=="nt": kw["creationflags"]=0x08000000
    try: return subprocess.run(cmd,**kw).stdout or ""
    except Exception: return ""
def _num(x):
    try: return round(float(str(x).strip().replace(",",".")),1)
    except Exception: return None
def _cpu_name():
    if os.name=="nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(k,"ProcessorNameString")[0]).strip()
        except Exception: pass
    return platform.processor() or "CPU"
def _gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        for p in (r"C:\Windows\System32\nvidia-smi.exe",r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(p): exe=p; break
    if not exe: return []
    out = _sh([exe,"--query-gpu=name,temperature.gpu,memory.total,memory.used,utilization.gpu,power.draw",
               "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p)>=5:
            gpus.append({"name":p[0],"temp":_num(p[1]),"mem_total":_num(p[2]),"mem_used":_num(p[3]),
                         "util":_num(p[4]),"power":_num(p[5]) if len(p)>=6 else None})
    return gpus
def _ram():
    try:
        import psutil
        v = psutil.virtual_memory()
        return {"total":round(v.total/2**30,1),"used":round((v.total-v.available)/2**30,1)}
    except Exception: return None
def _cpu_load():
    try:
        import psutil; return round(psutil.cpu_percent(interval=None),1)
    except Exception: return None
def _ps(): return shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
def _cpu_sensor(kind,cache,lo,hi):
    now = time.time()
    if now-cache["ts"]<10: return cache["val"]
    cache["ts"]=now; val=None
    if os.name=="nt":
        for ns in ("root/LibreHardwareMonitor","root/OpenHardwareMonitor"):
            out = _sh([_ps(),"-NoProfile","-NonInteractive","-Command",
                "Get-CimInstance -Namespace "+ns+" -ClassName Sensor -ErrorAction SilentlyContinue | "
                "Where-Object {$_.SensorType -eq '"+kind+"' -and $_.Identifier -match 'cpu'} | "
                "Select-Object -ExpandProperty Value"],timeout=12)
            vals = [v for v in (_num(x) for x in out.split()) if v is not None and lo<v<hi]
            if vals: val=max(vals); break
    if val is None: cache["ts"]=now+40
    cache["val"]=val; return val
def collect_hw():
    now = time.time()
    if now-HW["ts"]<2 and HW["data"]["gpus"]: return HW["data"]
    HW["ts"]=now
    if not HW["data"]["cpu"]: HW["data"]["cpu"]=_cpu_name()
    HW["data"]["gpus"]=_gpus(); HW["data"]["ram"]=_ram(); HW["data"]["cpu_load"]=_cpu_load()
    HW["data"]["cpu_temp"]=_cpu_sensor("Temperature",CPU_TEMP,0,120)
    HW["data"]["cpu_power"]=_cpu_sensor("Power",CPU_POWER,0,2000)
    return HW["data"]

# ============================================================================
# ВЕБ-ХЕНДЛЕРЫ
# ============================================================================
async def h_root(r):
    ideas = load_ideas() or {k:[] for k in IDEA_MODES}
    return web.Response(text=HTML.replace("{{IDEAS}}",json.dumps(ideas,ensure_ascii=False)),
                        content_type="text/html")
async def h_ideas_regen(r):
    mode = None
    try: mode = ((await r.json()) or {}).get("mode")
    except Exception: pass
    if mode not in IDEA_MODES: mode = "real"
    try:
        ideas = await generate_ideas(mode)
        return web.json_response({"ok":True,"ideas":ideas,"mode":mode})
    except Exception as e:
        return web.json_response({"ok":False,"error":str(e)[:200],"mode":mode})
async def h_ideas_get(r):
    return web.json_response(load_ideas() or {k:[] for k in IDEA_MODES})
async def h_state(r):
    ch = CHATS.get(r.query.get("id")) or next(iter(CHATS.values()))
    return web.json_response({"id":ch["id"],"title":ch["title"],"busy":ch["busy"],
        "messages":ch["messages"],"items":ch["items"],"progress":ch["progress"],
        "stage":ch["stage"],"mode":ch["mode"],"ideas_ver":IDEAS_VER,"seed":CONFIG.get("seed",0),
        "chats":[{"id":c["id"],"title":c["title"],"busy":c["busy"],"mode":c["mode"]} for c in CHATS.values()]})
async def h_mode(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    if ch and d.get("mode") in CHAT_MODES:
        ch["mode"]=d["mode"]; save_chats()
        return web.json_response({"ok":True})
    return web.json_response({"ok":False})
async def h_chat_create(r):
    d = {}
    try: d = await r.json()
    except Exception: pass
    ch = make_chat((d.get("title") or "").strip() or None); save_chats()
    return web.json_response({"ok":True,"id":ch["id"],"title":ch["title"]})
async def h_chat_delete(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    if not ch: return web.json_response({"ok":False})
    ch["cancel"]=True; t=ch.get("task")
    if t and not t.done(): t.cancel()
    ch["epoch"]+=1
    for pid in [p for p,i in PID_MAP.items() if i["chat"] is ch]: PID_MAP.pop(pid,None)
    del CHATS[ch["id"]]
    if not CHATS: make_chat("Чат 1")
    save_chats(); return web.json_response({"ok":True})
async def h_chat_rename(r):
    d = await r.json(); ch = CHATS.get(d.get("id")); t = (d.get("title") or "").strip()
    if ch and t: ch["title"]=t[:60]; save_chats()
    return web.json_response({"ok":bool(ch and t)})
async def h_stop(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    if ch:
        ch["cancel"]=True; t=ch.get("task")
        if t and not t.done(): t.cancel()
        if HTTP:
            try:
                async with HTTP.post(COMFY_URL+"/interrupt",timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    await resp.read()
            except Exception: pass
    return web.json_response({"ok":bool(ch)})
async def h_reset(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    if not ch: return web.json_response({"ok":False})
    ch["epoch"]+=1; ch["cancel"]=True; t=ch.get("task")
    if t and not t.done(): t.cancel()
    ch["messages"],ch["items"],ch["turns"]=[],[],[]
    ch["stage"]="idle"; ch["draft"]=None; ch["last"]={"theme":"","shots":[],"ts":0}; ch["progress"]=None
    for pid in [p for p,i in PID_MAP.items() if i["chat"] is ch]: PID_MAP.pop(pid,None)
    save_chats(); return web.json_response({"ok":True})
async def h_new(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    if not ch: return web.json_response({"ok":False})
    clear_last(ch); say(ch,"b","Прошлый кадр забыт. Следующий запрос — новая тема.")
    return web.json_response({"ok":True})
async def h_upload(r):
    if Image is None:
        return web.json_response({"ok":False,"error":"нужен Pillow: pip install pillow"},status=500)
    try: reader = await r.multipart()
    except Exception as e:
        return web.json_response({"ok":False,"error":f"не multipart: {e}"},status=400)
    out = []
    async for part in reader:
        if part.name!="file": continue
        blob = await part.read(decode=False)
        try:
            im = Image.open(io.BytesIO(blob)); im.thumbnail((2048,2048),Image.LANCZOS)
            if im.mode not in ("RGB","RGBA"):
                im = im.convert("RGBA" if "transparency" in im.info else "RGB")
            fn = "src-"+time.strftime("%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:6]+".png"
            im.save(ref_path(fn),"PNG")
            out.append({"file":fn,"url":"/uploads/"+fn,"w":im.width,"h":im.height})
        except Exception as e:
            out.append({"error":str(e)[:120]})
    return web.json_response({"ok":any("file" in o for o in out),"files":out})
async def h_ref_file(r):
    p = ref_path(r.match_info["name"])
    return web.FileResponse(p) if os.path.isfile(p) else web.Response(status=404)
async def h_chat(r):
    d = await r.json(); ch = CHATS.get(d.get("id"))
    t = (d.get("text") or "").strip()
    if d.get("mode") in CHAT_MODES: ch and ch.update(mode=d["mode"])
    images = []
    for im in (d.get("images") or [])[:4]:
        fn = os.path.basename(str((im or {}).get("file") if isinstance(im,dict) else im or ""))
        if fn and os.path.isfile(ref_path(fn)): images.append(fn)
    if not ch or (not t and not images): return web.json_response({"ok":False})
    if ch["busy"]: return web.json_response({"ok":False,"busy":True})
    if not t: t = "Переделай исходник, правок нет — опиши как есть." if images else ""
    if not ch["turns"] and re.match(r"^Чат \d+$",ch["title"] or ""):
        ch["title"] = t[:42]+("…" if len(t)>42 else "")
    say(ch,"u",t,images=[{"file":f} for f in images] or None)
    # 1) сценарий на согласовании
    if ch["stage"]=="approve":
        if APPROVE_RE.match(t):
            launch(ch,run_generate(ch))
        else:
            launch(ch,revise_draft(ch,t))
        return web.json_response({"ok":True})
    # 2) ответы анкеты
    if ch["stage"]=="ask":
        ch["stage"]="idle"
        theme = ch["ask_theme"] or t
        if t.lower() in ("генерируй","пропусти","skip","давай","сам"):
            launch(ch,run_scene(ch,theme))
        else:
            launch(ch,run_scene(ch,theme,answers=t))
        return web.json_response({"ok":True})
    if APPROVE_RE.match(t):
        say(ch,"b","Сейчас нет сценария на согласовании. Напиши тему или выбери идею.")
        return web.json_response({"ok":True})
    # 3) по режиму
    mode = ch["mode"]
    if mode=="prompt":
        raw = extract_prompt(t)
        if not raw:
            say(ch,"e","Режим «промт»: пришли английские теги через запятую.")
            return web.json_response({"ok":True})
        launch(ch,run_generate(ch,direct_tags=raw))
    elif mode=="source":
        launch(ch,run_source(ch,t,images))
    elif mode=="ask":
        launch(ch,run_ask(ch,t))
    else:
        launch(ch,run_scene(ch,t))
    return web.json_response({"ok":True})
async def h_cfg_get(r):
    if r.query.get("refresh") or not ASSETS["loras"]:
        ASSETS.update(ckpts=[],loras=[],upscales=[],detectors=[],impact=False,tipo=False,samplers=[],schedulers=[])
        await fetch_assets()
    ASSETS["samplers"] = ASSETS.get("samplers") or SAMPLER_NAMES
    ASSETS["schedulers"] = ASSETS.get("schedulers") or SCHEDULER_NAMES
    return web.json_response({"config":CONFIG,"assets":ASSETS,"resolutions":list(RESOLUTIONS.keys())})
async def h_cfg_set(r):
    d = await r.json()
    for k in ("ckpt","resolution","upscaler","steps","cfg","max_images","use_critic","feedback_on",
              "sampler","scheduler","clip_skip","quality_on","artist","save_dir",
              "hires_on","hires_scale","hires_denoise","hires_steps","hires_cfg","hires_sampler",
              "hires_scheduler","hires_max_px","src_img2img","src_denoise","src_auto_size",
              "tag_model","tag_url","tag_temp","tag_fallback",
              "detail_on","detail_feather","detail_max_size","detail_seed_mode","tipo_tokens",
              "seed","seed_mode","denoise",
              "tipo_autostart","tipo_exe","tipo_model_path","tipo_device","tipo_on"):
        if k in d: CONFIG[k]=d[k]
    for k in ("fw_fh","custom_wh"):
        if k in d and isinstance(d[k],list) and len(d[k])==2:
            CONFIG[k]=[max(256,int(d[k][0])//8*8),max(256,int(d[k][1])//8*8)]
    if "loras" in d and isinstance(d["loras"],list):
        CONFIG["loras"]=d["loras"][:10]
        while len(CONFIG["loras"])<10: CONFIG["loras"].append({"name":"","strength":0.8,"on":False})
    if "detailers" in d and isinstance(d["detailers"],list):
        CONFIG["detailers"]=norm_detailers(d["detailers"])
    try: CONFIG["seed"] = max(0,min(2**64-1,int(CONFIG.get("seed") or 0)))
    except Exception: CONFIG["seed"] = 0
    try: CONFIG["denoise"] = max(0.05,min(1.0,float(CONFIG.get("denoise",1.0))))
    except Exception: CONFIG["denoise"] = 1.0
    save_config(CONFIG); return web.json_response({"ok":True})
async def h_img(r):
    p = img_path(r.match_info["name"])
    return web.FileResponse(p) if os.path.isfile(p) else web.Response(status=404)
async def h_hw(r):
    data = await asyncio.get_running_loop().run_in_executor(None,collect_hw)
    return web.json_response(data)

# ============================================================================
# HTML
# ============================================================================
HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HERETIC v2</title>
<style>
:root{--ink900:#08050f;--ink850:#0d0819;--ink800:#130c22;--ink750:#18102c;--ink700:#1e1636;
--ink600:#2a1f4a;--line:#362a60;--soft:#241b42;--txt:#f4ecff;--dim:#b7a9d6;--mute:#8a7ab0;
--mag:#ff2d95;--cyan:#22e0ff;--lime:#b6ff3d;--amber:#ffb020;--violet:#8b5cff;
--ok:#3ddc84;--bad:#ff4d5e;--info:#5aa9ff;
--hot:linear-gradient(100deg,#ff2d95,#8b5cff 48%,#22e0ff);--cool:linear-gradient(100deg,#22e0ff,#b6ff3d);
--top:60px;--chat:470px;--rail:334px;--mono:ui-monospace,Consolas,monospace;color-scheme:dark}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:#6b3fb8 var(--ink850)}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-track{background:var(--ink850)}
::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#8b5cff,#ff2d95);border-radius:8px}
html,body{height:100%}body{margin:0;background:var(--ink900);color:var(--txt);overflow:hidden;
font:14px/1.5 "Segoe UI",system-ui,sans-serif}
body.dragging{user-select:none;cursor:col-resize}
.top{position:relative;z-index:20;height:var(--top);display:flex;align-items:center;gap:12px;padding:0 14px;
background:rgba(13,8,25,.9);border-bottom:1px solid var(--line)}
.top:after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:1px;background:var(--hot);opacity:.7}
.brand{display:flex;align-items:center;gap:10px}
.mark{width:34px;height:34px;border-radius:10px;background:var(--hot);display:grid;place-items:center;
font-weight:900;font-size:18px;color:#0b0714}
.bt b{display:block;font-size:15px;letter-spacing:.22em}.bt span{display:block;font-size:9px;letter-spacing:.3em;color:var(--mute)}
.status{display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:999px;background:var(--ink750);
border:1px solid var(--line);font-size:12px;color:var(--dim)}
.status i{width:8px;height:8px;border-radius:50%;background:var(--ok)}
.status.busy i{background:var(--mag);animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.35}}
.spacer{flex:1}.hw{display:flex;gap:8px;overflow-x:auto;max-width:52vw;scrollbar-width:none}
.chip{flex:0 0 auto;min-width:104px;padding:4px 9px;border-radius:9px;background:var(--ink750);
border:1px solid var(--soft);font-size:10.5px;position:relative;overflow:hidden}
.chip b{display:block;font-size:11px;white-space:nowrap}.chip s{display:block;text-decoration:none;color:var(--mute);font-family:var(--mono);white-space:nowrap}
.chip u{position:absolute;left:0;bottom:0;height:2px;background:var(--cool)}
.chip.hot u{background:linear-gradient(100deg,#ffb020,#ff2d95)}
.t-ok{color:var(--lime)}.t-warn{color:var(--amber)}.t-bad{color:var(--bad)}
.chip.prog{min-width:170px;border-color:#3f6b23}.chip.prog b{color:var(--lime)}
.icobtn{width:34px;height:34px;border-radius:9px;background:var(--ink750);color:var(--dim);
border:1px solid var(--line);cursor:pointer;font-size:15px;display:grid;place-items:center}
.icobtn:hover{color:var(--txt);border-color:var(--mag)}.icobtn.on{color:var(--lime)}
.panes{position:relative;z-index:10;display:flex;height:calc(100% - var(--top));overflow:hidden}
.pane{display:flex;flex-direction:column;min-width:0;background:rgba(19,12,34,.85);position:relative}
.chat{width:var(--chat);flex:0 0 auto;border-right:1px solid var(--line)}
.gallery{flex:1 1 auto;min-width:200px}
.rail{width:var(--rail);flex:0 0 auto;border-left:1px solid var(--line);background:rgba(13,8,25,.92)}
.phead{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--soft);min-height:44px}
.phead h2{margin:0;font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:var(--dim)}
.pill{font-size:10.5px;padding:3px 9px;border-radius:999px;background:var(--ink700);border:1px solid var(--line);color:var(--mute);font-family:var(--mono)}
.pill.live{color:var(--lime);border-color:#3f6b23}.pill.dirty{color:var(--amber);border-color:#7a5514}
.mini{background:var(--ink700);border:1px solid var(--line);color:var(--dim);border-radius:7px;padding:4px 10px;font-size:11px;cursor:pointer}
.mini:hover{color:var(--txt);border-color:var(--cyan)}.mini.on{color:var(--lime);border-color:#4c7a2a}
.chatbar{display:flex;gap:6px;padding:6px 8px;border-bottom:1px solid var(--soft)}
.chattabs{display:flex;flex-wrap:wrap;gap:4px;flex:1;max-height:62px;overflow-y:auto}
.chattabs.open{max-height:40vh}
.ctab{display:flex;align-items:center;gap:6px;background:var(--ink750);border:1px solid var(--soft);color:var(--mute);
border-radius:8px;padding:4px 8px;font-size:11.5px;cursor:pointer;max-width:100%}
.ctab span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px}
.ctab.on{background:var(--ink600);color:var(--txt);border-color:var(--mag)}
.ctab em{font-style:normal;opacity:.55;cursor:pointer}.ctab em:hover{color:var(--bad)}
.ctab em.arm{opacity:1;color:#fff;background:var(--bad);border-radius:5px;padding:0 6px;font-weight:700}
.ctab.add{color:var(--cyan);font-weight:700}
.bdot{width:6px;height:6px;border-radius:50%;background:var(--mag);animation:pulse 1s infinite}
.grip{flex:0 0 auto;width:8px;cursor:col-resize;background:var(--ink850);display:grid;place-items:center}
.grip i{width:2px;height:44px;background:var(--line)}.grip:hover,.grip.act{background:linear-gradient(180deg,#8b5cff,#ff2d95)}
.logwrap{flex:1;min-height:0;position:relative;display:flex}
.log{flex:1;overflow-y:auto;padding:14px 12px 6px}
.logempty{position:absolute;inset:0;display:none;padding:14px;overflow-y:auto;background:rgba(8,5,15,.55)}
.logempty.show{display:block}
.emptywrap{display:flex;flex-direction:column;justify-content:center;gap:12px;max-width:470px;margin:0 auto;min-height:100%}
.emptyhead{text-align:center}.emptyhead h3{margin:0 0 6px;font-size:13px;letter-spacing:.2em;text-transform:uppercase;
background:var(--hot);-webkit-background-clip:text;background-clip:text;color:transparent}
.emptyhead p{margin:0;color:var(--dim);font-size:12px}
.ideapanel{display:flex;flex-direction:column;gap:9px;padding:10px;border-radius:14px;border:1px solid var(--line);
background:linear-gradient(165deg,rgba(42,31,74,.6),rgba(13,8,25,.8))}
.ideatop{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.seg{display:flex;gap:3px;padding:3px;border-radius:11px;background:rgba(8,5,15,.6);border:1px solid var(--soft)}
.seg button{background:none;border:0;border-radius:8px;color:var(--mute);font:600 11.5px/1 "Segoe UI",sans-serif;
padding:7px 9px;cursor:pointer;white-space:nowrap}
.seg button.on{color:#0b0714;background:var(--cool)}
.seg button[data-m=fantasy].on{background:linear-gradient(100deg,#ffb020,#ff2d95)}
.seeds{max-height:38vh;overflow-y:auto;display:flex;flex-direction:column;gap:7px}
.idea{display:flex;background:var(--ink750);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.idea:hover{border-color:var(--mag)}
.idea .n{flex:0 0 auto;width:26px;display:grid;place-items:center;font:10px var(--mono);color:var(--mute);background:rgba(8,5,15,.45)}
.idea .body{flex:1;background:none;border:0;color:var(--txt);text-align:left;padding:9px 11px;font:12.5px/1.55 "Segoe UI",sans-serif;cursor:pointer}
.idea .acts{display:flex;flex-direction:column;gap:2px;padding:4px;border-left:1px solid var(--soft)}
.idea .acts button{width:26px;height:24px;background:none;border:0;border-radius:6px;color:var(--mute);cursor:pointer}
.idea .acts button:hover{background:var(--ink600);color:var(--txt)}
.ideastat{font:10.5px var(--mono);color:var(--mute)}
.m{margin:0 0 10px;padding:9px 12px;border-radius:12px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid transparent}
.m .who{display:block;font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;margin-bottom:4px;opacity:.75}
.m.u{background:linear-gradient(135deg,rgba(255,45,149,.18),rgba(139,92,255,.14));border-color:#6d2a63;margin-left:26px}
.m.u .who{color:var(--mag)}
.m.b{background:var(--ink750);border-color:var(--soft);margin-right:18px}.m.b .who{color:var(--cyan)}
.m.e{background:rgba(255,77,94,.13);border-color:#7d2733}.m.e .who{color:var(--bad)}
.m.s{background:linear-gradient(160deg,rgba(139,92,255,.14),rgba(34,224,255,.07));border-color:#5b3f9e}
.m.s .who{color:var(--violet)}
.m .thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
.m .thumbs img{width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid var(--line);cursor:zoom-in}
.sacts{display:flex;gap:6px;margin-top:9px;align-items:center;flex-wrap:wrap}
.sacts .hint{font-size:10.5px;color:var(--mute)}
.qcard{margin:0 0 12px;border-radius:14px;border:1px solid #6b4a1f;overflow:hidden;
background:linear-gradient(160deg,rgba(255,176,32,.1),rgba(139,92,255,.08))}
.qhead{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid rgba(255,176,32,.22)}
.qhead b{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--amber)}
.qbody{padding:6px 12px 10px}.qrow{padding:8px 0;border-bottom:1px dashed rgba(255,255,255,.07)}
.qtext{font-size:12.5px;margin-bottom:6px}.qtext em{font-style:normal;color:var(--amber);font-family:var(--mono);margin-right:6px}
.opts{display:flex;flex-wrap:wrap;gap:5px}
.opt{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:4px 9px;font-size:11.5px;cursor:pointer}
.opt.sel{background:var(--cool);color:#08210f;border-color:transparent;font-weight:600}
.opt em{font-style:normal;opacity:.6;font-family:var(--mono);margin-right:5px}
.qcustom{margin-top:6px;width:100%;background:var(--ink850);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:6px 9px;font-size:12px}
.qfoot{padding:9px 12px;border-top:1px solid rgba(255,176,32,.22)}
.qprev{font:11.5px var(--mono);color:var(--lime);min-height:17px;margin-bottom:8px;overflow-wrap:anywhere}
.qacts{display:flex;gap:6px;align-items:center}.qacts .cnt{font-size:10.5px;color:var(--mute);margin-left:auto}
.jump{position:absolute;left:50%;transform:translateX(-50%);bottom:14px;z-index:6;display:none;align-items:center;gap:7px;
background:var(--hot);color:#0b0714;border:0;border-radius:999px;padding:7px 15px;font-size:12px;font-weight:700;cursor:pointer}
.jump.show{display:flex}.jump span{background:rgba(0,0,0,.28);border-radius:999px;padding:1px 7px;font-family:var(--mono)}
.composer{border-top:1px solid var(--line);padding:10px 12px 11px}
.modeseg{display:flex;gap:3px;padding:3px;border-radius:11px;background:rgba(8,5,15,.6);border:1px solid var(--soft);margin-bottom:8px}
.modeseg button{flex:1;background:none;border:0;border-radius:8px;color:var(--mute);font:600 11px/1.2 "Segoe UI",sans-serif;padding:7px 4px;cursor:pointer}
.modeseg button.on{color:#0b0714;background:var(--hot)}
.attbar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.att{position:relative;width:72px;height:72px;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
.att img{width:100%;height:100%;object-fit:cover}
.att .del{position:absolute;right:2px;top:2px;width:18px;height:18px;border:0;border-radius:5px;background:rgba(8,5,15,.7);color:#ff9aa5;cursor:pointer}
.chat.drop:after{content:'отпусти — прикреплю исходник';position:absolute;inset:8px;z-index:40;border:2px dashed var(--cyan);
border-radius:14px;background:rgba(34,224,255,.08);display:grid;place-items:center;color:var(--cyan);pointer-events:none}
#inp{width:100%;resize:none;background:var(--ink850);color:var(--txt);border:1px solid var(--line);border-radius:11px;
padding:9px 12px;font:13.5px/20px "Segoe UI",sans-serif;min-height:38px;max-height:220px;overflow-y:auto}
#inp:focus{outline:0;border-color:var(--mag)}
.cbar{display:flex;align-items:center;gap:8px;margin-top:8px}
.hint{font-size:10.5px;color:var(--mute)}.count{font-size:10.5px;color:var(--mute);font-family:var(--mono)}
.send{background:var(--hot);color:#0b0714;border:0;border-radius:9px;padding:8px 16px;font-size:12.5px;font-weight:800;cursor:pointer}
.send:disabled{opacity:.4}
.stop{background:rgba(255,77,94,.16);color:#ff9aa5;border:1px solid #7d2733;border-radius:9px;padding:8px 12px;font-size:12px;cursor:pointer}
.chips{display:flex;gap:5px}
.chips button{background:var(--ink750);border:1px solid var(--soft);color:var(--mute);border-radius:999px;padding:4px 11px;font-size:11px;cursor:pointer}
.chips button.on{background:var(--ink600);color:var(--txt);border-color:var(--violet)}
.grid{flex:1;overflow-y:auto;padding:14px;display:grid;gap:14px;align-content:start;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--ink800);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.chead{display:flex;align-items:center;gap:8px;padding:8px 11px;border-bottom:1px solid var(--soft)}
.badge{font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:999px;font-weight:700}
.badge.wip{background:rgba(90,169,255,.16);color:var(--info)}.badge.ok{background:rgba(61,220,132,.16);color:var(--ok)}
.badge.bad{background:rgba(255,77,94,.16);color:var(--bad)}.badge.retry{background:rgba(255,176,32,.16);color:var(--amber)}
.seed{font:10.5px var(--mono);color:var(--mute);cursor:pointer;border:1px dashed var(--line);padding:2px 7px;border-radius:6px;background:none}
.shot{position:relative;background:var(--ink850);aspect-ratio:16/9;display:grid;place-items:center;overflow:hidden}
.shot img{width:100%;height:100%;object-fit:contain;cursor:zoom-in}
.skel{position:absolute;inset:0;background:linear-gradient(100deg,#18102c 30%,#2a1f4a 50%,#18102c 70%);background-size:220% 100%;animation:sweep 1.5s linear infinite}
@keyframes sweep{from{background-position:120% 0}to{background-position:-120% 0}}
.shot .lbl{position:relative;z-index:2;font:11px var(--mono);color:var(--dim);background:rgba(8,5,15,.55);padding:4px 10px;border-radius:8px}
.pbar{position:absolute;left:0;right:0;bottom:0;height:5px;background:rgba(255,255,255,.08)}
.pbar i{display:block;height:100%;width:0;background:var(--cool)}
.verdict{padding:8px 11px;font-size:11.5px;color:var(--dim);border-top:1px solid var(--soft);white-space:pre-wrap;overflow-wrap:anywhere}
.verdict.good{color:#9ff0c4}.verdict.bad{color:#ffb3bb}
.srcs{display:flex;gap:4px;padding:6px 11px;border-top:1px solid var(--soft);align-items:center}
.srcs img{width:40px;height:40px;object-fit:cover;border-radius:6px;cursor:zoom-in}
.srcs s{text-decoration:none;font-size:9.5px;color:var(--mute);text-transform:uppercase}
details.tags,details.scene{border-top:1px solid var(--soft)}
details.tags summary,details.scene summary{padding:7px 11px;font-size:11px;color:var(--mute);cursor:pointer;list-style:none}
details.tags summary:hover,details.scene summary:hover{color:var(--txt)}
.tagbox{max-height:150px;overflow-y:auto;padding:0 11px 10px;display:flex;flex-wrap:wrap;gap:4px}
.scenebox{max-height:220px;overflow-y:auto;padding:0 11px 10px;font-size:11.5px;color:var(--dim);white-space:pre-wrap}
.tag{font:10.5px var(--mono);background:var(--ink750);border:1px solid var(--soft);color:var(--dim);border-radius:6px;padding:2px 7px;cursor:pointer}
.tag:hover{border-color:var(--lime);color:var(--lime)}
.cacts{display:flex;gap:6px;padding:9px 11px;border-top:1px solid var(--soft)}
.cacts button,.cacts a{flex:1;text-align:center;text-decoration:none;background:var(--ink750);border:1px solid var(--line);
color:var(--dim);border-radius:8px;padding:6px 4px;font-size:11px;cursor:pointer}
.cacts button:hover,.cacts a:hover{color:#fff;border-color:var(--cyan)}
.galempty{position:absolute;inset:52px 0 0;display:none;flex-direction:column;align-items:center;justify-content:center;gap:12px;text-align:center;padding:30px;pointer-events:none}
.galempty.show{display:flex}
.galempty h3{margin:0;font-size:17px;letter-spacing:.16em;text-transform:uppercase;background:var(--hot);-webkit-background-clip:text;background-clip:text;color:transparent}
.galempty p{margin:0;color:var(--mute);font-size:12.5px;max-width:420px}
.railbody{flex:1;overflow-y:auto;padding:4px 12px 14px}
.sec{padding:12px 0;border-bottom:1px solid var(--soft)}
.sech{font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:var(--violet);font-weight:800;margin-bottom:9px}
.lab{display:block;font-size:11px;color:var(--mute);margin:9px 0 4px}
select,input[type=number],input[type=text]{width:100%;background:var(--ink850);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12px}
select:focus,input:focus{outline:0;border-color:var(--cyan)}
select option,select optgroup{background:#161028;color:#f4ecff}
select option:checked{background:#8b5cff;color:#fff}
select option:hover{background:#2a1f4a;color:#b6ff3d}
.ideapanel{position:relative}
.csb-host{scrollbar-width:none}
.csb-host::-webkit-scrollbar{width:0;height:0;display:none}
.log.csb-host,.grid.csb-host,.railbody.csb-host,.seeds.csb-host{padding-right:20px}
.csb{position:absolute;right:3px;width:14px;z-index:7;opacity:0;pointer-events:none;transition:opacity .25s}
.csb.on{opacity:1;pointer-events:auto}
.csb-track{position:absolute;left:0;right:0;top:19px;bottom:19px;border-radius:999px;background:rgba(8,5,15,.55);border:1px solid var(--soft);cursor:pointer}
.csb-track:before{content:'';position:absolute;left:50%;top:8px;bottom:8px;width:2px;margin-left:-1px;background:repeating-linear-gradient(180deg,var(--line) 0 3px,transparent 3px 10px);opacity:.55;border-radius:2px}
.csb-thumb{position:absolute;left:2px;right:2px;top:0;height:40px;border:0;padding:0;border-radius:999px;background:linear-gradient(180deg,#8b5cff,#ff2d95);cursor:grab;box-shadow:0 0 10px rgba(139,92,255,.35);transition:box-shadow .15s,filter .15s;touch-action:none}
.csb-thumb:after{content:'';position:absolute;left:50%;top:50%;width:2px;height:12px;transform:translate(-50%,-50%);background:rgba(255,255,255,.75);border-radius:2px;box-shadow:-3px 0 0 rgba(255,255,255,.35),3px 0 0 rgba(255,255,255,.35)}
.csb-thumb:hover{filter:brightness(1.15);box-shadow:0 0 14px rgba(255,45,149,.55)}
.csb.act .csb-thumb{cursor:grabbing;background:linear-gradient(180deg,#22e0ff,#b6ff3d);box-shadow:0 0 16px rgba(34,224,255,.6)}
.csb-btn{position:absolute;left:1px;right:1px;height:16px;border:1px solid var(--soft);border-radius:6px;background:var(--ink750);color:var(--mute);font-size:8px;line-height:1;cursor:pointer;display:grid;place-items:center;padding:0}
.csb-btn:hover{color:var(--lime);border-color:#4c7a2a;background:var(--ink600)}
.csb-up{top:0}.csb-dn{bottom:0}
.csb-track:focus-visible,.csb-thumb:focus-visible,.csb-btn:focus-visible{outline:2px solid var(--cyan);outline-offset:1px}
.info{font-size:10.5px;color:var(--mute);font-family:var(--mono);margin-top:7px;line-height:1.6}
.presets{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.presets button{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:8px 4px;font-size:11px;cursor:pointer}
.presets button:hover{background:var(--cool);color:#0b0714}
.stepper2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stp{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--ink850)}
.stp button{width:26px;background:var(--ink700);border:0;color:var(--dim);cursor:pointer;font-size:14px}
.stp input{border:0;border-radius:0;text-align:center;padding:6px 2px}
.rng{margin:11px 0}.rng label{display:flex;justify-content:space-between;font-size:11.5px;color:var(--dim);margin-bottom:6px}
.rng b{font-family:var(--mono);color:var(--lime)}
.node{border:1px solid var(--line);border-radius:12px;background:linear-gradient(180deg,#1b1330,#120b22);overflow:hidden;margin:10px 0;box-shadow:0 10px 30px rgba(0,0,0,.35)}
.node .nhead{display:flex;align-items:center;gap:8px;padding:8px 11px;background:rgba(8,5,15,.5);border-bottom:1px solid var(--soft);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.node .nhead i{width:8px;height:8px;border-radius:2px;background:var(--violet)}
.sockets{padding:6px 11px;border-bottom:1px solid var(--soft);display:flex;flex-direction:column;gap:3px}
.sock{display:flex;align-items:center;gap:7px;font-size:10.5px;color:var(--mute)}
.sock i{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.sock s{margin-left:auto;text-decoration:none;font-family:var(--mono);color:var(--dim);max-width:62%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nrow{display:flex;align-items:center;gap:6px;padding:5px 8px}
.nrow .nl{flex:0 0 92px;font-size:10.5px;color:var(--mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nw{flex:1;display:flex;align-items:center;background:var(--ink850);border:1px solid var(--line);border-radius:999px;overflow:hidden;min-width:0}
.nw button{flex:0 0 22px;background:none;border:0;color:var(--mute);cursor:pointer;font-size:10px;padding:5px 0}
.nw button:hover{color:var(--txt);background:var(--ink600)}
.nw input,.nw select{flex:1;min-width:0;background:none;border:0;color:var(--txt);text-align:center;font:11.5px var(--mono);padding:5px 2px;-moz-appearance:textfield}
.nw input::-webkit-outer-spin-button,.nw input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.nw select{appearance:none;text-align-last:center;cursor:pointer}
.nw input:focus,.nw select:focus{outline:0;color:var(--lime)}
.dice{flex:0 0 auto;background:var(--ink700);border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:4px 8px;cursor:pointer;font-size:12px}
.dice:hover{color:var(--lime);border-color:#4c7a2a}
input[type=range]{-webkit-appearance:none;width:100%;height:5px;border-radius:5px;background:linear-gradient(90deg,#8b5cff,#ff2d95);cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#fff;border:3px solid var(--violet)}
.switch{display:flex;align-items:center;gap:10px;cursor:pointer;font-size:12px;color:var(--dim)}
.switch input{display:none}
.switch i{width:38px;height:21px;border-radius:999px;background:var(--ink600);border:1px solid var(--line);position:relative;flex:0 0 auto}
.switch i:after{content:'';position:absolute;left:2px;top:2px;width:15px;height:15px;border-radius:50%;background:var(--mute);transition:.2s}
.switch input:checked+i{background:var(--cool);border-color:transparent}
.switch input:checked+i:after{left:19px;background:#08210f}
.lrow,.drow{padding:8px 0;border-bottom:1px dashed rgba(255,255,255,.06)}
.lrow.off,.drow.off{opacity:.42}
.ltop{display:grid;grid-template-columns:38px 1fr 34px;gap:7px;align-items:center}
.ltop .num{font:10px var(--mono);color:var(--mute);text-align:right}
.dtop{display:grid;grid-template-columns:38px 54px 1fr;gap:7px;align-items:center}
.dtop .dn{font-size:11px;color:var(--dim)}
.dnums{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px}
.dnums label{display:block;font-size:9.5px;color:var(--mute);margin-bottom:2px;text-transform:uppercase}
.dnums input{padding:4px 6px;font-size:11px;text-align:center}
.railfoot{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:10px 12px;border-top:1px solid var(--line)}
.railfoot .primary{grid-column:1/-1;background:var(--hot);color:#0b0714;border:0;border-radius:9px;padding:9px;font-size:12.5px;font-weight:800;cursor:pointer}
.railfoot .ghost{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:7px;font-size:11px;cursor:pointer}
.lb{position:fixed;inset:0;z-index:90;display:none;background:rgba(5,3,10,.93)}
.lb.show{display:flex}
.lbimg{flex:1;display:grid;place-items:center;padding:20px;min-width:0}
.lbimg img{max-width:100%;max-height:100%;object-fit:contain;border-radius:10px;cursor:zoom-in}
.lbimg img.zoom{max-width:none;max-height:none;cursor:zoom-out}.lbimg.wrap{overflow:auto;display:block}
.lbside{width:330px;flex:0 0 auto;border-left:1px solid var(--line);background:rgba(13,8,25,.95);display:flex;flex-direction:column;padding:14px;gap:11px;overflow-y:auto}
.lbside h4{margin:0;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--violet)}
.lbmeta{font:11.5px var(--mono);color:var(--dim);line-height:1.9;white-space:pre-wrap}
.lbtags{flex:1;overflow-y:auto;display:flex;flex-wrap:wrap;gap:4px;align-content:start}
.lbacts{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.lbacts button,.lbacts a{background:var(--ink750);border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:8px;font-size:11.5px;cursor:pointer;text-align:center;text-decoration:none}
.lbclose{position:absolute;right:14px;top:12px;z-index:2}
.lbnav{position:absolute;top:50%;transform:translateY(-50%);width:42px;height:64px;border-radius:10px;background:rgba(26,16,48,.8);border:1px solid var(--line);color:var(--txt);font-size:20px;cursor:pointer;z-index:2}
.lbprev{left:12px}.lbnext{right:352px}
.toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:120;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{padding:9px 18px;border-radius:999px;font-size:12.5px;font-weight:600;color:#0b0714;background:var(--cool)}
.toast.warn{background:linear-gradient(100deg,#ffb020,#ff2d95)}.toast.bad{background:linear-gradient(100deg,#ff4d5e,#ff2d95);color:#fff}
.tabs{display:none}
@media (max-width:1240px){
.rail{position:absolute;right:0;top:0;bottom:0;z-index:30;transform:translateX(100%);transition:transform .28s}
body.rail-open .rail{transform:none}.lbnext{right:12px}.lbside{display:none}}
@media (max-width:900px){
:root{--top:56px}.bt span{display:none}.hw{max-width:34vw}
.panes{height:calc(100% - var(--top) - 52px)}.grip{display:none}
.chat{width:100%!important;border-right:0}.pane{display:none}
.panes[data-view=chat] .chat{display:flex}.panes[data-view=gallery] .gallery{display:flex}
.panes[data-view=rail] .rail{display:flex;position:static;transform:none;width:100%}
.tabs{display:grid;grid-template-columns:repeat(3,1fr);height:52px;background:rgba(13,8,25,.95);border-top:1px solid var(--line)}
.tabs button{background:none;border:0;color:var(--mute);font-size:12px;cursor:pointer;text-transform:uppercase}
.tabs button.on{color:var(--txt);border-top:2px solid var(--mag)}
.grid{grid-template-columns:1fr}}
</style></head><body>
<header class="top">
<div class="brand"><div class="mark">H</div><div class="bt"><b>HERETIC</b><span>v2 · KoloBokPove$ilsa</span></div></div>
<div class="status" id="status"><i></i><span id="statusText">готов</span></div>
<div class="spacer"></div>
<div class="chip prog" id="progChip" style="display:none"><b id="progT">генерация</b><s id="progL">—</s><u id="progU" style="width:0%"></u></div>
<div class="hw" id="hw"></div>
<button class="icobtn" id="soundBtn" title="Звук по завершении">🔕</button>
<button class="icobtn" id="railBtn" title="Параметры">⚙</button>
</header>
<main class="panes" id="panes" data-view="chat">
<section class="pane chat" id="chatPane">
<div class="phead"><h2>Диалог</h2><span class="pill" id="modelPill">KoloBokPove$ilsa</span>
<div class="spacer"></div>
<button class="mini" id="ideaToggle">💡 идеи</button>
<button class="mini" id="newBtn">новая тема</button>
<button class="mini" id="resetBtn">сброс</button></div>
<div class="chatbar"><div class="chattabs" id="chatTabs"></div><button class="mini" id="chatMore">▾</button></div>
<div class="logwrap">
<div class="log" id="log"></div>
<div class="logempty" id="logEmpty"><div class="emptywrap">
<div class="emptyhead"><h3>С чего начнём</h3>
<p>Выбери режим внизу, опиши сцену или прикрепи исходник. Можно взять идею — она вставится полным сценарием, правишь и отправляешь.</p></div>
<div class="ideapanel">
<div class="ideatop">
<div class="seg" id="ideaMode">
<button type="button" data-m="real" class="on">реализм/аниме</button>
<button type="button" data-m="fantasy">фантастика</button>
</div>
<button class="mini" id="regenIdeasBtn">🎲 новые идеи</button>
</div>
<div class="seeds" id="seedIdeas"></div>
<div class="ideastat"><span id="ideaCount">—</span></div>
</div></div></div>
<button class="jump" id="jumpBtn">↓ новые <span id="jumpN">0</span></button>
</div>
<div class="composer">
<div class="modeseg" id="modeSeg">
<button data-m="scene" class="on" title="Qwen развернёт сюжет, ты согласишься или поправишь">1 · сюжет</button>
<button data-m="source" title="Исходник-картинка + правки">2 · исходник</button>
<button data-m="prompt" title="Готовые английские теги как есть">3 · промт</button>
<button data-m="ask" title="Сначала уточняющие вопросы">4 · вопросы</button>
</div>
<div class="attbar" id="attBar"></div>
<textarea id="inp" rows="1" spellcheck="false" placeholder="Опиши сцену… Enter — отправить"></textarea>
<div class="cbar">
<button class="mini" id="attBtn" title="Исходники (drag&drop, Ctrl+V)"><span id="attIco">📎</span> <span id="attCount"></span></button>
<input type="file" id="attInput" accept="image/*" multiple hidden>
<span class="hint">Enter — отправить · Shift+Enter — перенос</span>
<div class="spacer"></div><span class="count" id="count">0</span>
<button class="send" id="sendBtn">Отправить</button>
<button class="stop" id="stopBtn">Стоп</button>
</div></div>
</section>
<div class="grip" id="grip" title="Ширина чата · dblclick — сброс"><i></i></div>
<section class="pane gallery">
<div class="phead"><h2>Серия</h2><span class="pill" id="galCount">0</span><div class="spacer"></div>
<div class="chips" id="filters">
<button data-f="all" class="on">все</button><button data-f="wip">в работе</button>
<button data-f="ok">готово</button><button data-f="bad">брак</button></div></div>
<div class="grid" id="grid"></div>
<div class="galempty show" id="galEmpty"><h3>Холст пуст</h3>
<p>Здесь появятся кадры: статус, seed, сценарий-пояснение, вердикт критика и теги. Во время генерации виден шаг сэмплера.</p></div>
</section>
<aside class="pane rail" id="rail">
<div class="phead"><h2>Параметры</h2><span class="pill live" id="saveState">сохранено</span>
<div class="spacer"></div><button class="mini" id="railClose">×</button></div>
<div class="railbody">
<div class="sec"><div class="sech">Пресеты</div><div class="presets" id="presets">
<button data-p="draft">Черновик</button><button data-p="final">Финал</button><button data-p="max">Максимум</button></div></div>
<div class="sec"><div class="sech">Модель</div><label class="lab">Чекпоинт</label><select id="ckpt"></select>
<div class="info" id="assetsInfo">списки не загружены</div></div>
<div class="sec"><div class="sech">Теггер · TIPO (llama-server)</div>
<label class="lab">Модель (справочно)</label><input type="text" id="tagModel">
<label class="lab">Адрес llama-server</label><input type="text" id="tagUrl">
<label class="lab">llama-server.exe</label><input type="text" id="tipoExe">
<label class="lab">Путь к GGUF</label><input type="text" id="tipoModelPath">
<label class="lab">GPU для TIPO (индекс как в nvidia-smi, «cpu» — без GPU)</label><input type="text" id="tipoDevice">
<label class="switch" style="margin-top:8px"><input type="checkbox" id="tipoAuto"><i></i><span>Автозапуск вместе с мостом</span></label>
<label class="switch" style="margin-top:8px"><input type="checkbox" id="tipoOn"><i></i><span>TIPO достраивает теги (выкл = только Qwen)</span></label>
<label class="switch" style="margin-top:8px"><input type="checkbox" id="tagFallback"><i></i><span>Fallback: достроить теги Qwen'ом, если TIPO лёг</span></label>
<div class="info" id="tagInfo">—</div></div>
<div class="sec"><div class="sech">Холст</div><label class="lab">Разрешение генерации</label><select id="res"></select>
<div class="stepper2" id="cwRow" style="display:none;margin-top:6px">
<div class="stp"><button data-t="cw" data-d="-8">−</button><input type="number" id="cw" min="256" max="4096" step="8"><button data-t="cw" data-d="8">+</button></div>
<div class="stp"><button data-t="ch" data-d="-8">−</button><input type="number" id="ch" min="256" max="4096" step="8"><button data-t="ch" data-d="8">+</button></div>
</div>
<label class="lab">Финал после апскейла · Ш × В</label>
<div class="stepper2">
<div class="stp"><button data-t="fw" data-d="-8">−</button><input type="number" id="fw" min="256" step="8"><button data-t="fw" data-d="8">+</button></div>
<div class="stp"><button data-t="fh" data-d="-8">−</button><input type="number" id="fh" min="256" step="8"><button data-t="fh" data-d="8">+</button></div>
</div>
<label class="lab">Апскейлер</label><select id="up"></select></div>
<div class="sec"><div class="sech">KSampler · как в ComfyUI</div>
<div class="node">
<div class="nhead"><i></i>KSampler · база</div>
<div class="sockets">
<div class="sock"><i style="background:#b388ff"></i>модель<s id="sockModel">—</s></div>
<div class="sock"><i style="background:#ffb020"></i>положительный<s>теги сценария</s></div>
<div class="sock"><i style="background:#ff8a3d"></i>отрицательный<s>NEGATIVE</s></div>
<div class="sock"><i style="background:#ff2d95"></i>латентное_изображение<s id="sockLatent">—</s></div>
</div>
<div class="nrow"><span class="nl">seed</span>
<div class="nw"><button data-seed="-1">◀</button><input type="number" id="seed" min="0" max="9007199254740991" step="1"><button data-seed="1">▶</button></div>
<button class="dice" id="seedDice" title="Случайный seed">🎲</button></div>
<div class="nrow"><span class="nl">после генерации</span>
<div class="nw"><button data-cyc="seedMode" data-d="-1">◀</button><select id="seedMode">
<option value="randomize">randomize</option><option value="fixed">fixed</option>
<option value="increment">increment</option><option value="decrement">decrement</option></select>
<button data-cyc="seedMode" data-d="1">▶</button></div></div>
<div class="nrow"><span class="nl">название_сэмплера</span>
<div class="nw"><button data-cyc="sampler" data-d="-1">◀</button><select id="sampler"></select><button data-cyc="sampler" data-d="1">▶</button></div></div>
<div class="nrow"><span class="nl">scheduler</span>
<div class="nw"><button data-cyc="scheduler" data-d="-1">◀</button><select id="scheduler"></select><button data-cyc="scheduler" data-d="1">▶</button></div></div>
<div class="nrow"><span class="nl">шумоподавление</span>
<div class="nw"><button data-den="-0.05">◀</button><input type="number" id="denoise" min="0.05" max="1" step="0.05"><button data-den="0.05">▶</button></div></div>
<div class="rng" style="padding:0 10px"><label>шаги <b id="stepsV">45</b></label><input type="range" id="steps" min="4" max="150" step="1"></div>
<div class="rng" style="padding:0 10px"><label>cfg <b id="cfgV">5.5</b></label><input type="range" id="cfg" min="1" max="15" step="0.1"></div>
<div class="info" style="padding:0 10px 8px">denoise &lt; 1.00 осмыслен только с исходником; для генерации с нуля держи 1.00</div>
</div>
<div class="rng"><label>Картинок за раз <b id="maxV">1</b></label><input type="range" id="max" min="1" max="10" step="1"></div>
<div class="node">
<div class="nhead"><i style="background:var(--cyan)"></i>KSampler · hires</div>
<div class="nrow"><span class="nl">включён</span><label class="switch" style="flex:1"><input type="checkbox" id="hiresOn"><i></i><span>второй проход</span></label></div>
<div class="nrow"><span class="nl">сэмплер</span><div class="nw"><select id="hiresSampler"></select></div></div>
<div class="nrow"><span class="nl">scheduler</span><div class="nw"><select id="hiresScheduler"></select></div></div>
<div class="rng" style="padding:0 10px"><label>шаги <b id="hiresStepsV">20</b></label><input type="range" id="hiresSteps" min="4" max="80" step="1"></div>
<div class="rng" style="padding:0 10px"><label>cfg <b id="hiresCfgV">5.0</b></label><input type="range" id="hiresCfg" min="1" max="15" step="0.1"></div>
<div class="rng" style="padding:0 10px"><label>denoise <b id="hiresDenoiseV">0.40</b></label><input type="range" id="hiresDenoise" min="0.05" max="1" step="0.05"></div>
<div class="rng" style="padding:0 10px"><label>масштаб <b id="hiresScaleV">1.5</b></label><input type="range" id="hiresScale" min="1" max="2.5" step="0.05"></div>
</div></div>
<div class="sec"><div class="sech">Качество</div>
<label class="switch"><input type="checkbox" id="crit"><i></i><span>Критик: анатомия и замысел</span></label>
<label class="switch" style="margin-top:8px"><input type="checkbox" id="fb"><i></i><span>Оценка кадра в чат</span></label></div>
<div class="sec"><div class="sech">Детализация · лицо / глаза / руки</div>
<label class="switch"><input type="checkbox" id="detOn"><i></i><span>Перерисовывать области</span></label>
<div id="detailers" style="margin-top:6px"></div><div class="info" id="detInfo">—</div></div>
<div class="sec"><div class="sech">Исходник · режим 2</div>
<label class="switch"><input type="checkbox" id="srcI2I"><i></i><span>Исходник как img2img-основа</span></label>
<div class="rng"><label>Сила перерисовки <b id="srcDenoiseV">0.55</b></label>
<input type="range" id="srcDenoise" min="0.15" max="0.95" step="0.05"></div>
<label class="switch"><input type="checkbox" id="srcAutoSize"><i></i><span>Холст по пропорциям исходника</span></label></div>
<div class="sec"><div class="sech">LoRA · 10 слотов</div><div id="loras"></div></div>
</div>
<div class="railfoot">
<button class="primary" id="saveBtn">Сохранить сейчас</button>
<button class="ghost" id="refreshBtn">Обновить списки</button>
<button class="ghost" id="revertBtn">Отменить правки</button>
</div>
</aside>
</main>
<nav class="tabs" id="tabs">
<button data-v="chat" class="on">Диалог</button><button data-v="gallery">Серия</button><button data-v="rail">Параметры</button>
</nav>
<div class="lb" id="lb">
<button class="icobtn lbclose" id="lbClose">✕</button>
<button class="lbnav lbprev" id="lbPrev">‹</button><button class="lbnav lbnext" id="lbNext">›</button>
<div class="lbimg" id="lbWrap"><img id="lbImg" alt=""></div>
<div class="lbside"><h4>Кадр</h4><div class="lbmeta" id="lbMeta"></div>
<h4>Теги</h4><div class="lbtags" id="lbTags"></div>
<div class="lbacts"><button id="lbCopy">Копировать промпт</button><a id="lbDl" download>Скачать</a></div></div>
</div>
<div class="toasts" id="toasts"></div>
<script>
(function(){
'use strict';
var $=function(id){return document.getElementById(id);};
var log=$('log'),grid=$('grid'),inp=$('inp'),activeId=null;
try{activeId=localStorage.getItem('heretic.chat')||null;}catch(e){}
function node(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}
function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(function(r){return r.json();});}
function toast(msg,kind){var t=node('div','toast'+(kind?' '+kind:''),msg);$('toasts').appendChild(t);
setTimeout(function(){t.style.opacity='0';setTimeout(function(){t.remove();},300);},2400);}
function copy(text,label){if(navigator.clipboard){navigator.clipboard.writeText(text).then(function(){toast(label||'Скопировано');});}
else{var ta=node('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');toast(label||'Скопировано');}catch(e){}ta.remove();}}
var soundOn=false,actx=null;try{soundOn=localStorage.getItem('heretic.sound')==='1';}catch(e){}
function paintSound(){$('soundBtn').textContent=soundOn?'🔔':'🔕';$('soundBtn').classList.toggle('on',soundOn);}
function chime(){if(!soundOn)return;try{if(!actx)actx=new (window.AudioContext||window.webkitAudioContext)();
var t=actx.currentTime;[[880,0],[1318.5,.13]].forEach(function(p){var o=actx.createOscillator(),g=actx.createGain();
o.frequency.value=p[0];g.gain.setValueAtTime(0,t+p[1]);g.gain.linearRampToValueAtTime(.15,t+p[1]+.02);
g.gain.exponentialRampToValueAtTime(.0001,t+p[1]+.5);o.connect(g);g.connect(actx.destination);o.start(t+p[1]);o.stop(t+p[1]+.55);});}catch(e){}}
$('soundBtn').onclick=function(){soundOn=!soundOn;try{localStorage.setItem('heretic.sound',soundOn?'1':'0');}catch(e){}paintSound();if(soundOn)chime();};
paintSound();
/* лог */
var rendered=[],pinned=true,unread=0,busy=false,stage='idle',ideasOpen=false,lastMsgCount=0;
function toBottom(){log.scrollTop=log.scrollHeight;}
function updateJump(){$('jumpBtn').classList.toggle('show',!pinned&&unread>0);$('jumpN').textContent=unread;}
log.addEventListener('scroll',function(){var gap=log.scrollHeight-log.scrollTop-log.clientHeight;
pinned=gap<48;if(pinned)unread=0;updateJump();},{passive:true});
$('jumpBtn').onclick=function(){pinned=true;unread=0;updateJump();toBottom();};
function sameMsg(a,b){return a.role===b.role&&a.text===b.text&&JSON.stringify(a.images||[])===JSON.stringify(b.images||[]);}
function syncLog(msgs){
msgs=Array.isArray(msgs)?msgs:[];
if(msgs.length===rendered.length&&msgs.length&&log.lastChild&&log.lastChild._txt){
var grow=true;for(var j=0;j<msgs.length-1;j++){if(!sameMsg(rendered[j],msgs[j])){grow=false;break;}}
var a=rendered[msgs.length-1],b=msgs[msgs.length-1];
if(grow&&a&&b&&a.role===b.role&&b.text.indexOf(a.text)===0&&a.text!==b.text){
log.lastChild._txt.nodeValue=b.text;rendered=msgs.slice();if(pinned)toBottom();return;}}
var sig=JSON.stringify(msgs),old=JSON.stringify(rendered);
if(sig===old)return;
var n=rendered.length;log.innerHTML='';
msgs.forEach(function(m){log.appendChild(msgNode(m));});
rendered=msgs.slice();
if(pinned)toBottom();else if(msgs.length>n){unread+=msgs.length-n;updateJump();}
syncEmptyState();}
var WHO={u:'вы',b:'арт-директор',e:'ошибка',q:'уточнение',s:'сценарий'};
function msgNode(m){
if(m.role==='q'){var qs=parseQuestions(m.text);if(qs)return questionCard(qs);}
var d=node('div','m '+(m.role||'b'));
d.appendChild(node('span','who',WHO[m.role]||'система'));
d._txt=document.createTextNode(m.text||'');d.appendChild(d._txt);
if(m.images&&m.images.length){var th=node('div','thumbs');
m.images.forEach(function(r){var im=document.createElement('img');im.src='/uploads/'+encodeURIComponent(r.file);
im.loading='lazy';im.onclick=function(){window.open(im.src,'_blank');};th.appendChild(im);});d.appendChild(th);}
if(m.role==='s'){
var acts=node('div','sacts');
var go=node('button','send','🎬 Генерировать');go.onclick=function(){send('генерируй');};
var cp=node('button','mini','⧉ копировать');cp.onclick=function(){copy(m.text,'Сценарий скопирован');};
acts.appendChild(go);acts.appendChild(cp);
acts.appendChild(node('span','hint','напиши правки в чат — сценарий обновится'));
d.appendChild(acts);}
return d;}
function syncEmptyState(){
var empty=rendered.length===0;
if(rendered.length>lastMsgCount)ideasOpen=false;
lastMsgCount=rendered.length;
$('logEmpty').classList.toggle('show',empty||ideasOpen);
$('ideaToggle').classList.toggle('on',empty||ideasOpen);
$('ideaToggle').disabled=empty;}
$('ideaToggle').onclick=function(){ideasOpen=!ideasOpen;syncEmptyState();if(ideasOpen)renderIdeas();};
/* вопросы */
function optsFrom(line){var re=/(\d+)\)\s*/g,hits=[],mm;
while((mm=re.exec(line))!==null)hits.push({n:parseInt(mm[1],10),i:mm.index,e:re.lastIndex});
if(hits.length<2)return null;
var list=hits.map(function(h,k){var end=(k+1<hits.length)?hits[k+1].i:line.length;
return{n:h.n,text:line.slice(h.e,end).trim()};}).filter(function(o){return o.text;});
return list.length>=2?{opts:list,start:hits[0].i}:null;}
function parseQuestions(t){var lines=String(t||'').split(/\r?\n/),qs=[],cur=null;
lines.forEach(function(raw){var line=raw.trim();if(!line)return;
var mq=line.match(/^(\d+)\.\s+(.+)$/);
if(mq){var rest=mq[2],o=optsFrom(rest);
cur={n:parseInt(mq[1],10),q:(o?rest.slice(0,o.start):rest).trim(),options:o?o.opts:[]};qs.push(cur);return;}
var o2=optsFrom(line);if(o2&&cur&&!cur.options.length)cur.options=o2.opts;});
qs=qs.filter(function(q){return q.q;});return qs.length>=2?qs:null;}
function questionCard(qs){
var st={choices:{},text:{}};
var card=node('div','qcard'),head=node('div','qhead');
head.appendChild(node('b',null,'Уточнение · '+qs.length+' вопросов'));
head.appendChild(node('div','spacer'));
var solo=node('button','mini','Решу сам');head.appendChild(solo);card.appendChild(head);
var body=node('div','qbody'),prev=node('div','qprev'),cnt=node('span','cnt');
function buildAnswer(){var parts=[];qs.forEach(function(q){var c=st.choices[q.n];if(!c)return;
if(c.custom){var v=(st.text[q.n]||'').trim();if(v)parts.push(q.n+'-'+v);}else parts.push(q.n+'-'+c.n);});
return parts.join(', ');}
function refresh(){prev.textContent=buildAnswer()||'— выбери варианты кликом —';
cnt.textContent='отвечено '+Object.keys(st.choices).length+' / '+qs.length;}
qs.forEach(function(q){
var row=node('div','qrow'),qt=node('div','qtext');
qt.appendChild(node('em',null,q.n+'.'));qt.appendChild(document.createTextNode(q.q));row.appendChild(qt);
var box=node('div','opts');
q.options.forEach(function(o){
var b=node('button','opt');b.appendChild(node('em',null,o.n+')'));b.appendChild(document.createTextNode(o.text));
var cust=/свой вариант|своё|свое|другое/i.test(o.text),field=null;
if(cust){field=node('input','qcustom');field.placeholder='напиши свой вариант…';field.style.display='none';
field.oninput=function(){st.text[q.n]=field.value;refresh();};
field.onkeydown=function(e){e.stopPropagation();};}
b.onclick=function(){var cur=st.choices[q.n];
if(cur&&cur.n===o.n)delete st.choices[q.n];else st.choices[q.n]={n:o.n,custom:cust};
Array.prototype.forEach.call(box.children,function(x){if(x._paint)x._paint();});refresh();};
b._paint=function(){var sel=st.choices[q.n]&&st.choices[q.n].n===o.n;
b.classList.toggle('sel',!!sel);if(field)field.style.display=sel?'block':'none';};
box.appendChild(b);if(field)row.appendChild(field);});
row.appendChild(box);body.appendChild(row);});
card.appendChild(body);
var foot=node('div','qfoot');foot.appendChild(prev);
var acts=node('div','qacts'),go=node('button','send','Отправить ответы');
go.onclick=function(){send(buildAnswer()||'генерируй');};
acts.appendChild(go);acts.appendChild(cnt);foot.appendChild(acts);card.appendChild(foot);
solo.onclick=function(){send('генерируй');};
refresh();return card;}
/* исходники */
var atts=[],uploading=0;
function paintAtts(){var bar=$('attBar');bar.innerHTML='';
atts.forEach(function(a,i){var d=node('div','att'),im=document.createElement('img');im.src=a.url;d.appendChild(im);
var x=node('button','del','×');x.onclick=function(){atts.splice(i,1);paintAtts();};d.appendChild(x);bar.appendChild(d);});
$('attBtn').classList.toggle('on',atts.length>0);$('attCount').textContent=atts.length||'';
$('attIco').textContent=uploading?'⏳':'📎';}
async function uploadFiles(files){
var list=Array.prototype.filter.call(files||[],function(f){return /^image\//.test(f.type);});
for(var i=0;i<list.length;i++){var fd=new FormData();fd.append('file',list[i],list[i].name||'paste.png');
uploading++;paintAtts();
try{var j=await (await fetch('/api/upload',{method:'POST',body:fd})).json();
(j.files||[]).forEach(function(f){if(f.file)atts.push({file:f.file,url:f.url});else toast('Не принят: '+(f.error||'?'),'bad');});}
catch(e){toast('Загрузка не удалась','bad');}
finally{uploading--;paintAtts();}}}
$('attBtn').onclick=function(){$('attInput').click();};
$('attInput').onchange=function(){uploadFiles(this.files);this.value='';};
inp.addEventListener('paste',function(e){var f=e.clipboardData&&e.clipboardData.files;if(f&&f.length){e.preventDefault();uploadFiles(f);}});
['dragenter','dragover'].forEach(function(ev){$('chatPane').addEventListener(ev,function(e){e.preventDefault();$('chatPane').classList.add('drop');});});
['dragleave','drop'].forEach(function(ev){$('chatPane').addEventListener(ev,function(e){e.preventDefault();$('chatPane').classList.remove('drop');});});
$('chatPane').addEventListener('drop',function(e){if(e.dataTransfer&&e.dataTransfer.files)uploadFiles(e.dataTransfer.files);});
window.addEventListener('dragover',function(e){e.preventDefault();});
window.addEventListener('drop',function(e){e.preventDefault();});
/* режимы чата */
var chatMode='scene';
var PLACE={scene:'Опиши сцену — Qwen развернёт её в полный сценарий…',
source:'Прикрепи исходник (📎) и напиши, что поменять…',
prompt:'Английские теги через запятую — уйдут в Illustrious как есть…',
ask:'Опиши тему — сначала будут уточняющие вопросы…'};
function paintMode(){Array.prototype.forEach.call($('modeSeg').children,function(b){
b.classList.toggle('on',b.dataset.m===chatMode);});inp.placeholder=PLACE[chatMode]||PLACE.scene;}
function setMode(m){if(chatMode===m)return;chatMode=m;paintMode();
if(activeId)post('/api/mode',{id:activeId,mode:m});}
$('modeSeg').addEventListener('click',function(e){var b=e.target.closest('button');if(b)setMode(b.dataset.m);});
/* композер */
function autoGrow(){inp.style.height='auto';inp.style.height=Math.max(38,Math.min(inp.scrollHeight,220))+'px';}
function updateCount(){$('count').textContent=inp.value.length;}
inp.addEventListener('input',function(){autoGrow();updateCount();});
inp.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey&&!e.ctrlKey){e.preventDefault();send();}});
async function send(txt){
var t=(txt!=null?String(txt):inp.value).trim();
if(!t&&!atts.length)return;
if(busy){toast('Чат занят — дождись или открой другой','warn');return;}
if(!activeId){toast('Чат не выбран','bad');return;}
if(uploading){toast('Дождись загрузки файлов','warn');return;}
var imgs=atts.map(function(a){return{file:a.file};});
if(txt==null){inp.value='';autoGrow();updateCount();}
pinned=true;unread=0;updateJump();
try{var j=await post('/api/chat',{id:activeId,text:t,images:imgs,mode:chatMode});
if(!j.ok)toast('Не принято: идёт генерация','warn');
else{atts=[];paintAtts();tick();}}catch(e){toast('Нет связи с мостом','bad');}}
$('sendBtn').onclick=function(){send();};
$('stopBtn').onclick=async function(){try{await post('/api/stop',{id:activeId});toast('Останавливаю','warn');}catch(e){}};
$('resetBtn').onclick=async function(){try{var j=await post('/api/reset',{id:activeId});
if(j.ok){rendered=[];log.innerHTML='';items=[];galSig='';grid.innerHTML='';atts=[];paintAtts();tick();toast('Чат очищен');}}catch(e){}};
$('newBtn').onclick=async function(){try{await post('/api/new',{id:activeId});toast('Новый сюжет');tick();}catch(e){}};
/* чаты */
function clearChatView(){rendered=[];log.innerHTML='';items=[];galSig='';galN=0;grid.innerHTML='';
atts=[];paintAtts();pinned=true;unread=0;updateJump();wasBusy=false;lastMsgCount=0;}
function switchChat(id){activeId=id;try{localStorage.setItem('heretic.chat',id);}catch(e){}clearChatView();tick();}
async function newChat(){try{var j=await post('/api/chats/create',{});if(j.ok){switchChat(j.id);toast('Новый чат');}}catch(e){}}
var delArm=null,delArmT=0,lastTabs=[];
function disarmDel(){delArm=null;clearTimeout(delArmT);}
function repaintTabs(){tabsSig='';renderTabs(lastTabs);}
async function delChat(c,force){
if(!force&&delArm!==c.id){
delArm=c.id;repaintTabs();
clearTimeout(delArmT);
delArmT=setTimeout(function(){delArm=null;repaintTabs();},3000);
toast('Ещё один клик по «точно?» — чат удалится','warn');return;}
disarmDel();
try{var j=await post('/api/chats/delete',{id:c.id});
if(j.ok){if(c.id===activeId){activeId=null;clearChatView();}tick();toast('Чат удалён');}
else toast('Сервер не нашёл этот чат — перезагрузи страницу','bad');}
catch(e){toast('Нет связи с мостом','bad');repaintTabs();}}
async function renameChat(c){var t=prompt('Название чата:',c.title||'');if(!t||!t.trim())return;
try{await post('/api/chats/rename',{id:c.id,title:t.trim()});tick();}catch(e){}}
var tabsSig='',tabsN=0;
function renderTabs(list){list=list||[];tabsN=list.length;lastTabs=list;
var sig=list.map(function(c){return c.id+'|'+c.title+'|'+(c.busy?1:0);}).join('~')+'#'+activeId+'#'+(delArm||'');
if(sig===tabsSig)return;tabsSig=sig;
var box=$('chatTabs');box.innerHTML='';
list.forEach(function(c){var b=node('button','ctab'+(c.id===activeId?' on':''));b.title=c.title;
if(c.busy&&c.id!==activeId)b.appendChild(node('i','bdot'));
b.appendChild(node('span',null,c.title||'чат'));
var armed=delArm===c.id;
var x=node('em',armed?'arm':null,armed?'точно?':'×');
x.onclick=function(e){e.stopPropagation();delChat(c,armed);};b.appendChild(x);
b.onclick=function(){if(c.id!==activeId)switchChat(c.id);};
b.ondblclick=function(){renameChat(c);};box.appendChild(b);});
var add=node('button','ctab add','+');add.onclick=newChat;box.appendChild(add);
$('chatMore').textContent=($('chatTabs').classList.contains('open')?'▴':'▾')+' '+tabsN;}
$('chatMore').onclick=function(){$('chatTabs').classList.toggle('open');tabsSig='';};
/* идеи */
var IDEAS={{IDEAS}};
var ideaMode='real',regenBusy=false,ideasVer=-1;
try{var _m=localStorage.getItem('heretic.ideaMode');if(_m==='real'||_m==='fantasy')ideaMode=_m;}catch(e){}
function paintIdeaMode(){Array.prototype.forEach.call($('ideaMode').children,function(b){
b.classList.toggle('on',b.dataset.m===ideaMode);});}
function renderIdeas(){
var box=$('seedIdeas');box.innerHTML='';
var list=IDEAS[ideaMode]||[];
list.forEach(function(it,i){
var row=node('div','idea');row.appendChild(node('span','n',('0'+(i+1)).slice(-2)));
var body=node('button','body',it.short);body.title='Вставить полный сценарий в поле ввода';
body.onclick=function(){inp.value=it.full;autoGrow();updateCount();
setMode('scene');ideasOpen=false;syncEmptyState();inp.focus();
toast('Полный сценарий в поле — правь и отправляй');};
var acts=node('div','acts'),cp=node('button',null,'⧉');cp.title='Копировать полный сценарий';
cp.onclick=function(){copy(it.full,'Сценарий скопирован');};
acts.appendChild(cp);row.appendChild(body);row.appendChild(acts);box.appendChild(row);});
$('ideaCount').textContent=list.length?(list.length+' сцен · '+ideaMode):'идей нет — нажми «новые идеи»';}
$('ideaMode').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
ideaMode=b.dataset.m;try{localStorage.setItem('heretic.ideaMode',ideaMode);}catch(e){}paintIdeaMode();renderIdeas();});
$('regenIdeasBtn').onclick=async function(){
if(regenBusy){toast('Уже генерирую…','warn');return;}
regenBusy=true;var btn=this,html=btn.innerHTML;btn.disabled=true;btn.innerHTML='◌ генерирую…';
try{var j=await post('/api/ideas/regen',{mode:ideaMode});
if(j.ok&&j.ideas&&j.ideas.length){IDEAS[ideaMode]=j.ideas;renderIdeas();toast('Новые идеи: '+j.ideas.length);}
else toast('Не удалось: '+(j.error||'пусто'),'bad');}
catch(e){toast('Нет связи','bad');}
finally{btn.disabled=false;btn.innerHTML=html;regenBusy=false;}};
paintIdeaMode();renderIdeas();syncEmptyState();
/* галерея */
var items=[],filter='all',galSig='',lbIndex=-1,galN=0;
var FLABEL={all:'все',wip:'в работе',ok:'готово',bad:'брак'};
var MLABEL={scene:'сюжет',source:'исходник',prompt:'промт',ask:'вопросы'};
function statusKind(s){s=String(s||'');if(s==='готово')return'ok';if(s==='брак')return'bad';if(s==='ретрай')return'retry';return'wip';}
function tagList(p){return String(p||'').split(',').map(function(x){return x.trim();}).filter(Boolean);}
function passFilter(it){var k=statusKind(it.status);if(filter==='all')return true;
if(filter==='wip')return k==='wip'||k==='retry';return k===filter;}
function syncGallery(arr){
items=Array.isArray(arr)?arr:[];
$('galCount').textContent=items.length;
var cnt={all:items.length,wip:0,ok:0,bad:0};
items.forEach(function(it){var k=statusKind(it.status);if(k==='retry')k='wip';cnt[k]++;});
Array.prototype.forEach.call($('filters').children,function(b){b.textContent=FLABEL[b.dataset.f]+' '+cnt[b.dataset.f];});
$('galEmpty').classList.toggle('show',items.filter(passFilter).length===0);
var sig=JSON.stringify(items.map(function(it){var c={};for(var k in it)if(k!=='progress')c[k]=it[k];return c;}))+'|'+filter;
if(sig!==galSig){galSig=sig;var top=grid.scrollTop,grew=items.length>galN;galN=items.length;
grid.innerHTML='';items.forEach(function(it,i){if(passFilter(it))grid.appendChild(card(it,i));});
grid.scrollTop=grew?grid.scrollHeight:top;}
updateProgress();}
function updateProgress(){items.forEach(function(it){
var c=grid.querySelector('.card[data-idx="'+it.idx+'"]');if(!c||!it.progress)return;
var pr=it.progress,lbl=c.querySelector('.lbl'),bar=c.querySelector('.pbar i');
if(lbl)lbl.textContent=(pr.stage||'')+(pr.max?(' · '+pr.value+'/'+pr.max+' · '+pr.pct+'%'):'');
if(bar)bar.style.width=(pr.pct||0)+'%';});}
function card(it,i){
var c=node('div','card');c.dataset.idx=it.idx;
var head=node('div','chead');
head.appendChild(node('span','badge '+statusKind(it.status),'#'+it.idx+' · '+(it.status||'')));
head.appendChild(node('div','spacer'));
if(it.mode)head.appendChild(node('span','pill',MLABEL[it.mode]||it.mode));
var sd=node('button','seed','seed '+it.seed);sd.onclick=function(){copy(String(it.seed),'Seed скопирован');};
head.appendChild(sd);c.appendChild(head);
var shot=node('div','shot');
if(it.file){var img=document.createElement('img');img.src='/img/'+encodeURIComponent(it.file);
img.loading='lazy';img.onclick=function(){openLb(i);};shot.appendChild(img);}
else{shot.appendChild(node('div','skel'));shot.appendChild(node('div','lbl',it.status||'ожидание'));
var pb=node('div','pbar');pb.appendChild(document.createElement('i'));shot.appendChild(pb);}
c.appendChild(shot);
if(it.src&&it.src.length){var sr=node('div','srcs');sr.appendChild(node('s',null,'исходник'));
it.src.forEach(function(f){var im=document.createElement('img');im.src='/uploads/'+encodeURIComponent(f);
im.onclick=function(){window.open(im.src,'_blank');};sr.appendChild(im);});c.appendChild(sr);}
if(it.verdict)c.appendChild(node('div','verdict '+(it.ok===false?'bad':(it.ok?'good':'')),it.verdict));
if(it.scene){var ds=document.createElement('details');ds.className='scene';
var sm=document.createElement('summary');sm.textContent='сценарий · что и зачем генерировали';ds.appendChild(sm);
ds.appendChild(node('div','scenebox',it.scene));c.appendChild(ds);}
var tags=tagList(it.prompt);
if(tags.length){var det=document.createElement('details');det.className='tags';
var sum=document.createElement('summary');sum.textContent='теги · '+tags.length;det.appendChild(sum);
var box=node('div','tagbox');
tags.forEach(function(t){var b=node('button','tag',t);b.onclick=function(){copy(t,'Тег скопирован');};box.appendChild(b);});
det.appendChild(box);c.appendChild(det);}
var acts=node('div','cacts'),open=node('button',null,'Открыть');
open.disabled=!it.file;open.onclick=function(){openLb(i);};
var cp=node('button',null,'Копировать');cp.onclick=function(){copy(it.prompt||'','Промпт скопирован');};
acts.appendChild(open);acts.appendChild(cp);
if(it.file){var a=document.createElement('a');a.textContent='Скачать';
a.href='/img/'+encodeURIComponent(it.file);a.setAttribute('download',it.file);acts.appendChild(a);}
c.appendChild(acts);return c;}
$('filters').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
filter=b.dataset.f;Array.prototype.forEach.call($('filters').children,function(x){x.classList.toggle('on',x===b);});
galSig='';syncGallery(items);});
/* лайтбокс */
function withFile(){return items.map(function(it,i){return i;}).filter(function(i){return !!items[i].file;});}
function openLb(i){if(!items[i]||!items[i].file)return;lbIndex=i;paintLb();$('lb').classList.add('show');}
function paintLb(){var it=items[lbIndex];if(!it)return;
var url='/img/'+encodeURIComponent(it.file),im=$('lbImg');
im.className='';$('lbWrap').className='lbimg';im.src=url;
$('lbMeta').textContent='#'+it.idx+' · '+(it.status||'')+'\nseed '+it.seed+'\n'+it.file;
$('lbDl').href=url;$('lbDl').setAttribute('download',it.file);
var box=$('lbTags');box.innerHTML='';
tagList(it.prompt).forEach(function(t){var b=node('button','tag',t);b.onclick=function(){copy(t,'Тег скопирован');};box.appendChild(b);});}
function closeLb(){$('lb').classList.remove('show');lbIndex=-1;}
function step(d){var list=withFile();if(!list.length)return;
var pos=list.indexOf(lbIndex);if(pos<0)pos=0;
pos=(pos+d+list.length)%list.length;lbIndex=list[pos];paintLb();}
$('lbClose').onclick=closeLb;$('lbPrev').onclick=function(){step(-1);};$('lbNext').onclick=function(){step(1);};
$('lbCopy').onclick=function(){if(items[lbIndex])copy(items[lbIndex].prompt||'','Промпт скопирован');};
$('lbWrap').onclick=function(e){if(e.target===$('lbWrap'))closeLb();};
$('lbImg').onclick=function(){var z=$('lbImg').classList.toggle('zoom');$('lbWrap').classList.toggle('wrap',z);};
document.addEventListener('keydown',function(e){
if($('lb').classList.contains('show')){
if(e.key==='Escape')closeLb();else if(e.key==='ArrowLeft')step(-1);else if(e.key==='ArrowRight')step(1);}});
/* настройки */
var CFG={},ASSETS={},RESLIST=[],saveTimer=null,loaded=false;
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function optsHtml(arr,val,empty){arr=Array.isArray(arr)?arr:[];
var h='<option value="">'+esc(empty)+'</option>';
if(val&&arr.indexOf(val)<0)h+='<option value="'+esc(val)+'" selected>'+esc(val)+' [нет в ComfyUI]</option>';
return h+arr.map(function(n){return '<option value="'+esc(n)+'"'+(n===val?' selected':'')+'>'+esc(n)+'</option>';}).join('');}
function setSel(id,arr,val,empty){$(id).innerHTML=optsHtml(arr,val,empty);}
function paintSockets(){
var m=$('sockModel');if(m)m.textContent=(CFG.ckpt||'—').replace(/\.safetensors$/,'');
var l=$('sockLatent');if(!l)return;
var wh=null,k=$('res').value;
if(k==='custom'){wh=[parseInt($('cw').value,10)||1408,parseInt($('ch').value,10)||704];}
else{var mm=String(k||'').match(/(\d+)\s*x\s*(\d+)/);if(mm)wh=[parseInt(mm[1],10),parseInt(mm[2],10)];}
l.textContent=$('srcI2I').checked?'VAEEncode · исходник (режим 2)':('EmptyLatent '+(wh?wh[0]+'×'+wh[1]:''));}
function applyCfg(c){c=c||{};
setSel('ckpt',ASSETS.ckpts,c.ckpt,'— выбери чекпоинт —');
var rs=$('res'),list=RESLIST.length?RESLIST:(c.resolution?[c.resolution]:[]);
rs.innerHTML=list.map(function(k){var lab=k==='custom'?'Свой размер…':k;
return '<option value="'+esc(k)+'"'+(k===c.resolution?' selected':'')+'>'+esc(lab)+'</option>';}).join('');
var cwv=(c.custom_wh&&c.custom_wh.length===2)?c.custom_wh:[1408,704];
$('cw').value=cwv[0];$('ch').value=cwv[1];
$('cwRow').style.display=c.resolution==='custom'?'grid':'none';
var ff=(c.fw_fh&&c.fw_fh.length===2)?c.fw_fh:[2000,1000];
$('fw').value=ff[0];$('fh').value=ff[1];
$('steps').value=c.steps==null?45:c.steps;$('stepsV').textContent=$('steps').value;
$('cfg').value=c.cfg==null?5.5:c.cfg;$('cfgV').textContent=Number($('cfg').value).toFixed(1);
$('max').value=c.max_images==null?1:c.max_images;$('maxV').textContent=$('max').value;
$('seed').value=(c.seed==null?0:c.seed);
$('seedMode').value=c.seed_mode||'randomize';
setSel('sampler',ASSETS.samplers,c.sampler||'dpmpp_2m','— сэмплер —');
setSel('scheduler',ASSETS.schedulers,c.scheduler||'karras','— scheduler —');
$('denoise').value=(c.denoise==null?1:c.denoise);
$('hiresOn').checked=c.hires_on!==false;
setSel('hiresSampler',ASSETS.samplers,c.hires_sampler||'dpmpp_2m_sde','— сэмплер —');
setSel('hiresScheduler',ASSETS.schedulers,c.hires_scheduler||'karras','— scheduler —');
$('hiresSteps').value=c.hires_steps==null?20:c.hires_steps;$('hiresStepsV').textContent=$('hiresSteps').value;
$('hiresCfg').value=c.hires_cfg==null?5:c.hires_cfg;$('hiresCfgV').textContent=Number($('hiresCfg').value).toFixed(1);
$('hiresDenoise').value=c.hires_denoise==null?0.4:c.hires_denoise;$('hiresDenoiseV').textContent=Number($('hiresDenoise').value).toFixed(2);
$('hiresScale').value=c.hires_scale==null?1.5:c.hires_scale;$('hiresScaleV').textContent=Number($('hiresScale').value).toFixed(2);
paintSockets();
setSel('up',ASSETS.upscales,c.upscaler||'','— без апскейла —');
$('crit').checked=!!c.use_critic;$('fb').checked=c.feedback_on!==false;
$('tagModel').value=c.tag_model||'TIPO-200M-ft.Q8_0.gguf';
$('tagUrl').value=c.tag_url||'http://127.0.0.1:8080';
$('tagFallback').checked=c.tag_fallback!==false;
$('tipoExe').value=c.tipo_exe||'';$('tipoModelPath').value=c.tipo_model_path||'';
$('tipoDevice').value=(c.tipo_device==null?'0':String(c.tipo_device));
$('tipoAuto').checked=c.tipo_autostart!==false;
$('tipoOn').checked=c.tipo_on!==false;
$('tagInfo').textContent='TIPO: '+(ASSETS.tipo?'llama-server отвечает':'НЕТ — проверь пути и tipo_server.log');
$('srcI2I').checked=c.src_img2img!==false;
$('srcDenoise').value=c.src_denoise==null?0.55:c.src_denoise;
$('srcDenoiseV').textContent=Number($('srcDenoise').value).toFixed(2);
$('srcAutoSize').checked=c.src_auto_size!==false;
loraRows(c.loras);$('detOn').checked=c.detail_on!==false;detailRows(c.detailers);
$('assetsInfo').textContent='ComfyUI: чекпоинтов '+(ASSETS.ckpts||[]).length+' · LoRA '+(ASSETS.loras||[]).length+
' · апскейлеров '+(ASSETS.upscales||[]).length;}
function loraRows(list){var L=$('loras');L.innerHTML='';
var arr=(Array.isArray(list)?list:[]).slice(0,10);
while(arr.length<10)arr.push({name:'',strength:0.8,on:false});
arr.forEach(function(l,i){l=l||{};
var row=node('div','lrow'+(l.on?'':' off')),top=node('div','ltop');
var sw=document.createElement('label');sw.className='switch';
var cb=document.createElement('input');cb.type='checkbox';cb.checked=!!l.on;
sw.appendChild(cb);sw.appendChild(document.createElement('i'));
var sel=document.createElement('select');sel.innerHTML=optsHtml(ASSETS.loras,l.name||'','— нет —');
var num=node('span','num','#'+(i+1));
top.appendChild(sw);top.appendChild(sel);top.appendChild(num);
var rng=document.createElement('input');rng.type='range';rng.min='0';rng.max='2';rng.step='0.05';
rng.value=l.strength==null?0.8:l.strength;num.textContent=Number(rng.value).toFixed(2);
cb.onchange=function(){row.classList.toggle('off',!cb.checked);markDirty();};
sel.onchange=markDirty;
rng.oninput=function(){num.textContent=Number(rng.value).toFixed(2);markDirty();};
row.appendChild(top);row.appendChild(rng);L.appendChild(row);});}
var DET_KEYS=[['face','лицо'],['eyes','глаза'],['hands','руки'],['custom','свой']];
function detailRows(list){var box=$('detailers');box.innerHTML='';
var saved={};(Array.isArray(list)?list:[]).forEach(function(d){if(d&&d.key)saved[d.key]=d;});
DET_KEYS.forEach(function(pair){var key=pair[0],d=saved[key]||{},on=(key==='custom')?!!d.on:(d.on!==false);
var row=node('div','drow'+(on?'':' off'));row.dataset.k=key;
var top=node('div','dtop'),sw=document.createElement('label');sw.className='switch';
var cb=document.createElement('input');cb.type='checkbox';cb.checked=on;
sw.appendChild(cb);sw.appendChild(document.createElement('i'));top.appendChild(sw);
top.appendChild(node('span','dn',pair[1]));
var sel=document.createElement('select');
sel.innerHTML=optsHtml(ASSETS.detectors,d.model||'',key==='custom'?'— модель —':'— авто —');
top.appendChild(sel);row.appendChild(top);
var wrap=node('div','rng'),lab=document.createElement('label'),dv=d.denoise==null?0.35:d.denoise;
var bv=node('b',null,Number(dv).toFixed(2));
lab.appendChild(document.createTextNode('перерисовка '));lab.appendChild(bv);
var rng=document.createElement('input');rng.type='range';rng.min='0.05';rng.max='1';rng.step='0.05';rng.value=dv;
wrap.appendChild(lab);wrap.appendChild(rng);row.appendChild(wrap);
var nums=node('div','dnums');
[['steps','шаги',1,60,1,d.steps==null?20:d.steps],['cfg','cfg',1,12,0.5,d.cfg==null?5:d.cfg],
['guide','px',256,1024,64,d.guide==null?512:d.guide]].forEach(function(p){
var cell=node('div');cell.appendChild(node('label',null,p[1]));
var i2=document.createElement('input');i2.type='number';i2.min=p[2];i2.max=p[3];i2.step=p[4];i2.value=p[5];i2.dataset.f=p[0];
i2.oninput=markDirty;cell.appendChild(i2);nums.appendChild(cell);});
row.appendChild(nums);
cb.onchange=function(){row.classList.toggle('off',!cb.checked);markDirty();};
sel.onchange=markDirty;
rng.oninput=function(){bv.textContent=Number(rng.value).toFixed(2);markDirty();};
box.appendChild(row);});
var n=(ASSETS.detectors||[]).length;
$('detInfo').textContent=n?('детекторов: '+n+(ASSETS.impact?'':' · нет FaceDetailer')):'детекторов нет · нужен Impact-Pack';}
function readUI(){
var rows=Array.prototype.map.call($('loras').querySelectorAll('.lrow'),function(r){
return{on:r.querySelector('input[type=checkbox]').checked,name:r.querySelector('select').value,
strength:parseFloat(r.querySelector('input[type=range]').value)||0};});
var dets=Array.prototype.map.call($('detailers').querySelectorAll('.drow'),function(r){
var o={key:r.dataset.k,on:r.querySelector('input[type=checkbox]').checked,
model:r.querySelector('select').value,denoise:parseFloat(r.querySelector('input[type=range]').value)||0.35};
Array.prototype.forEach.call(r.querySelectorAll('.dnums input'),function(i){o[i.dataset.f]=parseFloat(i.value)||0;});
return o;});
return{ckpt:$('ckpt').value,resolution:$('res').value,
custom_wh:[parseInt($('cw').value,10)||1408,parseInt($('ch').value,10)||704],
fw_fh:[parseInt($('fw').value,10)||2000,parseInt($('fh').value,10)||1000],
steps:parseInt($('steps').value,10)||45,cfg:parseFloat($('cfg').value)||5.5,
max_images:parseInt($('max').value,10)||1,upscaler:$('up').value,
seed:parseInt($('seed').value,10)||0,seed_mode:$('seedMode').value,
sampler:$('sampler').value,scheduler:$('scheduler').value,
denoise:parseFloat($('denoise').value)||1,
hires_on:$('hiresOn').checked,hires_sampler:$('hiresSampler').value,hires_scheduler:$('hiresScheduler').value,
hires_steps:parseInt($('hiresSteps').value,10)||20,hires_cfg:parseFloat($('hiresCfg').value)||5,
hires_denoise:parseFloat($('hiresDenoise').value)||0.4,hires_scale:parseFloat($('hiresScale').value)||1.5,
use_critic:$('crit').checked,feedback_on:$('fb').checked,
tag_model:$('tagModel').value.trim()||'TIPO-200M-ft.Q8_0.gguf',
tag_url:$('tagUrl').value.trim()||'http://127.0.0.1:8080',
tag_fallback:$('tagFallback').checked,
tipo_exe:$('tipoExe').value.trim(),tipo_model_path:$('tipoModelPath').value.trim(),
tipo_device:$('tipoDevice').value.trim()||'0',tipo_autostart:$('tipoAuto').checked,tipo_on:$('tipoOn').checked,
src_img2img:$('srcI2I').checked,src_denoise:parseFloat($('srcDenoise').value)||0.55,
src_auto_size:$('srcAutoSize').checked,
loras:rows,detail_on:$('detOn').checked,detailers:dets};}
function setSaveState(kind){var e=$('saveState');
e.className='pill'+(kind==='dirty'?' dirty':(kind==='live'?' live':''));
e.textContent=kind==='dirty'?'не сохранено':(kind==='err'?'ошибка':'сохранено');}
function markDirty(){if(!loaded)return;setSaveState('dirty');
clearTimeout(saveTimer);saveTimer=setTimeout(function(){save(true);},650);}
async function save(silent){clearTimeout(saveTimer);var c=readUI();
try{var r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});
if(!r.ok)throw new Error('HTTP '+r.status);
CFG=c;setSaveState('live');if(!silent)toast('Настройки сохранены');}
catch(e){setSaveState('err');if(!silent)toast('Не сохранено: '+e,'bad');}}
async function loadCfg(force){
try{var j=await (await fetch('/api/config'+(force?'?refresh=1':''),{cache:'no-store'})).json();
CFG=j.config||{};ASSETS=j.assets||{};RESLIST=j.resolutions||[];
applyCfg(CFG);loaded=true;setSaveState('live');
if(force)toast('Списки обновлены из ComfyUI');}
catch(e){$('assetsInfo').textContent='нет связи с мостом';}}
$('saveBtn').onclick=function(){save(false);};
$('refreshBtn').onclick=function(){loadCfg(true);};
$('revertBtn').onclick=function(){applyCfg(CFG);setSaveState('live');toast('Правки отменены');};
$('presets').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
var p=b.dataset.p;
if(p==='draft'){$('steps').value=20;$('cfg').value=5;$('up').value='';$('crit').checked=false;}
if(p==='final'){$('steps').value=45;$('cfg').value=5.5;if((ASSETS.upscales||[]).length)$('up').value=ASSETS.upscales[0];$('crit').checked=true;}
if(p==='max'){$('steps').value=64;$('cfg').value=6;if((ASSETS.upscales||[]).length)$('up').value=ASSETS.upscales[0];$('crit').checked=true;}
$('stepsV').textContent=$('steps').value;$('cfgV').textContent=Number($('cfg').value).toFixed(1);
markDirty();toast('Пресет применён');});
['steps','cfg','max'].forEach(function(id){$(id).addEventListener('input',function(){
$(id+'V').textContent=(id==='cfg')?Number($(id).value).toFixed(1):$(id).value;markDirty();});});
['ckpt','up','crit','fb','srcI2I','srcAutoSize','tagFallback','tipoAuto','tipoOn'].forEach(function(id){$(id).addEventListener('change',markDirty);});
['tagModel','tagUrl','tipoExe','tipoModelPath','tipoDevice','cw','ch','fw','fh'].forEach(function(id){$(id).addEventListener('input',markDirty);});
$('res').addEventListener('change',function(){
$('cwRow').style.display=$('res').value==='custom'?'grid':'none';paintSockets();markDirty();});
$('srcDenoise').addEventListener('input',function(){$('srcDenoiseV').textContent=Number(this.value).toFixed(2);markDirty();});
$('detOn').addEventListener('change',markDirty);
$('seedDice').onclick=function(){$('seed').value=Math.floor(Math.random()*4294967295)+1;markDirty();};
$('rail').addEventListener('click',function(e){
var b=e.target.closest('button');if(!b)return;
if(b.dataset.seed!=null){var el=$('seed');el.value=Math.max(0,(parseInt(el.value,10)||0)+parseInt(b.dataset.seed,10));markDirty();return;}
if(b.dataset.den!=null){var el2=$('denoise');el2.value=Math.max(0.05,Math.min(1,(parseFloat(el2.value)||1)+parseFloat(b.dataset.den))).toFixed(2);markDirty();return;}
if(b.dataset.cyc){var sel=$(b.dataset.cyc);if(!sel||!sel.options.length)return;
sel.selectedIndex=(sel.selectedIndex+parseInt(b.dataset.d,10)+sel.options.length)%sel.options.length;markDirty();}});
['seed','denoise'].forEach(function(id){$(id).addEventListener('input',markDirty);});
['seedMode','sampler','scheduler','hiresSampler','hiresScheduler','hiresOn'].forEach(function(id){$(id).addEventListener('change',markDirty);});
[['hiresSteps','hiresStepsV',0],['hiresCfg','hiresCfgV',1],['hiresDenoise','hiresDenoiseV',2],['hiresScale','hiresScaleV',2]].forEach(function(p){
$(p[0]).addEventListener('input',function(){$(p[1]).textContent=Number(this.value).toFixed(p[2]);markDirty();});});
['cw','ch'].forEach(function(id){$(id).addEventListener('input',paintSockets);});
$('srcI2I').addEventListener('change',paintSockets);
Array.prototype.forEach.call(document.querySelectorAll('.stp button'),function(b){
b.onclick=function(){var t=$(b.dataset.t),d=parseInt(b.dataset.d,10);
t.value=Math.max(256,(parseInt(t.value,10)||1024)+d);markDirty();};});
/* ширина чата */
var grip=$('grip'),chatPane=$('chatPane');
function setChatW(w){var max=Math.max(320,Math.min(760,window.innerWidth-560));
w=Math.max(300,Math.min(Math.round(w),max));
document.documentElement.style.setProperty('--chat',w+'px');chatPane.style.width=w+'px';
try{localStorage.setItem('heretic.chatW',String(w));}catch(e){}}
try{var sw=parseInt(localStorage.getItem('heretic.chatW'),10);if(sw)setChatW(sw);}catch(e){}
grip.addEventListener('pointerdown',function(e){grip.setPointerCapture(e.pointerId);grip.classList.add('act');
document.body.classList.add('dragging');e.preventDefault();});
grip.addEventListener('pointermove',function(e){if(grip.classList.contains('act'))
setChatW(e.clientX-chatPane.getBoundingClientRect().left);});
function endDrag(e){if(!grip.classList.contains('act'))return;
grip.classList.remove('act');document.body.classList.remove('dragging');
try{grip.releasePointerCapture(e.pointerId);}catch(err){}}
grip.addEventListener('pointerup',endDrag);grip.addEventListener('pointercancel',endDrag);
grip.addEventListener('dblclick',function(){setChatW(470);});
/* рейл / табы */
function railIsOpen(){var w=window.innerWidth;
if(w<=900)return $('panes').dataset.view==='rail';
if(w<=1240)return document.body.classList.contains('rail-open');
return !document.body.classList.contains('rail-hide');}
function railSet(open){var w=window.innerWidth;
if(w<=900){$('panes').dataset.view=open?'rail':'chat';
Array.prototype.forEach.call($('tabs').children,function(x){x.classList.toggle('on',x.dataset.v===(open?'rail':'chat'));});return;}
if(w<=1240){document.body.classList.toggle('rail-open',open);return;}
document.body.classList.toggle('rail-hide',!open);}
$('railBtn').onclick=function(){railSet(!railIsOpen());};
$('railClose').onclick=function(){railSet(false);};
$('tabs').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
$('panes').dataset.view=b.dataset.v;
Array.prototype.forEach.call($('tabs').children,function(x){x.classList.toggle('on',x===b);});});
/* телеметрия */
function shortCpu(s){return String(s||'CPU').replace(/\(R\)|\(TM\)|CPU|Processor|\d+-Core/ig,'').replace(/@.*$/,'').replace(/\s+/g,' ').trim()||'CPU';}
function shortGpu(s){return String(s||'GPU').replace(/^NVIDIA\s+(GeForce\s+)?/i,'').trim()||'GPU';}
function tCls(v){return v==null?'':(v<62?'t-ok':(v<78?'t-warn':'t-bad'));}
function gb(v){return (v==null||isNaN(v))?'н/д':(Number(v)/1024).toFixed(1);}
function chipEl(title,line,pct,hot){var c=node('div','chip'+(hot?' hot':''));
c.appendChild(node('b',null,title));var s=document.createElement('s');s.innerHTML=line;c.appendChild(s);
var u=document.createElement('u');u.style.width=Math.max(0,Math.min(100,pct||0))+'%';c.appendChild(u);return c;}
async function hwTick(){var box=$('hw');
try{var h=await (await fetch('/api/hw',{cache:'no-store'})).json();box.innerHTML='';
var parts=[];
if(h.cpu_temp!=null)parts.push('<span class="'+tCls(h.cpu_temp)+'">'+Math.round(h.cpu_temp)+'°C</span>');
if(h.cpu_load!=null)parts.push(Math.round(h.cpu_load)+'%');
if(h.cpu_power!=null)parts.push(Math.round(h.cpu_power)+' Вт');
if(h.cpu_temp==null&&h.cpu_power==null)parts.push('LHM выкл');
box.appendChild(chipEl(shortCpu(h.cpu),parts.join(' · ')||'н/д',h.cpu_load||0,h.cpu_temp>=78));
if(h.ram){var p=h.ram.total?h.ram.used/h.ram.total*100:0;
box.appendChild(chipEl('RAM',Number(h.ram.used).toFixed(1)+' / '+Number(h.ram.total).toFixed(1)+' ГБ · '+Math.round(p)+'%',p,p>85));}
(h.gpus||[]).forEach(function(g,i){var vp=g.mem_total?g.mem_used/g.mem_total*100:0;
var l1='<span class="'+tCls(g.temp)+'">'+(g.temp==null?'н/д':Math.round(g.temp)+'°C')+'</span> · '+
(g.util!=null?Math.round(g.util)+'%':'н/д');
if(g.power!=null)l1+=' · '+Math.round(g.power)+' Вт';
box.appendChild(chipEl('GPU'+i+' '+shortGpu(g.name),l1+'<br>'+gb(g.mem_used)+' / '+gb(g.mem_total)+' ГБ',
Math.max(vp,g.util||0),g.temp>=78));});
if(!(h.gpus||[]).length)box.appendChild(chipEl('GPU','nvidia-smi не ответил',0,true));}
catch(e){box.innerHTML='';box.appendChild(chipEl('Телеметрия','нет ответа',0,true));}}
/* опрос */
var ticking=false,wasBusy=false;
async function tick(){if(ticking)return;ticking=true;
try{var s=await (await fetch('/api/state?id='+encodeURIComponent(activeId||''),{cache:'no-store'})).json();
var ids=(s.chats||[]).map(function(c){return c.id;});
if(!activeId||ids.indexOf(activeId)<0){activeId=s.id;
try{localStorage.setItem('heretic.chat',activeId);}catch(e){}clearChatView();}
renderTabs(s.chats);
if(s.ideas_ver!=null&&s.ideas_ver!==ideasVer){ideasVer=s.ideas_ver;
fetch('/api/ideas',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){IDEAS=d;renderIdeas();}).catch(function(){});}
busy=!!s.busy;stage=s.stage||'idle';
if(s.seed!=null&&document.activeElement!==$('seed'))$('seed').value=s.seed;
if(s.mode&&s.mode!==chatMode){chatMode=s.mode;paintMode();}
$('status').classList.toggle('busy',busy);
var pr=s.progress;
if(pr&&pr.active){$('progChip').style.display='';
$('progT').textContent=(pr.idx?('Кадр #'+pr.idx+' · '):'')+(pr.stage||'генерация');
$('progL').textContent=(pr.max?('шаг '+pr.value+'/'+pr.max+' · '+pr.pct+'%'):'подготовка')+
(pr.overall!=null?' · всего '+pr.overall+'%':'');
$('progU').style.width=(pr.overall||0)+'%';}
else $('progChip').style.display='none';
$('statusText').textContent=busy?((pr&&pr.active&&pr.overall!=null)?'генерация '+pr.overall+'%':'работа…'):'готов';
$('sendBtn').disabled=busy;
syncLog(s.messages);syncGallery(s.items);
if(wasBusy&&!busy){chime();toast('Готово');}
wasBusy=busy;}
catch(e){$('statusText').textContent='нет связи';$('status').classList.remove('busy');}
ticking=false;}
/* фирменный бегунок прокрутки */
function csScroll(host,id){
if(!host||!host.parentElement)return;
host.classList.add('csb-host');
var bar=node('div','csb');
var up=node('button','csb-btn csb-up','▲');up.type='button';up.title='Вверх на страницу';
var dn=node('button','csb-btn csb-dn','▼');dn.type='button';dn.title='Вниз на страницу';
var track=node('div','csb-track');track.tabIndex=0;track.setAttribute('role','scrollbar');
track.setAttribute('aria-controls',id||'');track.setAttribute('aria-orientation','vertical');
var thumb=node('button','csb-thumb');thumb.type='button';thumb.title='Тяни — прокрутка';
track.appendChild(thumb);bar.appendChild(up);bar.appendChild(track);bar.appendChild(dn);
host.parentElement.appendChild(bar);
var drag=null;
function paint(){
var sh=host.scrollHeight,ch=host.clientHeight,over=sh-ch>2;
bar.classList.toggle('on',over);
if(!over)return;
bar.style.top=host.offsetTop+'px';bar.style.height=ch+'px';
var th=track.clientHeight,hh=Math.max(34,Math.round(th*ch/sh));
thumb.style.height=hh+'px';
var p=host.scrollTop/(sh-ch);
thumb.style.top=Math.round(p*(th-hh))+'px';
track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');
track.setAttribute('aria-valuenow',String(Math.round(p*100)));}
function setTop(v){host.scrollTop=Math.max(0,Math.min(host.scrollHeight-host.clientHeight,v));}
thumb.addEventListener('pointerdown',function(e){
if(!bar.classList.contains('on'))return;
drag={off:e.clientY-thumb.getBoundingClientRect().top};
try{thumb.setPointerCapture(e.pointerId);}catch(err){}
bar.classList.add('act');document.body.classList.add('dragging');
e.preventDefault();e.stopPropagation();});
thumb.addEventListener('pointermove',function(e){
if(!drag)return;
var th=track.clientHeight,hh=thumb.offsetHeight;
var ny=e.clientY-drag.off-track.getBoundingClientRect().top;
setTop(Math.max(0,Math.min(1,ny/Math.max(1,th-hh)))*(host.scrollHeight-host.clientHeight));});
function endDrag(e){
if(!drag)return;drag=null;
bar.classList.remove('act');document.body.classList.remove('dragging');
try{thumb.releasePointerCapture(e.pointerId);}catch(err){}}
thumb.addEventListener('pointerup',endDrag);
thumb.addEventListener('pointercancel',endDrag);
thumb.addEventListener('click',function(e){e.stopPropagation();});
track.addEventListener('click',function(e){
if(e.target.closest('button'))return;
var r=track.getBoundingClientRect(),hh=thumb.offsetHeight;
var p=(e.clientY-r.top-hh/2)/Math.max(1,r.height-hh);
setTop(p*(host.scrollHeight-host.clientHeight));});
up.addEventListener('click',function(e){e.stopPropagation();host.scrollTop-=host.clientHeight*0.9;});
dn.addEventListener('click',function(e){e.stopPropagation();host.scrollTop+=host.clientHeight*0.9;});
bar.addEventListener('wheel',function(e){host.scrollTop+=e.deltaY;e.preventDefault();},{passive:false});
track.addEventListener('keydown',function(e){
var step=e.key==='PageUp'?-host.clientHeight*0.9:e.key==='PageDown'?host.clientHeight*0.9:
e.key==='ArrowUp'?-64:e.key==='ArrowDown'?64:
e.key==='Home'?-host.scrollHeight:e.key==='End'?host.scrollHeight:0;
if(step){e.preventDefault();host.scrollTop+=step;}});
host.addEventListener('scroll',paint,{passive:true});
new MutationObserver(paint).observe(host,{childList:true,subtree:true,characterData:true});
if(window.ResizeObserver){var ro=new ResizeObserver(paint);ro.observe(host);}
else window.addEventListener('resize',paint);
paint();}
csScroll(log,'log');csScroll(grid,'grid');csScroll(document.querySelector('.railbody'),'railbody');
csScroll($('seedIdeas'),'seedIdeas');
paintMode();loadCfg(false); tick();setInterval(tick,1200);
hwTick();setInterval(hwTick,3000);
setTimeout(function(){inp.focus();},400);
})();
</script></body></html>"""

# ============================================================================
# СТАРТ
# ============================================================================
async def _ideas_bg():
    await asyncio.sleep(3)
    cur = load_ideas()
    for mode in IDEA_MODES:
        if not cur or len(cur.get(mode) or []) < 5:
            try:
                print(f"[ideas] генерирую {mode}…")
                await generate_ideas(mode)
            except Exception as e:
                print(f"[ideas] {mode} не вышло:", e)
async def on_start(app):
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_connect=10,sock_read=600))
    await ensure_tipo()          # автозапуск TIPO до всего остального
    await fetch_assets()
    app["ws_task"] = asyncio.create_task(ws_loop())
    asyncio.create_task(_ideas_bg())
async def on_cleanup(app):
    app["ws_task"].cancel()
    stop_tipo()                  # гасим llama-server вместе с мостом
    if HTTP: await HTTP.close()
if __name__ == "__main__":
    if Image is None:
        print("!!! Pillow не установлен — исходники работать не будут: pip install pillow")
    if not os.path.isdir(CONFIG["output_dir"]):
        print("!!! output_dir не существует:", CONFIG["output_dir"])
    print("!!! ComfyUI: python main.py --cuda-device 1   (3070 Ti)")
    print("!!! LM Studio (Qwen3.5) — на CMP 90HX")
    print("!!! TIPO запускается мостом сам: tipo_exe + tipo_model_path, GPU = tipo_device")
    app = web.Application(client_max_size=64*1024**2)
    app.on_startup.append(on_start); app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", h_root),
        web.post("/api/ideas/regen", h_ideas_regen),
        web.get("/api/state", h_state),
        web.post("/api/mode", h_mode),
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