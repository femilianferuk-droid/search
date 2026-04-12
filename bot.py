import asyncio
import logging
import os
import json
import random
import string
from typing import Optional, List

from dotenv import load_dotenv
import aiohttp

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
import aiosqlite

# --- Загрузка переменных окружения ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Не указан BOT_TOKEN")

# --- Настройки ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# HTTP сессия
http_session: Optional[aiohttp.ClientSession] = None

# --- База данных ---
DB_PATH = "vest_search.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                searches_count INTEGER DEFAULT 0,
                last_searches TEXT DEFAULT '[]'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS market (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                seller_username TEXT,
                username_sale TEXT UNIQUE,
                price INTEGER,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def create_or_update_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(user_id)
        if not user:
            await db.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            await db.commit()
        elif username and user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()

async def increment_search(user_id: int, query_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(user_id)
        if user:
            new_count = user[2] + 1
            last_searches = json.loads(user[3] or "[]")
            last_searches.insert(0, query_text)
            last_searches = last_searches[:5]
            await db.execute(
                "UPDATE users SET searches_count = ?, last_searches = ? WHERE user_id = ?",
                (new_count, json.dumps(last_searches), user_id)
            )
            await db.commit()

# --- Состояния ---
class SearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()

class SellStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_price = State()

# --- Премиум эмодзи ---
EMOJI = {
    "search": "5870982283724328568",
    "market": "5884479287171485878",
    "profile": "5870994129244131212",
    "check": "5870633910337015697",
    "cross": "5870657884844462243",
    "pencil": "5870676941614354370",
    "info": "6028435952299413210",
    "bot": "6030400221232501136",
    "eye": "6037397706505195857",
    "money": "5904462880941545555",
    "gift": "6032644646587338669",
    "back": "5893057118545646106",
    "clock": "5983150113483134607",
    "write": "5870753782874246579",
    "link": "5769289093221454192",
    "graph": "5870921681735781843",
    "loading": "5345906554510012647",
    "wallet": "5769126056262898415",
}

def em(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(name, EMOJI["check"])}">👍</tg-emoji>'

# --- Реальная проверка юзернейма через t.me ---
async def check_username_availability(username: str) -> bool:
    """
    Проверяет, свободен ли юзернейм через HEAD-запрос к t.me/username
    True = свободен, False = занят
    """
    url = f"https://t.me/{username}"
    try:
        async with http_session.head(url, timeout=5, allow_redirects=True) as resp:
            if resp.status == 404:
                return True  # Свободен
            elif resp.status == 200:
                return False  # Занят
            elif resp.status == 429:
                logger.warning(f"Rate limit для {username}, ждём...")
                await asyncio.sleep(2)
                return await check_username_availability(username)
            else:
                logger.warning(f"Неожиданный статус {resp.status} для {username}")
                return False
    except asyncio.TimeoutError:
        logger.warning(f"Таймаут при проверке {username}")
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки {username}: {e}")
        return False

# --- Генерация вариантов для поиска ---
def generate_username_variants(base: str, length: int, count: int = 10) -> List[str]:
    """Генерирует варианты юзернеймов на основе ключевого слова"""
    variants = []
    alphabet = string.ascii_lowercase + string.digits
    
    # Вариант 1: просто обрезаем/дополняем базу
    if len(base) < length:
        variants.append(base + ''.join(random.choices(alphabet, k=length - len(base))))
    elif len(base) > length:
        variants.append(base[:length])
    else:
        variants.append(base)
    
    # Вариант 2: добавляем цифры
    for i in range(count - 1):
        suffix = ''.join(random.choices(string.digits, k=random.randint(1, 3)))
        if len(base) + len(suffix) <= length:
            variants.append(base + suffix)
        else:
            variants.append(base[:length - len(suffix)] + suffix)
    
    # Вариант 3: заменяем буквы на цифры (leet)
    leet_map = {'a': '4', 'e': '3', 'i': '1', 'o': '0', 's': '5'}
    leet_base = ''.join(leet_map.get(c, c) for c in base)
    if leet_base != base and len(leet_base) <= length:
        variants.append(leet_base)
    
    return list(set(variants))  # Убираем дубликаты

async def search_usernames(keyword: str, length: int, max_results: int = 5) -> List[str]:
    """Ищет свободные юзернеймы с указанным ключевым словом и длиной"""
    found = []
    variants = generate_username_variants(keyword.lower(), length, 15)
    
    for variant in variants:
        if len(variant) != length:
            continue
        if not variant[0].isalpha():
            continue
        if not all(c.isalnum() or c == '_' for c in variant):
            continue
            
        if await check_username_availability(variant):
            found.append(variant)
            if len(found) >= max_results:
                break
        await asyncio.sleep(0.3)  # Задержка между запросами
    
    return found

async def search_by_length_only(length: int, max_results: int = 5) -> List[str]:
    """Поиск случайных свободных юзернеймов заданной длины"""
    found = []
    alphabet = string.ascii_lowercase
    attempts = 0
    max_attempts = 50
    
    while len(found) < max_results and attempts < max_attempts:
        # Генерируем случайный юзернейм
        username = ''.join(random.choices(alphabet, k=length))
        
        if await check_username_availability(username):
            found.append(username)
        
        attempts += 1
        await asyncio.sleep(0.3)
    
    return found

# --- Клавиатуры ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(
        types.KeyboardButton(text=f"{em('search')} Поиск"),
        types.KeyboardButton(text=f"{em('market')} Маркет"),
        types.KeyboardButton(text=f"{em('profile')} Профиль")
    )
    return builder.as_markup(resize_keyboard=True)

def get_search_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="5 букв",
            callback_data="search_len_5",
            icon_custom_emoji_id=EMOJI["search"]
        )],
        [InlineKeyboardButton(
            text="6 букв",
            callback_data="search_len_6",
            icon_custom_emoji_id=EMOJI["search"]
        )],
        [InlineKeyboardButton(
            text="С ключевым словом",
            callback_data="search_keyword",
            icon_custom_emoji_id=EMOJI["pencil"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

# --- Обработчики ---
@router.message(Command("start"))
async def cmd_start(message: Message):
    await create_or_update_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"{em('bot')} <b>Vest Search</b>\n"
        f"{em('info')} Бот для поиска свободных юзернеймов Telegram.\n"
        f"Используй меню для навигации.",
        reply_markup=get_main_keyboard()
    )

@router.message(F.text == f"{em('search')} Поиск")
async def menu_search(message: Message):
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == f"{em('profile')} Профиль")
async def menu_profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    last_searches = json.loads(user[3] or "[]")
    last_str = "\n".join([f"• {s}" for s in last_searches]) if last_searches else "Пусто"
    
    text = (
        f"{em('profile')} <b>Профиль</b>\n\n"
        f"{em('link')} Юзернейм: @{user[1] or 'не указан'}\n"
        f"{em('graph')} Поисков: {user[2]}\n\n"
        f"{em('clock')} <b>Последние поиски:</b>\n{last_str}"
    )
    await message.answer(text, reply_markup=get_main_keyboard())

@router.message(F.text == f"{em('market')} Маркет")
async def menu_market(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, username_sale, price FROM market WHERE is_active = 1 ORDER BY id DESC LIMIT 10"
        ) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        await message.answer(
            f"{em('market')} <b>Маркет пуст</b>\n\nПока никто не выставил юзернеймы на продажу.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="Продать юзернейм",
                    callback_data="sell_start",
                    icon_custom_emoji_id=EMOJI["money"]
                )],
                [InlineKeyboardButton(
                    text="◁ Назад",
                    callback_data="back_to_main",
                    icon_custom_emoji_id=EMOJI["back"]
                )]
            ])
        )
        return

    text = f"{em('market')} <b>Маркет юзернеймов</b>\n\n"
    keyboard = []
    for row in rows:
        id_, name, price = row
        text += f"@{name} — {price} {em('money')}\n"
        keyboard.append([InlineKeyboardButton(
            text=f"@{name} | {price} ₽",
            callback_data=f"view_lot_{id_}",
            icon_custom_emoji_id=EMOJI["eye"]
        )])
    
    keyboard.append([InlineKeyboardButton(
        text="Продать юзернейм",
        callback_data="sell_start",
        icon_custom_emoji_id=EMOJI["money"]
    )])
    keyboard.append([InlineKeyboardButton(
        text="◁ Назад",
        callback_data="back_to_main",
        icon_custom_emoji_id=EMOJI["back"]
    )])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        f"{em('bot')} <b>Главное меню</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу свободные юзернеймы из {length} букв...</b>\n"
        f"{em('info')} Это может занять до 30 секунд.",
        reply_markup=get_back_button()
    )
    
    await increment_search(callback.from_user.id, f"Длина {length}")
    
    found = await search_by_length_only(length, max_results=5)
    
    if found:
        text = f"{em('check')} <b>Найдены свободные юзернеймы ({length} букв):</b>\n\n"
        text += "\n".join([f"• @{u} → t.me/{u}" for u in found])
    else:
        text = f"{em('cross')} <b>Ничего не найдено</b>\nПопробуй другие параметры."
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{em('pencil')} <b>Введи ключевое слово (латиница):</b>\n"
        f"Например: crypto, vest, moon",
        reply_markup=get_back_button()
    )
    await state.set_state(SearchStates.waiting_for_keyword)
    await callback.answer()

@router.message(SearchStates.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    keyword = message.text.strip().lower()
    
    # Проверка на латиницу и допустимые символы
    if not all(c.isalnum() or c == '_' for c in keyword):
        await message.answer(
            f"{em('cross')} Используй только латинские буквы, цифры и _",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    await state.update_data(keyword=keyword)
    await message.answer(
        f"{em('pencil')} <b>Укажи длину юзернейма (от 5 до 32):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(SearchStates.waiting_for_length)

@router.message(SearchStates.waiting_for_length)
async def process_length_and_search(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    length = int(message.text)
    if not (5 <= length <= 32):
        await message.answer(f"{em('cross')} Длина должна быть от 5 до 32.")
        return
    
    data = await state.get_data()
    keyword = data['keyword']
    
    await message.answer(
        f"{em('loading')} <b>Ищу юзернеймы с '{keyword}' ({length} букв)...</b>\n"
        f"{em('info')} Это может занять до 30 секунд.",
        reply_markup=get_main_keyboard()
    )
    
    await increment_search(message.from_user.id, f"'{keyword}' ({length})")
    
    found = await search_usernames(keyword, length, max_results=5)
    
    if found:
        text = f"{em('check')} <b>Найдены свободные юзернеймы:</b>\n\n"
        text += "\n".join([f"• @{u} → t.me/{u}" for u in found])
    else:
        text = f"{em('cross')} <b>Ничего не найдено.</b>\nПопробуй другое слово или длину."
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

@router.callback_query(F.data == "sell_start")
async def sell_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{em('write')} <b>Введи юзернейм для продажи (без @):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(SellStates.waiting_for_username)
    await callback.answer()

@router.message(SellStates.waiting_for_username)
async def sell_username_entered(message: Message, state: FSMContext):
    username = message.text.strip().replace("@", "").lower()
    
    if not (5 <= len(username) <= 32 and all(c.isalnum() or c == '_' for c in username)):
        await message.answer(f"{em('cross')} Некорректный формат юзернейма.")
        return
    
    # Проверяем, что юзернейм занят (значит существует)
    if await check_username_availability(username):
        await message.answer(
            f"{em('cross')} Этот юзернейм свободен! Продавать можно только существующие юзернеймы.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    await state.update_data(sale_username=username)
    await message.answer(
        f"{em('money')} <b>Введи цену в рублях:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(SellStates.waiting_for_price)

@router.message(SellStates.waiting_for_price)
async def sell_price_entered(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи целое число.")
        return
    
    price = int(message.text)
    data = await state.get_data()
    username_sale = data['sale_username']
    seller_id = message.from_user.id
    seller_username = message.from_user.username or f"id{seller_id}"
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO market (seller_id, seller_username, username_sale, price) VALUES (?, ?, ?, ?)",
                (seller_id, seller_username, username_sale, price)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await message.answer(
                f"{em('cross')} Этот юзернейм уже выставлен на продажу.",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            return
    
    await message.answer(
        f"{em('check')} <b>Объявление создано!</b>\n\n"
        f"@{username_sale} — {price} ₽\n\n"
        f"Покупатели смогут написать тебе через бота.",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data.startswith("view_lot_"))
async def view_lot(callback: CallbackQuery):
    lot_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT seller_id, seller_username, username_sale, price FROM market WHERE id = ? AND is_active = 1",
            (lot_id,)
        ) as cursor:
            lot = await cursor.fetchone()
    
    if not lot:
        await callback.answer("Лот не найден.", show_alert=True)
        return
    
    seller_id, seller_username, username_sale, price = lot
    text = (
        f"{em('gift')} <b>Юзернейм:</b> @{username_sale}\n"
        f"{em('money')} <b>Цена:</b> {price} ₽\n\n"
        f"{em('link')} <b>Продавец:</b> @{seller_username}\n\n"
        f"Нажми кнопку ниже, чтобы перейти в чат."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Написать продавцу",
            url=f"tg://user?id={seller_id}",
            icon_custom_emoji_id=EMOJI["write"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_market",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "back_to_market")
async def back_to_market_cb(callback: CallbackQuery):
    await callback.message.delete()
    fake_msg = callback.message
    fake_msg.from_user = callback.from_user
    await menu_market(fake_msg)
    await callback.answer()

# --- Жизненный цикл ---
@dp.startup()
async def on_startup():
    global http_session
    http_session = aiohttp.ClientSession()
    await init_db()
    logger.info("Бот запущен")

@dp.shutdown()
async def on_shutdown():
    if http_session:
        await http_session.close()
    logger.info("Бот остановлен")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
