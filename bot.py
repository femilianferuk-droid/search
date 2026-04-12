import asyncio
import logging
import os
import json
import random
import string
from typing import Optional, List
from datetime import datetime, timedelta

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
    ReplyKeyboardMarkup,
    KeyboardButton,
)
import aiosqlite

from telethon import TelegramClient
from telethon.errors import (
    UsernameNotOccupiedError, 
    UsernameOccupiedError, 
    FloodWaitError,
    SessionPasswordNeededError
)
from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest as ChannelUpdateUsernameRequest
from telethon.sessions import StringSession

# --- Загрузка переменных окружения ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_ID = 7973988177

if not BOT_TOKEN:
    raise ValueError("Не указан BOT_TOKEN в .env файле")

# --- Настройки ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

http_session: Optional[aiohttp.ClientSession] = None

# Telethon клиенты
telethon_clients: List[TelegramClient] = []
current_client_index = 0

# --- База данных ---
DB_PATH = "vest_search.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                searches_count INTEGER DEFAULT 0,
                found_count INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                last_searches TEXT DEFAULT '[]',
                registered_date TEXT
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telethon_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                session_string TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_by INTEGER,
                added_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reserved_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                channel_link TEXT,
                reserved_until TEXT,
                created_date TEXT
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
                """INSERT INTO users 
                   (user_id, username, registered_date) 
                   VALUES (?, ?, ?)""",
                (user_id, username, datetime.now().isoformat())
            )
            await db.commit()
        elif username and user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()

async def increment_search(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET searches_count = searches_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def add_found_nick(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET found_count = found_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def add_last_search(user_id: int, query: str):
    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(user_id)
        if user:
            last_searches = json.loads(user[5] or "[]")
            last_searches.insert(0, {"query": query, "time": datetime.now().isoformat()})
            last_searches = last_searches[:5]
            await db.execute(
                "UPDATE users SET last_searches = ? WHERE user_id = ?",
                (json.dumps(last_searches), user_id)
            )
            await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return [row[0] for row in await cursor.fetchall()]

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT SUM(searches_count) FROM users") as cursor:
            total_searches = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT SUM(found_count) FROM users") as cursor:
            total_found = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(*) FROM market WHERE is_active = 1") as cursor:
            active_listings = (await cursor.fetchone())[0]
    return total_users, total_searches, total_found, active_listings

async def save_reserved_channel(user_id: int, username: str, channel_link: str):
    async with aiosqlite.connect(DB_PATH) as db:
        reserved_until = (datetime.now() + timedelta(days=7)).isoformat()
        await db.execute(
            """INSERT INTO reserved_channels 
               (user_id, username, channel_link, reserved_until, created_date) 
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, channel_link, reserved_until, datetime.now().isoformat())
        )
        await db.commit()

# --- Загрузка Telethon аккаунтов ---
async def load_telethon_accounts():
    global telethon_clients
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, session_string FROM telethon_accounts WHERE is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
    
    for phone, session_string in rows:
        if session_string:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    telethon_clients.append(client)
                    logger.info(f"✅ Аккаунт {phone} загружен")
                else:
                    await client.disconnect()
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки {phone}: {e}")

async def save_telethon_account(phone: str, session_string: str, added_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO telethon_accounts 
               (phone, session_string, added_by, added_date) 
               VALUES (?, ?, ?, ?)""",
            (phone, session_string, added_by, datetime.now().isoformat())
        )
        await db.commit()

def get_next_client() -> Optional[TelegramClient]:
    global current_client_index
    if not telethon_clients:
        return None
    client = telethon_clients[current_client_index]
    current_client_index = (current_client_index + 1) % len(telethon_clients)
    return client

# --- Состояния ---
class SearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()

class SellStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_price = State()

class AdminStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()
    waiting_for_broadcast = State()

class AutoReserveStates(StatesGroup):
    waiting_for_channels_count = State()

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
    "admin": "5870982283724328568",
    "add": "5870633910337015697",
    "phone": "6039450962865688331",
    "broadcast": "6039422865189638057",
    "stats": "5870921681735781843",
    "auto": "6030400221232501136",
    "key": "6037249452824072506",
}

def em(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(name, EMOJI["check"])}">👍</tg-emoji>'

# --- Главное меню ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔍 Поиск"),
                KeyboardButton(text="🤖 Автозанятие")
            ],
            [
                KeyboardButton(text="📦 Маркет"),
                KeyboardButton(text="👤 Профиль")
            ]
        ],
        resize_keyboard=True
    )

# --- Проверка юзернейма ---
async def check_username_real(username: str) -> bool:
    client = get_next_client()
    if not client:
        return False
    
    try:
        result = await client(CheckUsernameRequest(username))
        return result
    except UsernameOccupiedError:
        return False
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds, 5))
        return False
    except:
        return False

async def check_many_usernames(usernames: List[str], max_workers: int = 3) -> List[str]:
    found = []
    for i in range(0, len(usernames), max_workers):
        batch = usernames[i:i + max_workers]
        tasks = [check_username_real(u) for u in batch]
        results = await asyncio.gather(*tasks)
        
        for username, is_free in zip(batch, results):
            if is_free:
                found.append(username)
        
        await asyncio.sleep(0.5)
    
    return found

# --- Создание канала и занятие юзернейма ---
async def create_channel_with_username(client: TelegramClient, username: str, title: str) -> Optional[str]:
    try:
        # Создаём приватный канал
        result = await client(CreateChannelRequest(
            title=title,
            about=f"Зарезервировано через Vest Search. Будет передано через 7 дней.",
            megagroup=False
        ))
        
        channel = result.chats[0]
        
        # Устанавливаем юзернейм
        await client(ChannelUpdateUsernameRequest(channel, username))
        
        # Ссылка на канал
        channel_link = f"https://t.me/{username}"
        
        return channel_link
    except FloodWaitError as e:
        logger.warning(f"Flood wait при создании канала: {e.seconds} сек")
        await asyncio.sleep(min(e.seconds, 30))
        return None
    except Exception as e:
        logger.error(f"Ошибка создания канала {username}: {e}")
        return None

# --- Генерация юзернеймов ---
def generate_random_usernames(length: int, count: int = 20) -> List[str]:
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    while len(variants) < count:
        username = ''.join(random.choices(letters, k=length))
        variants.add(username)
    
    return list(variants)

def generate_with_keyword(keyword: str, length: int, count: int = 20) -> List[str]:
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    if len(keyword) >= length:
        return [keyword[:length]]
    
    remaining = length - len(keyword)
    
    while len(variants) < count:
        if random.choice([True, False]):
            suffix = ''.join(random.choices(letters, k=remaining))
            variants.add(keyword + suffix)
        else:
            prefix = ''.join(random.choices(letters, k=remaining))
            variants.add(prefix + keyword)
    
    return list(variants)

# --- Инлайн клавиатуры ---
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

def get_auto_reserve_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="5 букв",
            callback_data="autoreserve_len_5",
            icon_custom_emoji_id=EMOJI["auto"]
        )],
        [InlineKeyboardButton(
            text="6 букв",
            callback_data="autoreserve_len_6",
            icon_custom_emoji_id=EMOJI["auto"]
        )],
        [InlineKeyboardButton(
            text="С ключевым словом",
            callback_data="autoreserve_keyword",
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

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить аккаунт",
            callback_data="admin_add_account",
            icon_custom_emoji_id=EMOJI["add"]
        )],
        [InlineKeyboardButton(
            text="📋 Список аккаунтов",
            callback_data="admin_list_accounts",
            icon_custom_emoji_id=EMOJI["eye"]
        )],
        [InlineKeyboardButton(
            text="📊 Статистика",
            callback_data="admin_stats",
            icon_custom_emoji_id=EMOJI["stats"]
        )],
        [InlineKeyboardButton(
            text="📢 Рассылка",
            callback_data="admin_broadcast",
            icon_custom_emoji_id=EMOJI["broadcast"]
        )],
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
        f"{em('info')} Бот для поиска и автоматического занятия юзернеймов Telegram.\n\n"
        f"Используй меню для навигации.",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer(f"{em('cross')} Нет доступа!")
        return
    
    await message.answer(
        f"{em('admin')} <b>Админ-панель</b>\n\n"
        f"Активных аккаунтов: {len(telethon_clients)}",
        reply_markup=get_admin_keyboard()
    )

@router.message(F.text == "🔍 Поиск")
async def menu_search(message: Message):
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == "🤖 Автозанятие")
async def menu_autoreserve(message: Message):
    if not telethon_clients:
        await message.answer(
            f"{em('cross')} Нет активных аккаунтов для автозанятия!",
            reply_markup=get_main_keyboard()
        )
        return
    
    await message.answer(
        f"{em('auto')} <b>Автоматическое занятие юзернеймов</b>\n\n"
        f"Бот создаст каналы с твоим именем и займёт свободные юзернеймы.\n"
        f"{em('info')} Через 7 дней каналы будут переданы тебе.\n\n"
        f"Выбери тип поиска:",
        reply_markup=get_auto_reserve_type_keyboard()
    )

@router.message(F.text == "👤 Профиль")
async def menu_profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    text = (
        f"{em('profile')} <b>Vest Search</b>\n\n"
        f"{em('link')} Юзернейм: @{user[1] or 'не указан'}\n"
        f"{em('graph')} Всего поисков: {user[2]}\n"
        f"{em('check')} Найдено ников: {user[3]}\n"
        f"{em('people')} Рефералов: {user[4]}\n\n"
        f"{em('clock')} <b>Последние поиски:</b>\n"
    )
    
    last_searches = json.loads(user[5] or "[]")
    if last_searches:
        for s in last_searches[:5]:
            text += f"• {s['query']}\n"
    else:
        text += "Пусто"
    
    await message.answer(text, reply_markup=get_main_keyboard())

@router.message(F.text == "📦 Маркет")
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
        text += f"@{name} — {price} ₽\n"
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
        f"{em('bot')} <b>Vest Search</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# --- Поиск ---
@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    
    if not telethon_clients:
        await callback.answer("❌ Нет активных аккаунтов для поиска!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу свободные юзернеймы из {length} букв...</b>",
        reply_markup=get_back_button()
    )
    
    await increment_search(callback.from_user.id)
    await add_last_search(callback.from_user.id, f"{length} букв")
    
    variants = generate_random_usernames(length, 20)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(callback.from_user.id)
        text = f"{em('check')} <b>Найдены свободные юзернеймы ({length} букв):</b>\n\n"
        for u in found[:5]:
            text += f"• @{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено</b>"
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    if not telethon_clients:
        await callback.answer("❌ Нет активных аккаунтов для поиска!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('pencil')} <b>Введи ключевое слово (латиница):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(SearchStates.waiting_for_keyword)
    await callback.answer()

@router.message(SearchStates.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    keyword = message.text.strip().lower()
    
    if not all(c.isalpha() for c in keyword):
        await message.answer(f"{em('cross')} Используй только латинские буквы")
        await state.clear()
        return
    
    await state.update_data(keyword=keyword)
    await message.answer(
        f"{em('pencil')} <b>Укажи длину (от 5 до 32):</b>",
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
    
    await message.answer(f"{em('loading')} <b>Ищу с '{keyword}'...</b>")
    
    await increment_search(message.from_user.id)
    await add_last_search(message.from_user.id, f"'{keyword}' ({length})")
    
    variants = generate_with_keyword(keyword, length, 20)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(message.from_user.id)
        text = f"{em('check')} <b>Найдено с '{keyword}':</b>\n\n"
        for u in found[:5]:
            text += f"• @{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено.</b>"
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Автозанятие ---
@router.callback_query(F.data.startswith("autoreserve_"))
async def autoreserve_handler(callback: CallbackQuery, state: FSMContext):
    if not telethon_clients:
        await callback.answer("❌ Нет активных аккаунтов!", show_alert=True)
        return
    
    data_type = callback.data.replace("autoreserve_", "")
    
    if data_type == "len_5":
        await state.update_data(reserve_type="len", reserve_value=5)
    elif data_type == "len_6":
        await state.update_data(reserve_type="len", reserve_value=6)
    else:  # keyword
        await state.update_data(reserve_type="keyword", reserve_value=None)
        await callback.message.edit_text(
            f"{em('pencil')} <b>Введи ключевое слово:</b>",
            reply_markup=get_back_button()
        )
        await state.set_state(AutoReserveStates.waiting_for_channels_count)
        await callback.answer()
        return
    
    await callback.message.edit_text(
        f"{em('auto')} <b>Укажи количество каналов (от 1 до 10):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AutoReserveStates.waiting_for_channels_count)
    await callback.answer()

@router.message(AutoReserveStates.waiting_for_channels_count)
async def autoreserve_count_entered(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число от 1 до 10.")
        return
    
    count = int(message.text)
    if not (1 <= count <= 10):
        await message.answer(f"{em('cross')} Количество должно быть от 1 до 10.")
        return
    
    data = await state.get_data()
    reserve_type = data.get('reserve_type')
    reserve_value = data.get('reserve_value')
    
    # Определяем параметры генерации
    if reserve_type == "len":
        length = reserve_value
        variants = generate_random_usernames(length, count * 3)
    else:
        # Если это keyword, reserve_value ещё не установлен
        keyword = reserve_value
        length = 6  # по умолчанию
        variants = generate_with_keyword(keyword, length, count * 3)
    
    # Проверяем свободные
    found = await check_many_usernames(variants)
    
    if len(found) < count:
        await message.answer(
            f"{em('cross')} Найдено только {len(found)} свободных юзернеймов. Нужно {count}.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    # Берём нужное количество
    to_reserve = found[:count]
    
    await message.answer(
        f"{em('loading')} <b>Начинаю занятие {count} юзернеймов...</b>\n"
        f"{em('info')} Задержка между созданием: 15 секунд\n"
        f"Каналы будут названы: {message.from_user.full_name}"
    )
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    title = message.from_user.full_name or message.from_user.username or "Vest Search User"
    created = []
    
    for i, username in enumerate(to_reserve, 1):
        status_msg = await message.answer(
            f"{em('loading')} Создаю канал {i}/{count} для @{username}..."
        )
        
        channel_link = await create_channel_with_username(client, username, title)
        
        if channel_link:
            await save_reserved_channel(message.from_user.id, username, channel_link)
            created.append((username, channel_link))
            await status_msg.edit_text(
                f"{em('check')} Канал {i}/{count} создан: {channel_link}"
            )
        else:
            await status_msg.edit_text(
                f"{em('cross')} Ошибка создания @{username}"
            )
        
        if i < count:
            await asyncio.sleep(15)
    
    if created:
        text = f"{em('check')} <b>Занято {len(created)} юзернеймов!</b>\n\n"
        for u, link in created:
            text += f"• @{u} → {link}\n"
        text += f"\n{em('info')} Каналы будут переданы тебе через 7 дней."
    else:
        text = f"{em('cross')} Не удалось занять ни одного юзернейма."
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Админ-панель: Добавление аккаунта с 2FA ---
@router.callback_query(F.data == "admin_add_account")
async def admin_add_account(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('phone')} <b>Введи номер телефона:</b>\n+79123456789",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_phone)
    await callback.answer()

@router.message(AdminStates.waiting_for_phone)
async def admin_process_phone(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    
    phone = message.text.strip()
    
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent = await client.send_code_request(phone)
        await state.update_data(
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            client_session=client.session.save()
        )
        
        await message.answer(
            f"{em('pencil')} <b>Введи код из Telegram:</b>\n\n"
            f"Если есть 2FA, введи код, а затем пароль.",
            reply_markup=get_back_button()
        )
        await state.set_state(AdminStates.waiting_for_code)
    except Exception as e:
        await message.answer(f"{em('cross')} Ошибка: {e}")
        await state.clear()

@router.message(AdminStates.waiting_for_code)
async def admin_process_code(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    
    code = message.text.strip()
    data = await state.get_data()
    
    client = TelegramClient(StringSession(data['client_session']), API_ID, API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(
            phone=data['phone'],
            code=code,
            phone_code_hash=data['phone_code_hash']
        )
        
        # Успешный вход без 2FA
        session_string = client.session.save()
        await save_telethon_account(data['phone'], session_string, ADMIN_ID)
        
        new_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await new_client.connect()
        telethon_clients.append(new_client)
        
        await message.answer(
            f"{em('check')} <b>Аккаунт {data['phone']} добавлен!</b>",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        
    except SessionPasswordNeededError:
        # Требуется 2FA
        await state.update_data(client_session=client.session.save())
        await message.answer(
            f"{em('key')} <b>Введи пароль 2FA:</b>",
            reply_markup=get_back_button()
        )
        await state.set_state(AdminStates.waiting_for_2fa)
        
    except Exception as e:
        await message.answer(f"{em('cross')} Ошибка: {e}")
        await state.clear()

@router.message(AdminStates.waiting_for_2fa)
async def admin_process_2fa(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    
    password = message.text.strip()
    data = await state.get_data()
    
    client = TelegramClient(StringSession(data['client_session']), API_ID, API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(password=password)
        
        session_string = client.session.save()
        await save_telethon_account(data['phone'], session_string, ADMIN_ID)
        
        new_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await new_client.connect()
        telethon_clients.append(new_client)
        
        await message.answer(
            f"{em('check')} <b>Аккаунт {data['phone']} добавлен!</b>",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        await message.answer(f"{em('cross')} Ошибка: неверный пароль 2FA")
    
    await state.clear()

@router.callback_query(F.data == "admin_list_accounts")
async def admin_list_accounts(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, is_active, added_date FROM telethon_accounts"
        ) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        text = f"{em('info')} Нет добавленных аккаунтов"
    else:
        text = f"{em('admin')} <b>Список аккаунтов:</b>\n\n"
        for phone, is_active, date in rows:
            status = "✅" if is_active else "❌"
            text += f"{status} {phone}\n"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    total_users, total_searches, total_found, active_listings = await get_stats()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM reserved_channels") as cursor:
            reserved_count = (await cursor.fetchone())[0]
    
    text = (
        f"{em('stats')} <b>Статистика Vest Search</b>\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🔍 Всего поисков: {total_searches}\n"
        f"✅ Найдено ников: {total_found}\n"
        f"📦 Лотов в маркете: {active_listings}\n"
        f"🤖 Аккаунтов: {len(telethon_clients)}\n"
        f"🔒 Занято каналов: {reserved_count}"
    )
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('broadcast')} <b>Отправь сообщение для рассылки:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_process(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    
    users = await get_all_users()
    
    success = 0
    failed = 0
    
    await message.answer(f"{em('loading')} <b>Рассылка на {len(users)} пользователей...</b>")
    
    for user_id in users:
        try:
            await message.copy_to(user_id)
            success += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    
    await message.answer(
        f"{em('check')} <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно: {success}\n❌ Не доставлено: {failed}"
    )
    await state.clear()

# --- Маркет ---
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
        await message.answer(f"{em('cross')} Некорректный формат.")
        return
    
    is_free = await check_username_real(username)
    if is_free:
        await message.answer(
            f"{em('cross')} Этот юзернейм свободен! Продавать можно только существующие."
        )
        await state.clear()
        return
    
    await state.update_data(sale_username=username)
    await message.answer(f"{em('money')} <b>Введи цену в рублях:</b>")
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
            await message.answer(f"{em('cross')} Этот юзернейм уже в маркете.")
            await state.clear()
            return
    
    await message.answer(
        f"{em('check')} <b>Объявление создано!</b>\n\n@{username_sale} — {price} ₽"
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
        f"{em('link')} <b>Продавец:</b> @{seller_username}"
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
    await load_telethon_accounts()
    logger.info(f"✅ Vest Search запущен, аккаунтов: {len(telethon_clients)}")

@dp.shutdown()
async def on_shutdown():
    if http_session:
        await http_session.close()
    for client in telethon_clients:
        await client.disconnect()
    logger.info("Бот остановлен")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
