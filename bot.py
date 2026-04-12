import asyncio
import os
import logging
import string
import random
import re
from datetime import datetime
from typing import Optional, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
import aiosqlite

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [7973988177]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ==================== ПРЕМИУМ ЭМОДЗИ ID ====================
EMOJI = {
    "gear": "5870982283724328568",
    "profile": "5870994129244131212",
    "people": "5870772616305839506",
    "verified_user": "5891207662678317861",
    "blocked_user": "5893192487324880883",
    "file": "5870528606328852614",
    "smile": "5870764288364252592",
    "chart_up": "5870930636742595124",
    "stats": "5870921681735781843",
    "home": "5873147866364514353",
    "lock": "6037249452824072506",
    "unlock": "6037496202990194718",
    "megaphone": "6039422865189638057",
    "check": "5870633910337015697",
    "cross": "5870657884844462243",
    "pencil": "5870676941614354370",
    "trash": "5870875489362513438",
    "arrow_down": "5893057118545646106",
    "paperclip": "6039451237743595514",
    "link": "5769289093221454192",
    "info": "6028435952299413210",
    "bot": "6030400221232501136",
    "eye": "6037397706505195857",
    "eye_hidden": "6037243349675544634",
    "send": "5963103826075456248",
    "download": "6039802767931871481",
    "bell": "6039486778597970865",
    "gift": "6032644646587338669",
    "clock": "5983150113483134607",
    "party": "6041731551845159060",
    "font": "5870801517140775623",
    "write": "5870753782874246579",
    "media": "6035128606563241721",
    "geo": "6042011682497106307",
    "wallet": "5769126056262898415",
    "box": "5884479287171485878",
    "crypto": "5260752406890711732",
    "calendar": "5890937706803894250",
    "tag": "5886285355279193209",
    "time_past": "5775896410780079073",
    "apps": "5778672437122045013",
    "brush": "6050679691004612757",
    "add_text": "5771851822897566479",
    "format": "5778479949572738874",
    "money": "5904462880941545555",
    "money_send": "5890848474563352982",
    "money_receive": "5879814368572478751",
    "code": "5940433880585605708",
    "loading": "5345906554510012647",
    "back": "5373141891321699086",
    "search": "5373141891321699086",
    "star": "5377406995950217044",
    "fire": "5377534647686860257",
}

# ==================== СОСТОЯНИЯ FSM ====================
class SearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()

class SellStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_price = State()

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = "vest_search.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                search_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                search_query TEXT,
                search_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS market_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                username TEXT UNIQUE,
                price INTEGER,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES users (user_id)
            )
        ''')
        
        await db.commit()

async def get_or_create_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        if username:
            await db.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (username, user_id)
            )
        await db.commit()
        
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        )
        return await cursor.fetchone()

async def increment_search_count(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET search_count = search_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def add_search_history(user_id: int, query: str, search_type: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO search_history (user_id, search_query, search_type) VALUES (?, ?, ?)",
            (user_id, query, search_type)
        )
        await db.commit()

async def get_user_searches(user_id: int) -> List:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT search_query, search_type, created_at FROM search_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
            (user_id,)
        )
        return await cursor.fetchall()

async def add_market_listing(seller_id: int, username: str, price: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO market_listings (seller_id, username, price) VALUES (?, ?, ?)",
                (seller_id, username, price)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def get_market_listings(page: int = 0, per_page: int = 5) -> List:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """SELECT m.id, m.username, m.price, u.username as seller_username, m.seller_id 
               FROM market_listings m 
               JOIN users u ON m.seller_id = u.user_id 
               WHERE m.is_active = 1 
               ORDER BY m.created_at DESC 
               LIMIT ? OFFSET ?""",
            (per_page, page * per_page)
        )
        return await cursor.fetchall()

async def get_market_count() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM market_listings WHERE is_active = 1")
        return (await cursor.fetchone())[0]

async def delete_market_listing(listing_id: int, seller_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE market_listings SET is_active = 0 WHERE id = ? AND seller_id = ?",
            (listing_id, seller_id)
        )
        await db.commit()
        return cursor.rowcount > 0

# ==================== ПРОВЕРКА ЮЗЕРНЕЙМОВ ====================
async def check_username_available(username: str) -> bool:
    try:
        username = username.strip().replace("@", "").lower()
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$', username):
            return False
        chat = await bot.get_chat(f"@{username}")
        return False
    except Exception as e:
        return "chat not found" in str(e).lower() or "bot was blocked" in str(e).lower()

def generate_random_username(length: int) -> str:
    first_char = random.choice(string.ascii_letters)
    rest_chars = ''.join(random.choices(string.ascii_letters + string.digits + "_", k=length-1))
    return first_char + rest_chars

def generate_username_with_keyword(keyword: str, length: int) -> str:
    keyword = keyword.lower().replace("@", "").replace(" ", "")
    remaining = length - len(keyword)
    
    if remaining < 0:
        return None
    elif remaining == 0:
        return keyword
    else:
        if random.choice([True, False]):
            prefix = ''.join(random.choices(string.ascii_letters + string.digits, k=remaining))
            return prefix + keyword
        else:
            suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=remaining))
            return keyword + suffix

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="Поиск"),
        KeyboardButton(text="Маркет"),
        KeyboardButton(text="Профиль")
    )
    builder.adjust(2, 1)
    kb = builder.export()
    kb.resize_keyboard = True
    return kb

def get_search_keyboard() -> InlineKeyboardMarkup:
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
            icon_custom_emoji_id=EMOJI["font"]
        )],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )],
    ])

def get_length_keyboard(keyword: str = None) -> InlineKeyboardMarkup:
    prefix = f"len_{keyword}_" if keyword else "len_"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{i} букв",
            callback_data=f"{prefix}{i}",
            icon_custom_emoji_id=EMOJI["format"]
        ) for i in range(4, 9)],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_search",
            icon_custom_emoji_id=EMOJI["back"]
        )],
    ])

def get_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="История поисков",
            callback_data="search_history",
            icon_custom_emoji_id=EMOJI["clock"]
        )],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )],
    ])

def get_market_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Все юзернеймы",
            callback_data="market_list",
            icon_custom_emoji_id=EMOJI["box"]
        )],
        [InlineKeyboardButton(
            text="Продать юзернейм",
            callback_data="sell_username",
            icon_custom_emoji_id=EMOJI["money"]
        )],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )],
    ])

def get_market_listings_keyboard(listings: List, page: int = 0, total: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    for listing in listings:
        listing_id, username, price, seller_username, seller_id = listing
        builder.row(InlineKeyboardButton(
            text=f"@{username} - {price} RUB",
            callback_data=f"view_listing_{listing_id}",
            icon_custom_emoji_id=EMOJI["tag"]
        ))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(
            text="Назад",
            callback_data=f"market_page_{page-1}",
            icon_custom_emoji_id=EMOJI["back"]
        ))
    if (page + 1) * 5 < total:
        nav_buttons.append(InlineKeyboardButton(
            text="Далее",
            callback_data=f"market_page_{page+1}",
            icon_custom_emoji_id=EMOJI["send"]
        ))
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.row(InlineKeyboardButton(
        text="Назад в маркет",
        callback_data="back_to_market",
        icon_custom_emoji_id=EMOJI["back"]
    ))
    
    return builder.as_markup()

def get_listing_view_keyboard(listing_id: int, seller_id: int, seller_username: str, current_user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.row(InlineKeyboardButton(
        text="Написать продавцу",
        url=f"https://t.me/{seller_username}",
        icon_custom_emoji_id=EMOJI["send"]
    ))
    
    if current_user_id == seller_id:
        builder.row(InlineKeyboardButton(
            text="Удалить объявление",
            callback_data=f"delete_listing_{listing_id}",
            icon_custom_emoji_id=EMOJI["trash"]
        ))
    
    builder.row(InlineKeyboardButton(
        text="Назад к списку",
        callback_data="market_list",
        icon_custom_emoji_id=EMOJI["back"]
    ))
    
    return builder.as_markup()

def get_back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="В главное меню",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["home"]
        )],
    ])

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    
    welcome_text = f'''<b><tg-emoji emoji-id="{EMOJI['bot']}">🤖</tg-emoji> Vest Search — поиск свободных юзернеймов</b>

<tg-emoji emoji-id="{EMOJI['search']}">🔍</tg-emoji> <b>Поиск</b> — ищи свободные юзернеймы
<tg-emoji emoji-id="{EMOJI['box']}">📦</tg-emoji> <b>Маркет</b> — покупай и продавай юзернеймы
<tg-emoji emoji-id="{EMOJI['profile']}">👤</tg-emoji> <b>Профиль</b> — статистика и история

<tg-emoji emoji-id="{EMOJI['info']}">ℹ</tg-emoji> <i>Выбери действие в меню</i>'''
    
    await message.answer(
        welcome_text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "Поиск")
async def search_menu(message: Message):
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["search"]}">🔍</tg-emoji> Выбери тип поиска</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=get_search_keyboard()
    )

@dp.message(F.text == "Маркет")
async def market_menu(message: Message):
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Маркет юзернеймов</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Здесь можно купить или продать юзернеймы',
        parse_mode=ParseMode.HTML,
        reply_markup=get_market_keyboard()
    )

@dp.message(F.text == "Профиль")
async def profile_menu(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    
    if user:
        user_id, username, search_count, created_at = user
        text = f'''<b><tg-emoji emoji-id="{EMOJI["profile"]}">👤</tg-emoji> Твой профиль</b>

<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> <b>Юзернейм:</b> @{username or "Не указан"}
<tg-emoji emoji-id="{EMOJI["search"]}">🔍</tg-emoji> <b>Поисков:</b> {search_count}
<tg-emoji emoji-id="{EMOJI["calendar"]}">📅</tg-emoji> <b>Дата регистрации:</b> {created_at[:10]}'''
        
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_profile_keyboard()
        )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["home"]}">🏘</tg-emoji> Главное меню</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_search")
async def back_to_search(callback: CallbackQuery):
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["search"]}">🔍</tg-emoji> Выбери тип поиска</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=get_search_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_market")
async def back_to_market(callback: CallbackQuery):
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Маркет юзернеймов</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Здесь можно купить или продать юзернеймы',
        parse_mode=ParseMode.HTML,
        reply_markup=get_market_keyboard()
    )
    await callback.answer()

# ==================== ПОИСК ====================
@dp.callback_query(F.data.startswith("search_len_"))
async def search_by_length(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["loading"]}">🔄</tg-emoji> Ищу свободные юзернеймы ({length} букв)...</b>',
        parse_mode=ParseMode.HTML
    )
    
    found = []
    attempts = 0
    max_attempts = 50
    
    while len(found) < 5 and attempts < max_attempts:
        username = generate_random_username(length)
        if await check_username_available(username):
            found.append(username)
        attempts += 1
        await asyncio.sleep(0.3)
    
    await increment_search_count(callback.from_user.id)
    await add_search_history(callback.from_user.id, f"Длина: {length}", "by_length")
    
    if found:
        text = f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Найдены свободные юзернеймы ({length} букв):</b>\n\n'
        for i, username in enumerate(found, 1):
            text += f'<tg-emoji emoji-id="{EMOJI["unlock"]}">🔓</tg-emoji> <code>@{username}</code>\n'
        text += f'\n<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Проверено {attempts} вариантов</i>'
    else:
        text = f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Свободных юзернеймов не найдено</b>\n\n<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Проверено {attempts} вариантов. Попробуй другую длину</i>'
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_to_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["font"]}">🔗</tg-emoji> Введи ключевое слово</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Только латинские буквы, цифры и _</i>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Отмена",
                callback_data="back_to_search",
                icon_custom_emoji_id=EMOJI["cross"]
            )]
        ])
    )
    await state.set_state(SearchStates.waiting_for_keyword)
    await callback.answer()

@dp.message(SearchStates.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    keyword = message.text.strip().lower().replace("@", "").replace(" ", "")
    
    if not re.match(r'^[a-zA-Z0-9_]+$', keyword):
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Недопустимые символы!</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> Используй только латинские буквы, цифры и _',
            parse_mode=ParseMode.HTML
        )
        return
    
    if len(keyword) > 20:
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Слишком длинное слово!</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> Максимум 20 символов',
            parse_mode=ParseMode.HTML
        )
        return
    
    await state.update_data(keyword=keyword)
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Ключевое слово:</b> <code>{keyword}</code>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["format"]}">↔</tg-emoji> <b>Выбери длину юзернейма</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=get_length_keyboard(keyword)
    )
    await state.set_state(SearchStates.waiting_for_length)

@dp.callback_query(SearchStates.waiting_for_length, F.data.startswith("len_"))
async def search_with_keyword_and_length(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    
    if len(parts) == 3 and parts[1] != "":
        keyword = parts[1]
        length = int(parts[2])
    else:
        data = await state.get_data()
        keyword = data.get("keyword", "")
        length = int(parts[1])
    
    if not keyword:
        await callback.answer("Ошибка: ключевое слово не найдено")
        return
    
    if length < len(keyword):
        await callback.answer(f"Длина не может быть меньше длины слова ({len(keyword)})", show_alert=True)
        return
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["loading"]}">🔄</tg-emoji> Ищу юзернеймы с "{keyword}" ({length} букв)...</b>',
        parse_mode=ParseMode.HTML
    )
    
    found = []
    attempts = 0
    max_attempts = 50
    
    while len(found) < 5 and attempts < max_attempts:
        username = generate_username_with_keyword(keyword, length)
        if username and await check_username_available(username):
            found.append(username)
        attempts += 1
        await asyncio.sleep(0.3)
    
    await increment_search_count(callback.from_user.id)
    await add_search_history(callback.from_user.id, f"Ключ: {keyword}, длина: {length}", "by_keyword")
    
    if found:
        text = f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Найдены юзернеймы с "{keyword}" ({length} букв):</b>\n\n'
        for i, username in enumerate(found, 1):
            text += f'<tg-emoji emoji-id="{EMOJI["unlock"]}">🔓</tg-emoji> <code>@{username}</code>\n'
        text += f'\n<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Проверено {attempts} вариантов</i>'
    else:
        text = f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Свободных юзернеймов с "{keyword}" не найдено</b>\n\n<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Проверено {attempts} вариантов</i>'
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_to_main_keyboard()
    )
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "search_history")
async def show_search_history(callback: CallbackQuery):
    searches = await get_user_searches(callback.from_user.id)
    
    if searches:
        text = f'<b><tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> Последние поиски:</b>\n\n'
        for query, search_type, created_at in searches[:10]:
            emoji_id = EMOJI["font"] if "Ключ" in search_type else EMOJI["search"]
            text += f'<tg-emoji emoji-id="{emoji_id}">🔍</tg-emoji> {query}\n'
    else:
        text = f'<b><tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> История поисков пуста</b>'
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_to_main_keyboard()
    )
    await callback.answer()

# ==================== МАРКЕТ ====================
@dp.callback_query(F.data == "market_list")
async def show_market_list(callback: CallbackQuery):
    await show_market_page(callback, 0)

@dp.callback_query(F.data.startswith("market_page_"))
async def show_market_page(callback: CallbackQuery, page: int = None):
    if page is None:
        page = int(callback.data.split("_")[-1])
    
    listings = await get_market_listings(page)
    total = await get_market_count()
    
    if listings:
        text = f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Юзернеймы на продажу</b>\n\n'
        text += f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Страница {page + 1}</i>\n\n'
        text += '<tg-emoji emoji-id="' + EMOJI["tag"] + '">🏷</tg-emoji> <b>Выбери юзернейм:</b>'
        
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_market_listings_keyboard(listings, page, total)
        )
    else:
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> Нет активных объявлений</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["money"]}">🪙</tg-emoji> <i>Стань первым продавцом!</i>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_market_keyboard()
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_listing_"))
async def view_listing(callback: CallbackQuery):
    listing_id = int(callback.data.split("_")[-1])
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """SELECT m.id, m.username, m.price, u.username, m.seller_id, m.created_at 
               FROM market_listings m 
               JOIN users u ON m.seller_id = u.user_id 
               WHERE m.id = ? AND m.is_active = 1""",
            (listing_id,)
        )
        listing = await cursor.fetchone()
    
    if listing:
        listing_id, username, price, seller_username, seller_id, created_at = listing
        
        text = f'''<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Юзернейм на продажу</b>

<tg-emoji emoji-id="{EMOJI["link"]}">🔗</tg-emoji> <b>Юзернейм:</b> <code>@{username}</code>
<tg-emoji emoji-id="{EMOJI["money"]}">🪙</tg-emoji> <b>Цена:</b> {price} RUB
<tg-emoji emoji-id="{EMOJI["profile"]}">👤</tg-emoji> <b>Продавец:</b> @{seller_username}
<tg-emoji emoji-id="{EMOJI["calendar"]}">📅</tg-emoji> <b>Опубликовано:</b> {created_at[:10]}'''
        
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_listing_view_keyboard(listing_id, seller_id, seller_username, callback.from_user.id)
        )
    else:
        await callback.answer("Объявление не найдено или удалено", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_listing_"))
async def delete_listing(callback: CallbackQuery):
    listing_id = int(callback.data.split("_")[-1])
    
    success = await delete_market_listing(listing_id, callback.from_user.id)
    
    if success:
        await callback.answer("Объявление удалено", show_alert=True)
        await show_market_list(callback)
    else:
        await callback.answer("Не удалось удалить объявление", show_alert=True)

@dp.callback_query(F.data == "sell_username")
async def sell_username_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["money"]}">🪙</tg-emoji> Продажа юзернейма</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["write"]}">✍</tg-emoji> <b>Введи юзернейм для продажи</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Пример: username (без @)</i>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Отмена",
                callback_data="back_to_market",
                icon_custom_emoji_id=EMOJI["cross"]
            )]
        ])
    )
    await state.set_state(SellStates.waiting_for_username)
    await callback.answer()

@dp.message(SellStates.waiting_for_username)
async def process_sell_username(message: Message, state: FSMContext):
    username = message.text.strip().replace("@", "").lower()
    
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$', username):
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Неверный формат юзернейма!</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> Используй только латинские буквы, цифры и _',
            parse_mode=ParseMode.HTML
        )
        return
    
    await state.update_data(username=username)
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Юзернейм:</b> <code>@{username}</code>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["money"]}">🪙</tg-emoji> <b>Введи цену в RUB</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["info"]}">ℹ</tg-emoji> <i>Только целое число</i>',
        parse_mode=ParseMode.HTML
    )
    await state.set_state(SellStates.waiting_for_price)

@dp.message(SellStates.waiting_for_price)
async def process_sell_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError()
    except ValueError:
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Введи корректную цену (целое положительное число)!</b>',
            parse_mode=ParseMode.HTML
        )
        return
    
    data = await state.get_data()
    username = data.get("username")
    
    success = await add_market_listing(message.from_user.id, username, price)
    
    if success:
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Объявление создано!</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> <b>Юзернейм:</b> <code>@{username}</code>\n'
            f'<tg-emoji emoji-id="{EMOJI["money"]}">🪙</tg-emoji> <b>Цена:</b> {price} RUB',
            parse_mode=ParseMode.HTML,
            reply_markup=get_market_keyboard()
        )
    else:
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> Этот юзернейм уже выставлен на продажу!</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_market_keyboard()
        )
    
    await state.clear()

# ==================== ЗАПУСК ====================
async def main():
    await init_db()
    logger.info("База данных инициализирована")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
