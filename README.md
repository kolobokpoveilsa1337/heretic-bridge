# HERETIC · Anime Generation Bridge

Мост между LLM (Qwen3.5-9B через LM Studio), ComfyUI (SDXL/Illustrious) 
и VLM-критиком (Qwen3.5-9B) для автоматической генерации аниме-артов.

## Возможности
- 💬 Мультичат с историей диалогов
- 🎨 Генерация серий изображений через ComfyUI
- 🔍 VLM-критик: проверка анатомии и соответствия замыслу
- 🔄 Авто-ретрай при дефектах
- ✏️ Режим правки готового кадра (без потери композиции)
- 📊 Телеметрия: температура, загрузка, потребление GPU/CPU (Вт)
- ⚙️ Веб-интерфейс с настройкой LoRA, пресетами, лайтбоксом

## Требования
- Python 3.10+
- [LM Studio](https://lmstudio.ai/) с загруженной моделью Qwen
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) (запуск на отдельной GPU)
- `aiohttp`, `psutil`, `Pillow`

## Установка

```bash
pip install aiohttp psutil Pillow