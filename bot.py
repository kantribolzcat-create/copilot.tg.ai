import os
import asyncio
import logging
import random
import re
import html
import base64
import io
import urllib.parse
import urllib.request
import datetime
import time
from bs4 import BeautifulSoup
from gtts import gTTS
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from openai import OpenAI
from playwright.async_api import async_playwright

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

groq_client = None
if GROQ_API_KEY:
    groq_client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=GROQ_API_KEY
    )

BOT_USERNAME = "Copilot_AI2026_bot"

TEXT_MODELS = [
    "llama-3.3-70b-versatile",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b"
]

VISION_MODELS = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b"
]

AUDIO_EXTENSIONS = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".webm", ".mp4")

BASE_INSTRUCTION = (
    "Ты — Copilot AI. "
    "ПРАВИЛО АДЕКВАТНОСТИ И ФАКТОВ: Никогда не спорь с пользователем слепо и не включай режим глухого отрицания («этого не было»), "
    "только потому что в паре строчек веб-поиска нет прямого подтверждения. Если данные в поиске неполные или отсутствуют, будь гибким, "
    "обсуждай тему по существу, учитывай аргументы пользователя и не устраивай газлайтинг. Если есть [СВЕЖИЕ ДАННЫЕ ИЗ СЕТИ], опирайся на них.\n"
    "ПРАВИЛО ТОЧНЫХ ВЫЧИСЛЕНИЙ: Если в сообщении есть блок [ТОЧНЫЙ РЕЗУЛЬТАТ ВЫЧИСЛЕНИЯ], используй его без сомнений.\n"
    "СТРОЖАЙШЕЕ ПРАВИЛО СМЕНЫ РОЛИ: Если пользователь просит сменить стиль или роль, мгновенно переключайся!\n"
    "СТИЛЬ ПО УМОЛЧАНИЮ: Отвечай с ЕДКИМ САРКАЗМОМ, ЖИВЫМ ЮМОРОМ И ИРОНИЕЙ! Подкалывай собеседника, но оставайся адекватным и умным.\n"
    "ФОРМАТ: Пиши емко, лаконично и четко по сути, чтобы текст влезал на экран смартфона. ВСЕГДА завершай мысль точкой.\n"
    "ЯЗЫК: ИСКЛЮЧИТЕЛЬНО РУССКИЙ ЯЗЫК."
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

chat_history = {}
chat_custom_instructions = {}
group_message_counters = {}
chat_last_interaction = {}
bot_stats = {"requests_handled": 0, "math_solved": 0, "searches_made": 0}
AUTO_CLEAR_HOURS = 6
_bot_id_cache: int | None = None


async def get_cached_bot_id() -> int:
    global _bot_id_cache
    if _bot_id_cache is None:
        me = await bot.get_me()
        _bot_id_cache = me.id
    return _bot_id_cache


def prepare_text_for_tts(text: str) -> str:
    """Очищает разметку и спецсимволы, чтобы озвучка звучала естественно."""
    cleaned = re.sub(r'```.*?```', ' блок кода ', text, flags=re.DOTALL)
    cleaned = cleaned.replace("`", "").replace("**", "").replace("*", "")
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = cleaned.replace('"', '').replace("'", "").replace('«', '').replace('»', '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:1500]


async def send_voice_reply(message: types.Message, reply_text: str) -> None:
    """Озвучивает ответ и отправляет его голосовым сообщением (с откатом на текст при ошибке)."""
    chat_id = message.chat.id
    speech_text = prepare_text_for_tts(reply_text)
    if not speech_text:
        speech_text = "Хм, сказать вроде и нечего."

    audio_path = f"reply_voice_{message.message_id}_{random.randint(1000, 9999)}.ogg"
    try:
        await bot.send_chat_action(chat_id=chat_id, action="record_voice")

        def synth_tts():
            tts = gTTS(text=speech_text, lang="ru", slow=False)
            tts.save(audio_path)

        await asyncio.to_thread(synth_tts)
        audio_file = types.FSInputFile(audio_path)
        await message.reply_voice(voice=audio_file)
    except Exception as e:
        logging.error(f"Ошибка генерации/отправки голосового ответа: {e}")
        processed_text = clean_text_for_telegram(reply_text)
        try:
            await message.reply(processed_text, parse_mode="HTML")
        except Exception:
            await message.reply(html.escape(processed_text), parse_mode="HTML")
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


# ==========================================
# УСТАНОВКА ГЛАВНОГО МЕНЮ КОМАНД В ТЕЛЕГРАМЕ
# ==========================================
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="status", description="Показать статус-панель и метрики"),
        BotCommand(command="mode", description="Сменить роль или стиль (например /mode Жосткий хакер)"),
        BotCommand(command="reset", description="Стереть историю и сбросить память чата")
    ]
    await bot.set_my_commands(commands)


# ==========================================
# МОДУЛЬ ТОЧНЫХ МАТЕМАТИЧЕСКИХ ВЫЧИСЛЕНИЙ
# ==========================================
def try_solve_math(text: str) -> str | None:
    try:
        lower = text.lower()
        math_keywords = ["плюс", "минус", "умножить", "разделить", "+", "*", "/"]
        if not any(kw in lower for kw in math_keywords):
            return None

        if re.search(r'\b(19|20)\d{2}\b', text) and not any(op in text for op in ['+', '*', '/']):
            return None

        cleaned = text.lower().replace("сколько будет", "").replace("чему равно", "").replace("?", "").strip()
        cleaned = cleaned.replace(" умножить на ", " * ").replace(" умножить ", " * ").replace(" на ", " * ")
        cleaned = cleaned.replace(" плюс ", " + ").replace(" минус ", " - ").replace(" разделить на ", " / ").replace(" разделить ", " / ")
        cleaned = cleaned.replace("х", "*").replace("×", "*").replace(":", "/")

        cleaned_expr = re.sub(r'[^0-9\+\-\*\/\.\(\)\s]', '', cleaned).strip()

        if not cleaned_expr or not any(op in cleaned_expr for op in ['+', '-', '*', '/']):
            return None

        result = eval(cleaned_expr, {"__builtins__": {}}, {})
        if isinstance(result, float) and result.is_integer():
            result = int(result)

        bot_stats["math_solved"] += 1
        return str(result)
    except Exception:
        return None


# ==========================================
# МОДУЛЬ ВЕБ-АВТОМАТИЗАЦИИ (PLAYWRIGHT)
# ==========================================
async def automate_web_order(item_name: str, address: str, phone: str, user_name: str) -> bool:
    logging.info(f"Старт браузерного бота для заказа '{item_name}' по адресу '{address}'...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            await page.goto("https://demo.ecomwebtemplate.com/", timeout=15000)
            await page.wait_for_load_state("domcontentloaded")

            search_input = page.locator("input[type='search'], input[type='text'], input[name='s']").first
            if await search_input.count() > 0:
                await search_input.fill(item_name)
                await search_input.press("Enter")
                await page.wait_for_timeout(2000)

            add_to_cart_btn = page.locator("button:has-text('Add'), a:has-text('Add'), .add-to-cart, button:has-text('Купить')").first
            if await add_to_cart_btn.count() > 0:
                await add_to_cart_btn.click()
                await page.wait_for_timeout(2000)

            checkout_btn = page.locator("a:has-text('Checkout'), a:has-text('Корзина'), button:has-text('Checkout')").first
            if await checkout_btn.count() > 0:
                await checkout_btn.click()
                await page.wait_for_timeout(2000)

            name_field = page.locator("input[name*='name'], input[id*='name']").first
            if await name_field.count() > 0:
                await name_field.fill(user_name)

            address_field = page.locator("input[name*='address'], textarea[name*='address']").first
            if await address_field.count() > 0:
                await address_field.fill(address)

            phone_field = page.locator("input[type='tel'], input[name*='phone']").first
            if await phone_field.count() > 0:
                await phone_field.fill(phone)

            logging.info("Робот успешно заполнил форму заказа на сайте!")
            await browser.close()
            return True
    except Exception as e:
        logging.error(f"Ошибка работы Playwright робота: {e}")
        return False


def search_web_real_sync(query: str, max_results=8) -> str:
    try:
        clean_query = re.sub(r'[^\w\sА-Яа-яЁё]', ' ', query).strip()
        clean_query = " ".join(clean_query.split()[:12])
        encoded_query = urllib.parse.quote(clean_query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
        )

        with urllib.request.urlopen(req, timeout=6) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(html_content, 'html.parser')

            snippets = []
            for result in soup.find_all('div', class_='result'):
                title_tag = result.find('a', class_='result__url')
                snippet_tag = result.find('a', class_='result__snippet')

                title = title_tag.get_text(strip=True) if title_tag else ""
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

                combined = f"{title} — {snippet}" if title else snippet
                if combined and len(combined) > 15:
                    snippets.append(combined)

                if len(snippets) >= max_results:
                    break

            if snippets:
                bot_stats["searches_made"] += 1
                return "\n• " + "\n• ".join(snippets)
    except Exception as e:
        logging.warning(f"Ошибка умного веб-поиска: {e}")
    return ""


def clean_text_for_telegram(text: str) -> str:
    text = text.replace("&nbsp;", " ")

    def code_block_replacer(match):
        stripped_content = match.group(2).strip('\n')
        escaped_content = html.escape(stripped_content)
        return f"<pre><code>{escaped_content}</code></pre>"

    text = re.sub(r'```([a-zA-Z]*)\n?(.*?)```', code_block_replacer, text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+?)`', lambda m: "<code>" + html.escape(m.group(1)) + "</code>", text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\s)\*(?!\s)(.*?)(?<!\s)\*(?!\w)', r'<i>\1</i>', text)
    return text


def parse_requested_emoji(text: str) -> str | None:
    supported_emojis = ["👍", "🔥", "❤️", "❤", "😍", "🗿", "😂", "💩", "🤮", "🎉", "👏", "⚡️", "🧠", "💻", "👎", "🤣", "☝️", "☝", "💢"]
    for char in text:
        if char in supported_emojis:
            return "❤️" if char == "❤" else ("☝️" if char == "☝" else char)

    lower = text.lower()
    if "палец вверх" in lower or "лайк" in lower:
        return "👍"
    if "палец вниз" in lower or "дизлайк" in lower:
        return "👎"
    if "вверх" in lower or "указательный" in lower or "палец" in lower or "☝" in lower:
        return "☝️"
    if "огонь" in lower or "пожар" in lower:
        return "🔥"
    if "сердце" in lower or "любовь" in lower:
        return "❤️"
    if "смешно" in lower or "кек" in lower or "ржака" in lower or "🤣" in lower or "😂" in lower:
        return "😂"
    if "моаи" in lower or "камень" in lower:
        return "🗿"
    if "топ" in lower or "ракета" in lower:
        return "⚡️"
    if "гнев" in lower or "злой" in lower or "💢" in lower:
        return "💢"
    return None


def fetch_completion_sync(model_name, messages, max_toks=1300):
    try:
        response = groq_client.chat.completions.create(model=model_name, messages=messages, max_tokens=max_toks)
        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Ошибка модели {model_name}: {e}")
        return f"ERR_API [{model_name}]: {e}"
    return None


def extract_audio_info(message: types.Message) -> tuple[str | None, str, str]:
    """Возвращает (file_id, display_name, ext), если сообщение содержит аудиофайл."""
    if message.audio:
        ext = os.path.splitext(message.audio.file_name or "")[1].lower()
        if not ext:
            mime = (message.audio.mime_type or "").lower()
            if "mpeg" in mime or "mp3" in mime:
                ext = ".mp3"
            elif "ogg" in mime:
                ext = ".ogg"
            elif "wav" in mime:
                ext = ".wav"
            elif "m4a" in mime or "mp4" in mime:
                ext = ".m4a"
            else:
                ext = ".mp3"
        name = message.audio.title or message.audio.file_name or "аудиофайл"
        return message.audio.file_id, name, ext

    if message.document:
        mime = (message.document.mime_type or "").lower()
        file_name = message.document.file_name or ""
        ext = os.path.splitext(file_name)[1].lower()
        if mime.startswith("audio/") or ext in AUDIO_EXTENSIONS:
            return message.document.file_id, file_name or "аудиодокумент", ext or ".mp3"

    return None, "", ""


async def is_reply_to_bot(message: types.Message) -> bool:
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return False
    bot_id = await get_cached_bot_id()
    return message.reply_to_message.from_user.id == bot_id


async def generate_text_reply(chat_id: int, user_prompt_for_history: str) -> str:
    """Добавляет запрос в историю чата и получает ответ через текстовые модели Groq."""
    chat_history[chat_id].append({"role": "user", "content": user_prompt_for_history})
    if len(chat_history[chat_id]) > 6:
        chat_history[chat_id] = chat_history[chat_id][-6:]

    current_time_str = datetime.datetime.now().strftime("%d.%m.%Y (%A), %H:%M")
    dynamic_system_prompt = f"ТЕКУЩЕЕ ТОЧНОЕ ВРЕМЯ И ДАТА: {current_time_str}.\n{chat_custom_instructions[chat_id]}"
    messages_payload = [{"role": "system", "content": dynamic_system_prompt}] + chat_history[chat_id]

    reply_text = None
    attempt = 1
    while not reply_text:
        for model_name in TEXT_MODELS:
            res = await asyncio.to_thread(fetch_completion_sync, model_name, messages_payload, 1300)
            if res and not res.startswith("ERR_API"):
                reply_text = res
                break

        if not reply_text:
            attempt += 1
            logging.info(f"Попытка текста {attempt}: модели заняты, ждем 10 сек...")
            await asyncio.sleep(10)

    chat_history[chat_id].append({"role": "assistant", "content": reply_text})
    return reply_text


async def build_and_send_ai_answer(message: types.Message, prompt_text: str, as_voice: bool = False) -> None:
    """Выполняет расчёт/веб-поиск, запрашивает ИИ и отправляет голос или текст."""
    chat_id = message.chat.id
    lower_prompt = prompt_text.lower().strip()

    change_style_triggers = ["будь ", "стань ", "веди себя как ", "отвечай как ", "разговаривай как ", "пиши как ", "переключись на "]
    for trig in change_style_triggers:
        if lower_prompt.startswith(trig) or f" {trig}" in lower_prompt:
            chat_custom_instructions[chat_id] = (
                f"Ты — Copilot AI. ПРИКАЗ ПО ПОВЕДЕНИЮ: {prompt_text}. "
                "Соблюдай эту роль СТРОГО И БЕЗОГОВОРНО в каждом ответе! Отвечай КРАТКО, ЕМКО И НА РУССКОМ ЯЗЫКЕ."
            )
            chat_history[chat_id] = []

    requested_emoji = parse_requested_emoji(prompt_text)
    if requested_emoji:
        try:
            target_msg_id = message.reply_to_message.message_id if message.reply_to_message else message.message_id
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=target_msg_id,
                reaction=[types.ReactionTypeEmoji(emoji=requested_emoji)]
            )
        except Exception as e:
            logging.error(f"Ошибка реакций: {e}")

    math_result = try_solve_math(prompt_text)
    math_context = ""
    if math_result:
        math_context = f"\n\n[ТОЧНЫЙ РЕЗУЛЬТАТ ВЫЧИСЛЕНИЯ НА КАЛЬКУЛЯТОРЕ]: {math_result}"

    explicit_search_triggers = ["найди", "погугли", "ищи", "загугли", "новости", "курс", "погода", "цена", "что", "кто", "где", "когда", "почему", "как", "рил", "правда"]
    skip_search_words = ["привет", "ку", "здарова", "здорово", "как дела", "хай", "пока", "сброс", "reset", "start", "status"]

    need_web_search = False
    if math_result:
        need_web_search = False
    elif any(w in lower_prompt for w in skip_search_words) and len(prompt_text.split()) < 3:
        need_web_search = False
    elif "?" in prompt_text:
        need_web_search = True
    elif any(re.search(rf'\b{re.escape(trig)}\b', lower_prompt) for trig in explicit_search_triggers):
        need_web_search = True

    web_context = ""
    if need_web_search:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        search_result = await asyncio.to_thread(search_web_real_sync, prompt_text)
        if search_result:
            web_context = f"\n\n[СВЕЖИЕ ДАННЫЕ ИЗ СЕТИ]:\n{search_result}"

    await bot.send_chat_action(chat_id=chat_id, action="record_voice" if as_voice else "typing")

    final_user_content = prompt_text + math_context + web_context
    reply_text = await generate_text_reply(chat_id, final_user_content)

    if as_voice:
        await send_voice_reply(message, reply_text)
        return

    processed_text = clean_text_for_telegram(reply_text)
    try:
        await message.reply(processed_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки HTML: {e}")
        await message.reply(html.escape(processed_text), parse_mode="HTML")


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    chat_id = message.chat.id
    chat_history[chat_id] = []
    chat_custom_instructions[chat_id] = BASE_INSTRUCTION
    chat_last_interaction[chat_id] = time.time()
    await message.answer(
        "Здарова! Я <b>Copilot AI</b>. Нажми кнопку ввода команды <b>/</b> в чате, чтобы увидеть всю панель управления!",
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode="HTML"
    )


@dp.message(Command("reset"))
async def reset_cmd(message: types.Message):
    chat_id = message.chat.id
    chat_history[chat_id] = []
    chat_custom_instructions[chat_id] = BASE_INSTRUCTION
    chat_last_interaction[chat_id] = time.time()
    await message.reply("Память стёрта в этом чате!", reply_markup=types.ReplyKeyboardRemove())


@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    chat_id = message.chat.id
    chat_type = message.chat.type

    current_inst = chat_custom_instructions.get(chat_id, BASE_INSTRUCTION)
    role_name = "Дефолтный сарказм"
    if "ПРИКАЗ ПО ПОВЕДЕНИЮ:" in current_inst:
        parts = current_inst.split("ПРИКАЗ ПО ПОВЕДЕНИЮ:")
        if len(parts) > 1:
            role_name = parts[1].split(".")[0].strip()

    history_len = len(chat_history.get(chat_id, []))

    status_text = (
        f"<b>СТАТУС-ПАНЕЛЬ ИИ-БОТА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Тип чата:</b> <code>{chat_type}</code>\n"
        f"<b>Активная роль:</b> <i>{html.escape(role_name)}</i>\n"
        f"<b>Сообщений в памяти чата:</b> {history_len} / 6\n"
        f"<b>Успешных веб-поисков:</b> {bot_stats['searches_made']}\n"
        f"<b>Решено примеров:</b> {bot_stats['math_solved']}\n"
        f"<b>Всего запросов обработано:</b> {bot_stats['requests_handled']}\n"
        f"<b>Статус API:</b> Онлайн (Groq + Voice/Whisper/TTS + Web + Playwright)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Всё работает штатно, газлайтинг и ссылки отключены.</i>"
    )
    await message.reply(status_text, parse_mode="HTML")


@dp.message(Command("mode"))
async def mode_cmd(message: types.Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        new_persona = args[1]
        chat_custom_instructions[chat_id] = (
            f"Ты — Copilot AI. ПРИКАЗ ПО ПОВЕДЕНИЮ: {new_persona}. "
            "Соблюдай эту роль СТРОГО И БЕЗОГОВОРНО в каждом ответе! Отвечай КРАТКО, ЕМКО И НА РУССКОМ ЯЗЫКЕ."
        )
        chat_history[chat_id] = []
        chat_last_interaction[chat_id] = time.time()
        await message.reply(f"Поведение мгновенно изменено на: <b>{html.escape(new_persona)}</b>", parse_mode="HTML")
    else:
        await message.reply("Укажи режим, например: <code>/mode Будь максимально серьезным ученым</code>", parse_mode="HTML")


# ==========================================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ И ИИ
# ==========================================
@dp.message()
async def handle_ai_response(message: types.Message):
    asyncio.create_task(process_user_message_async(message))


async def process_user_message_async(message: types.Message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    is_private_or_business = (chat_type == "private") or bool(getattr(message, "business_connection_id", None))

    raw_text = message.text or message.caption or ""
    lower_raw = raw_text.lower().strip()

    # ЖЕСТКАЯ МОМЕНТАЛЬНАЯ ОЗВУЧКА — В САМОМ ВЕРХУ, ПЕРЕД РЕПЛАЯМИ И ФИЛЬТРАМИ
    if lower_raw.startswith("скажи") or lower_raw.startswith("озвучь"):
        words = raw_text.split()
        speech_prompt = " ".join(words[1:]) if len(words) > 1 else "Пустой текст."

        await bot.send_chat_action(chat_id=chat_id, action="record_voice")
        audio_path = f"voice_temp_{random.randint(1000, 9999)}.ogg"
        try:
            clean_for_speech = speech_prompt.replace('"', '').replace("'", "").replace('«', '').replace('»', '').replace('*', '')
            tts = gTTS(text=clean_for_speech, lang='ru', slow=False)
            tts.save(audio_path)

            audio_file = types.FSInputFile(audio_path)
            await message.reply_voice(
                voice=audio_file,
                caption=f"<b>{html.escape(speech_prompt)}</b>",
                parse_mode="HTML"
            )
            if os.path.exists(audio_path):
                os.remove(audio_path)
            return
        except Exception as e:
            logging.error(f"ОШИБКА ГОЛОСА: {e}")
            if os.path.exists(audio_path):
                os.remove(audio_path)
            await message.reply(f"Ошибка озвучки: {e}")
            return

    now = time.time()
    bot_stats["requests_handled"] += 1

    if chat_id in chat_last_interaction:
        if now - chat_last_interaction[chat_id] > (AUTO_CLEAR_HOURS * 3600):
            chat_history[chat_id] = []
            chat_custom_instructions[chat_id] = BASE_INSTRUCTION
    chat_last_interaction[chat_id] = now

    if chat_id not in chat_custom_instructions:
        chat_custom_instructions[chat_id] = BASE_INSTRUCTION

    if chat_id not in chat_history:
        chat_history[chat_id] = []

    # Обработка голосовых сообщений (ГС):
    # расшифровка всегда (в ЛС и группах), а ответ голосовым — в ЛС или по реплаю на бота в группе
    if message.voice:
        replied_to_bot = await is_reply_to_bot(message)
        should_voice_answer = is_private_or_business or replied_to_bot

        temp_audio_path = f"temp_voice_{message.message_id}_{random.randint(1000, 9999)}.ogg"
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            voice_file = await bot.get_file(message.voice.file_id)
            voice_bytes = await bot.download_file(voice_file.file_path)
            raw_voice = voice_bytes.read() if hasattr(voice_bytes, 'read') else voice_bytes.getvalue()

            with open(temp_audio_path, "wb") as f:
                f.write(raw_voice)

            def transcribe_voice():
                with open(temp_audio_path, "rb") as audio_file:
                    return groq_client.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=audio_file,
                        response_format="text",
                        language="ru"
                    )

            transcript_response = await asyncio.to_thread(transcribe_voice)
            transcript_text = (
                transcript_response if isinstance(transcript_response, str) else transcript_response.text
            ).strip()

            if not transcript_text:
                await message.reply("Не разобрал твоё бубнение, давай почетче!")
                if should_voice_answer:
                    await send_voice_reply(message, "Не разобрал твоё бубнение, повтори почетче.")
                return

            await message.reply(f"<i>Распознано:</i> <b>{html.escape(transcript_text)}</b>", parse_mode="HTML")

            if should_voice_answer:
                await build_and_send_ai_answer(message, transcript_text, as_voice=True)
            return
        except Exception as e:
            logging.error(f"Ошибка распознавания голосового: {e}")
            await message.reply("Не разобрал твоё бубнение, давай почетче!")
            if should_voice_answer:
                await send_voice_reply(message, "Что-то пошло не так с распознаванием голоса, попробуй ещё раз.")
            return
        finally:
            if os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                except OSError:
                    pass

    # Обработка реплаев для остальных сообщений
    text_content = raw_text
    if message.reply_to_message:
        replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if replied_text:
            if text_content:
                text_content = f"{replied_text}\n{text_content}"
            else:
                text_content = replied_text

    lower_text = text_content.lower().strip()

    # Обработка фото (Vision) с бесконечным циклом
    if message.photo:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        reply_text = None

        try:
            photo = message.photo[-1]
            photo_file = await bot.get_file(photo.file_id)
            photo_bytes_io = await bot.download_file(photo_file.file_path)

            raw_data = photo_bytes_io.read() if hasattr(photo_bytes_io, 'read') else photo_bytes_io.getvalue()

            if HAS_PIL:
                try:
                    img = Image.open(io.BytesIO(raw_data))
                    img.thumbnail((1024, 1024))
                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG", quality=85)
                    raw_data = buffer.getvalue()
                except Exception as img_err:
                    logging.warning(f"Сжатие PIL пропущено: {img_err}")

            base64_image = base64.b64encode(raw_data).decode("utf-8")

            prompt_text = text_content if text_content else (
                "Прочитай весь текст на этой картинке/инфографике и подробно перескажи суть "
                "в твоем фирменном саркастичном стиле!"
            )

            current_time_str = datetime.datetime.now().strftime("%d.%m.%Y (%A), %H:%M")
            vision_system_prompt = f"ТЕКУЩЕЕ ТОЧНОЕ ВРЕМЯ И ДАТА: {current_time_str}.\n{chat_custom_instructions[chat_id]}"

            vision_messages = [
                {"role": "system", "content": vision_system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]

            attempt = 1
            while not reply_text:
                for model_name in VISION_MODELS:
                    try:
                        response = await asyncio.to_thread(
                            groq_client.chat.completions.create,
                            model=model_name,
                            messages=vision_messages,
                            max_tokens=1300
                        )
                        if response.choices and response.choices[0].message.content:
                            reply_text = response.choices[0].message.content
                            break
                    except Exception as api_err:
                        logging.warning(f"Попытка фото {attempt}: модель {model_name} занята ({api_err}), ждем...")

                if not reply_text:
                    attempt += 1
                    await asyncio.sleep(5)

        except Exception as e:
            logging.error(f"Критическая ошибка обработки фото: {e}")
            reply_text = f"Ошибка обработки фото: {e}"

        chat_history[chat_id].append({"role": "user", "content": f"[Фото]: {text_content}"})
        chat_history[chat_id].append({"role": "assistant", "content": reply_text})
        await message.reply(clean_text_for_telegram(reply_text), parse_mode="HTML")
        return

    # Обработка аудиофайлов (2 этапа: 1. Распознавание через Whisper -> 2. Описание через ИИ)
    audio_file_id, audio_name, audio_ext = extract_audio_info(message)
    if audio_file_id:
        temp_audio_file_path = f"temp_audio_{message.message_id}_{random.randint(1000, 9999)}{audio_ext}"
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            tg_file = await bot.get_file(audio_file_id)
            audio_bytes_io = await bot.download_file(tg_file.file_path)
            raw_audio = audio_bytes_io.read() if hasattr(audio_bytes_io, 'read') else audio_bytes_io.getvalue()

            with open(temp_audio_file_path, "wb") as f:
                f.write(raw_audio)

            def transcribe_audio_file():
                with open(temp_audio_file_path, "rb") as audio_file:
                    return groq_client.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=audio_file,
                        response_format="text"
                    )

            transcript_response = await asyncio.to_thread(transcribe_audio_file)
            transcript_text = (
                transcript_response if isinstance(transcript_response, str) else transcript_response.text
            ).strip()

            if transcript_text:
                await message.reply(
                    f"<b>Этап 1/2 — Распознано</b> (<code>{html.escape(audio_name)}</code>):\n"
                    f"<b>{html.escape(transcript_text)}</b>",
                    parse_mode="HTML"
                )
                user_instruction = raw_text.strip() if raw_text.strip() else (
                    "Опиши суть этого аудиофайла, выдели главные мысли и сделай краткий вывод."
                )
                description_prompt = (
                    f"[Аудиофайл «{audio_name}»]\n"
                    f"Распознанный текст: {transcript_text}\n"
                    f"Запрос пользователя: {user_instruction}"
                )
            else:
                await message.reply(
                    f"<b>Этап 1/2 — Распознано</b> (<code>{html.escape(audio_name)}</code>):\n"
                    f"<i>Речь не распознана (возможно, музыка, шум или тишина).</i>",
                    parse_mode="HTML"
                )
                user_instruction = raw_text.strip() if raw_text.strip() else (
                    "Опиши этот аудиофайл по названию и факту отсутствия распознанной речи."
                )
                description_prompt = (
                    f"[Аудиофайл «{audio_name}»]\n"
                    f"Распознать речь не удалось (возможны музыка, шумы или тишина).\n"
                    f"Запрос пользователя: {user_instruction}"
                )

            await bot.send_chat_action(chat_id=chat_id, action="typing")
            reply_text = await generate_text_reply(chat_id, description_prompt)
            processed_text = clean_text_for_telegram(reply_text)
            try:
                await message.reply(
                    f"<b>Этап 2/2 — Описание ИИ:</b>\n{processed_text}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка отправки HTML для описания аудио: {e}")
                await message.reply(
                    f"<b>Этап 2/2 — Описание ИИ:</b>\n{html.escape(processed_text)}",
                    parse_mode="HTML"
                )
            return
        except Exception as e:
            logging.error(f"Ошибка обработки аудиофайла: {e}")
            await message.reply(f"Не удалось обработать аудиофайл: {e}")
            return
        finally:
            if os.path.exists(temp_audio_file_path):
                try:
                    os.remove(temp_audio_file_path)
                except OSError:
                    pass

    should_answer = True
    if not is_private_or_business and not message.photo:
        should_answer = False
        if await is_reply_to_bot(message):
            should_answer = True
        if f"@{BOT_USERNAME}".lower() in lower_text:
            should_answer = True
            text_content = text_content.replace(f"@{BOT_USERNAME}", "").strip()

        if not should_answer:
            group_message_counters[chat_id] = group_message_counters.get(chat_id, 0) + 1
            if group_message_counters[chat_id] >= 15:
                group_message_counters[chat_id] = 0
                should_answer = True

    if not should_answer:
        return

    # Генерация картинок (Pollinations) — без мусорных текстов на картинке и в подписи
    if any(trig in lower_raw for trig in ["сгенерируй", "нарисуй", "создай картинку", "аватарка", "изображение"]):
        clean_prompt = raw_text
        for trig in ["сгенерируй", "нарисуй", "создай картинку", "создай", "изображение", "картинку", "аватарка", f"@{BOT_USERNAME}"]:
            clean_prompt = re.sub(re.escape(trig), "", clean_prompt, flags=re.IGNORECASE).strip()

        clean_prompt = clean_prompt or raw_text
        await bot.send_chat_action(chat_id=chat_id, action="upload_photo")

        final_img_prompt = f"{clean_prompt}, clean image, no text, no letters, no watermark"
        encoded_prompt = urllib.parse.quote(final_img_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed={random.randint(1, 999999)}"
        try:
            await message.reply_photo(photo=image_url, caption=f"<b>{html.escape(clean_prompt)}</b>", parse_mode="HTML")
            return
        except Exception as e:
            logging.error(f"Ошибка картинки: {e}")
            return

    if not text_content:
        return

    await build_and_send_ai_answer(message, text_content, as_voice=False)


async def main():
    await set_bot_commands(bot)
    logging.info("Copilot AI запущен в идеальном штатном режиме.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
