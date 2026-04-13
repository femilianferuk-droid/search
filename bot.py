import asyncio
import logging
import os
import json
import random
import string
import uuid
import re
from typing import Optional, List, Dict, Tuple
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
    SessionPasswordNeededError,
    ChannelPrivateError,
    ChannelInvalidError,
    InviteHashExpiredError,
    InviteHashInvalidError
)
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    UpdateUsernameRequest,
    InviteToChannelRequest,
    EditAdminRequest,
    GetParticipantRequest,
    JoinChannelRequest
)
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import ChatAdminRights, ChannelParticipantCreator, ChannelParticipantAdmin
from telethon.sessions import StringSession

# --- Загрузка переменных окружения ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
ADMIN_ID = 7973988177
CRYPTO_BOT_TOKEN = "499354:AATdkiDyuC1tWd1ro5S5wFw6XcePNUNH5Ph"
SUPPORT_USERNAME = "VestSupport"

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
                balance REAL DEFAULT 0,
                searches_count INTEGER DEFAULT 0,
                found_count INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                last_searches TEXT DEFAULT '[]',
                registered_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS market_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                seller_username TEXT,
                channel_username TEXT UNIQUE,
                price REAL,
                status TEXT DEFAULT 'pending',
                bot_joined BOOLEAN DEFAULT 0,
                owner_verified BOOLEAN DEFAULT 0,
                buyer_id INTEGER,
                bot_account_username TEXT,
                created_date TEXT,
                sold_date TEXT
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crypto_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                invoice_id TEXT UNIQUE,
                amount REAL,
                status TEXT,
                created_date TEXT,
                paid_date TEXT
            )
        """)
        await db.commit()

# --- Работа с пользователями ---
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

async def get_user_balance(user_id: int) -> float:
    user = await get_user(user_id)
    return user[2] if user else 0

async def add_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()

async def subtract_balance(user_id: int, amount: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(user_id)
        if user and user[2] >= amount:
            await db.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (amount, user_id)
            )
            await db.commit()
            return True
        return False

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
            last_searches = json.loads(user[6] or "[]")
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
        async with db.execute("SELECT COUNT(*) FROM market_listings WHERE status = 'active'") as cursor:
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

# --- Crypto Bot API ---
async def create_crypto_invoice(user_id: int, amount_rub: float) -> Optional[Dict]:
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    
    amount_usdt = round(amount_rub / 90, 2)
    invoice_id = str(uuid.uuid4())
    
    data = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": f"Пополнение баланса Vest Search на {amount_rub}₽",
        "hidden_message": f"user_{user_id}",
        "paid_btn_name": "callback",
        "paid_btn_url": "https://t.me/VestSearchBot",
        "payload": invoice_id,
        "allow_comments": False,
        "allow_anonymous": False
    }
    
    try:
        async with http_session.post(url, headers=headers, json=data) as resp:
            result = await resp.json()
            if result.get("ok"):
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO crypto_payments (user_id, invoice_id, amount, status, created_date) VALUES (?, ?, ?, ?, ?)",
                        (user_id, invoice_id, amount_rub, "pending", datetime.now().isoformat())
                    )
                    await db.commit()
                return {
                    "invoice_id": invoice_id,
                    "pay_url": result["result"]["pay_url"],
                    "amount_usdt": amount_usdt
                }
            return None
    except Exception as e:
        logger.error(f"Crypto Bot error: {e}")
        return None

async def check_crypto_payment(invoice_id: str) -> bool:
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": invoice_id}
    
    try:
        async with http_session.get(url, headers=headers, params=params) as resp:
            result = await resp.json()
            if result.get("ok") and result["result"]["items"]:
                invoice = result["result"]["items"][0]
                return invoice["status"] == "paid"
            return False
    except:
        return False

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

async def get_bot_username(client: TelegramClient) -> str:
    me = await client.get_me()
    return me.username or f"id{me.id}"

# --- Вступление в канал по ссылке ---
def extract_invite_hash(link: str) -> Optional[str]:
    """Извлекает хеш из ссылки-приглашения"""
    patterns = [
        r't\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)',
        r'telegram\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)',
        r'https?://t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            return match.group(1)
    return None

async def join_channel_by_link(client: TelegramClient, invite_link: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Вступает в канал по ссылке-приглашению.
    Возвращает: (успех, юзернейм_канала, ошибка)
    """
    invite_hash = extract_invite_hash(invite_link)
    if not invite_hash:
        return False, None, "Неверный формат ссылки"
    
    try:
        # Сначала проверяем ссылку
        chat_invite = await client(CheckChatInviteRequest(invite_hash))
        
        # Вступаем в канал
        updates = await client(ImportChatInviteRequest(invite_hash))
        
        # Получаем информацию о канале
        if updates.chats:
            channel = updates.chats[0]
            channel_username = getattr(channel, 'username', None)
            return True, channel_username, None
        else:
            return False, None, "Не удалось получить информацию о канале"
            
    except InviteHashExpiredError:
        return False, None, "Ссылка-приглашение истекла"
    except InviteHashInvalidError:
        return False, None, "Неверная ссылка-приглашение"
    except FloodWaitError as e:
        return False, None, f"Слишком много попыток, подождите {e.seconds} сек"
    except Exception as e:
        logger.error(f"Ошибка вступления в канал: {e}")
        return False, None, f"Ошибка: {str(e)[:100]}"

# --- Проверка владельца канала ---
async def check_channel_owner(client: TelegramClient, channel_username: str) -> bool:
    """Проверяет, является ли аккаунт администратором канала"""
    try:
        channel = await client.get_entity(f"@{channel_username}")
        me = await client.get_me()
        
        participant = await client(GetParticipantRequest(channel, me))
        
        if isinstance(participant.participant, (ChannelParticipantCreator, ChannelParticipantAdmin)):
            if isinstance(participant.participant, ChannelParticipantCreator):
                return True
            admin_rights = participant.participant.admin_rights
            if admin_rights and admin_rights.post_messages:
                return True
        
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки владельца {channel_username}: {e}")
        return False

# --- Состояния ---
class SearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()

class MarketSellStates(StatesGroup):
    waiting_for_price = State()
    waiting_for_invite_link = State()
    waiting_for_transfer = State()

class AdminStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()
    waiting_for_broadcast = State()

class AutoReserveStates(StatesGroup):
    waiting_for_channels_count = State()

class BalanceStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_payment = State()

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
    "wallet": "5769126056262898415",
    "buy": "5904462880941545555",
    "sell": "5890848474563352982",
    "support": "6039450962865688331",
    "owner": "5891207662678317861",
    "transfer": "5890848474563352982",
    "invite": "5769289093221454192",
}

def em(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(name, EMOJI["check"])}">👍</tg-emoji>'

# --- Главное меню ТОЛЬКО с премиум эмодзи ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Поиск", icon_custom_emoji_id=EMOJI["search"]),
                KeyboardButton(text="Автозанятие", icon_custom_emoji_id=EMOJI["auto"])
            ],
            [
                KeyboardButton(text="Маркет", icon_custom_emoji_id=EMOJI["market"]),
                KeyboardButton(text="Профиль", icon_custom_emoji_id=EMOJI["profile"])
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

# --- Создание канала ---
async def create_channel_with_username(client: TelegramClient, username: str, title: str) -> Optional[str]:
    try:
        result = await client(CreateChannelRequest(
            title=title,
            about=f"Зарезервировано через Vest Search",
            megagroup=False
        ))
        
        channel = result.chats[0]
        await client(UpdateUsernameRequest(channel, username))
        
        return f"https://t.me/{username}"
    except FloodWaitError as e:
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

def get_market_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Купить юзернейм",
            callback_data="market_buy",
            icon_custom_emoji_id=EMOJI["buy"]
        )],
        [InlineKeyboardButton(
            text="Продать юзернейм",
            callback_data="market_sell",
            icon_custom_emoji_id=EMOJI["sell"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

def get_profile_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Пополнить баланс",
            callback_data="profile_deposit",
            icon_custom_emoji_id=EMOJI["money"]
        )],
        [InlineKeyboardButton(
            text="Вывод средств",
            callback_data="profile_withdraw",
            icon_custom_emoji_id=EMOJI["wallet"]
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

def get_transfer_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Я передал владельца",
            callback_data="check_owner",
            icon_custom_emoji_id=EMOJI["check"]
        )],
        [InlineKeyboardButton(
            text="◁ Отмена",
            callback_data="cancel_sell",
            icon_custom_emoji_id=EMOJI["cross"]
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

# --- Обработчики команд ---
@router.message(Command("start"))
async def cmd_start(message: Message):
    await create_or_update_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"{em('bot')} <b>Vest Search</b>\n"
        f"{em('info')} Поиск и автоматическое занятие юзернеймов Telegram.\n\n"
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

@router.message(F.text == "Поиск")
async def menu_search(message: Message):
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == "Автозанятие")
async def menu_autoreserve(message: Message):
    if not telethon_clients:
        await message.answer(
            f"{em('cross')} Нет активных аккаунтов!",
            reply_markup=get_main_keyboard()
        )
        return
    
    await message.answer(
        f"{em('auto')} <b>Автоматическое занятие юзернеймов</b>\n\n"
        f"Бот создаст каналы с твоим именем и займёт свободные юзернеймы.\n"
        f"{em('info')} Через 7 дней каналы будут переданы тебе.\n\n"
        f"Выбери тип:",
        reply_markup=get_auto_reserve_type_keyboard()
    )

@router.message(F.text == "Маркет")
async def menu_market_main(message: Message):
    await message.answer(
        f"{em('market')} <b>Маркетплейс</b>\n\n"
        f"Выбери действие:",
        reply_markup=get_market_menu_keyboard()
    )

@router.message(F.text == "Профиль")
async def menu_profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    text = (
        f"{em('profile')} <b>Vest Search</b>\n\n"
        f"{em('link')} Юзернейм: @{user[1] or 'не указан'}\n"
        f"{em('wallet')} Баланс: {user[2]:.2f} ₽\n"
        f"{em('graph')} Всего поисков: {user[3]}\n"
        f"{em('check')} Найдено ников: {user[4]}\n"
        f"{em('people')} Рефералов: {user[5]}\n\n"
        f"{em('clock')} <b>Последние поиски:</b>\n"
    )
    
    last_searches = json.loads(user[6] or "[]")
    if last_searches:
        for s in last_searches[:5]:
            text += f"• {s['query']}\n"
    else:
        text += "Пусто"
    
    await message.answer(text, reply_markup=get_profile_keyboard())

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        f"{em('bot')} <b>Vest Search</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# --- Профиль: пополнение и вывод ---
@router.callback_query(F.data == "profile_deposit")
async def profile_deposit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{em('money')} <b>Пополнение баланса</b>\n\n"
        f"Введи сумму в рублях (от 20₽):",
        reply_markup=get_back_button()
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await callback.answer()

@router.message(BalanceStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    amount = int(message.text)
    if amount < 20:
        await message.answer(f"{em('cross')} Минимальная сумма 20₽.")
        return
    
    invoice = await create_crypto_invoice(message.from_user.id, amount)
    
    if not invoice:
        await message.answer(f"{em('cross')} Ошибка создания счёта.")
        await state.clear()
        return
    
    await state.update_data(invoice_id=invoice["invoice_id"], amount=amount)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Оплатить",
            url=invoice["pay_url"],
            icon_custom_emoji_id=EMOJI["money"]
        )],
        [InlineKeyboardButton(
            text="✅ Проверить оплату",
            callback_data="check_payment",
            icon_custom_emoji_id=EMOJI["check"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])
    
    await message.answer(
        f"{em('money')} <b>Счёт создан!</b>\n\n"
        f"Сумма: {amount}₽ ({invoice['amount_usdt']} USDT)\n"
        f"Нажми кнопку ниже для оплаты.",
        reply_markup=keyboard
    )
    await state.set_state(BalanceStates.waiting_for_payment)

@router.callback_query(F.data == "check_payment")
async def check_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    invoice_id = data.get("invoice_id")
    amount = data.get("amount")
    
    if not invoice_id:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    
    is_paid = await check_crypto_payment(invoice_id)
    
    if is_paid:
        await add_balance(callback.from_user.id, amount)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE crypto_payments SET status = 'paid', paid_date = ? WHERE invoice_id = ?",
                (datetime.now().isoformat(), invoice_id)
            )
            await db.commit()
        
        await callback.message.edit_text(
            f"{em('check')} <b>Баланс пополнен на {amount}₽!</b>",
            reply_markup=get_back_button()
        )
        await state.clear()
    else:
        await callback.answer("Платёж ещё не получен", show_alert=True)
    
    await callback.answer()

@router.callback_query(F.data == "profile_withdraw")
async def profile_withdraw(callback: CallbackQuery):
    await callback.message.edit_text(
        f"{em('support')} <b>Вывод средств</b>\n\n"
        f"Для вывода обратитесь в поддержку:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_back_button()
    )
    await callback.answer()

# --- Маркет: покупка ---
@router.callback_query(F.data == "market_buy")
async def market_buy_list(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, channel_username, price, seller_username 
               FROM market_listings 
               WHERE status = 'active' 
               ORDER BY price ASC"""
        ) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        await callback.message.edit_text(
            f"{em('market')} <b>Нет доступных юзернеймов</b>",
            reply_markup=get_back_button()
        )
        await callback.answer()
        return
    
    text = f"{em('market')} <b>Доступные юзернеймы:</b>\n\n"
    keyboard = []
    
    for row in rows:
        id_, username, price, seller = row
        text += f"@{username} — {price}₽\n"
        keyboard.append([InlineKeyboardButton(
            text=f"@{username} | {price}₽",
            callback_data=f"buy_channel_{id_}",
            icon_custom_emoji_id=EMOJI["buy"]
        )])
    
    keyboard.append([InlineKeyboardButton(
        text="◁ Назад",
        callback_data="back_to_market_menu",
        icon_custom_emoji_id=EMOJI["back"]
    )])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("buy_channel_"))
async def buy_channel_confirm(callback: CallbackQuery):
    listing_id = int(callback.data.split("_")[-1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_username, price, seller_id FROM market_listings WHERE id = ? AND status = 'active'",
            (listing_id,)
        ) as cursor:
            listing = await cursor.fetchone()
    
    if not listing:
        await callback.answer("Лот не найден", show_alert=True)
        return
    
    username, price, seller_id = listing
    
    user_balance = await get_user_balance(callback.from_user.id)
    
    if user_balance < price:
        await callback.answer(f"Недостаточно средств! Баланс: {user_balance:.2f}₽", show_alert=True)
        return
    
    if not await subtract_balance(callback.from_user.id, price):
        await callback.answer("Ошибка списания", show_alert=True)
        return
    
    seller_amount = price * 0.9
    await add_balance(seller_id, seller_amount)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE market_listings 
               SET status = 'sold', buyer_id = ?, sold_date = ? 
               WHERE id = ?""",
            (callback.from_user.id, datetime.now().isoformat(), listing_id)
        )
        await db.commit()
    
    await callback.message.edit_text(
        f"{em('check')} <b>Покупка успешна!</b>\n\n"
        f"Юзернейм: @{username}\n"
        f"Цена: {price}₽\n\n"
        f"{em('info')} Свяжись с продавцом для получения канала.",
        reply_markup=get_back_button()
    )
    
    try:
        await bot.send_message(
            seller_id,
            f"{em('money')} <b>Твой юзернейм @{username} продан!</b>\n"
            f"На баланс зачислено: {seller_amount:.2f}₽\n\n"
            f"Покупатель: @{callback.from_user.username or 'id' + str(callback.from_user.id)}"
        )
        await bot.send_message(
            callback.from_user.id,
            f"{em('info')} <b>Контакт продавца:</b> @{listing[3]}"
        )
    except:
        pass
    
    await callback.answer()

# --- Маркет: продажа (новая логика со ссылкой) ---
@router.callback_query(F.data == "market_sell")
async def market_sell_start(callback: CallbackQuery, state: FSMContext):
    if not telethon_clients:
        await callback.answer("❌ Нет активных аккаунтов!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('money')} <b>Продажа юзернейма</b>\n\n"
        f"Введи цену (от 5₽ до 20000₽):\n"
        f"{em('info')} Комиссия платформы: 10%",
        reply_markup=get_back_button()
    )
    await state.set_state(MarketSellStates.waiting_for_price)
    await callback.answer()

@router.message(MarketSellStates.waiting_for_price)
async def market_sell_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    price = int(message.text)
    if not (5 <= price <= 20000):
        await message.answer(f"{em('cross')} Цена должна быть от 5 до 20000₽.")
        return
    
    await state.update_data(price=price)
    await message.answer(
        f"{em('invite')} <b>Отправь ссылку-приглашение в канал:</b>\n\n"
        f"Пример: https://t.me/+abcdefghijkl\n\n"
        f"{em('info')} Как получить ссылку:\n"
        f"1. Открой канал\n"
        f"2. Нажми «Редактировать»\n"
        f"3. Выбери «Пригласительные ссылки»\n"
        f"4. Создай новую ссылку и отправь её боту",
        reply_markup=get_back_button()
    )
    await state.set_state(MarketSellStates.waiting_for_invite_link)

@router.message(MarketSellStates.waiting_for_invite_link)
async def market_sell_invite_link(message: Message, state: FSMContext):
    invite_link = message.text.strip()
    
    if not invite_link.startswith("https://t.me/"):
        await message.answer(f"{em('cross')} Неверный формат ссылки. Должна начинаться с https://t.me/")
        return
    
    data = await state.get_data()
    price = data['price']
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    bot_username = await get_bot_username(client)
    
    await message.answer(f"{em('loading')} Вступаю в канал...")
    
    success, channel_username, error = await join_channel_by_link(client, invite_link)
    
    if not success:
        await message.answer(
            f"{em('cross')} <b>Не удалось вступить в канал:</b>\n{error}\n\n"
            f"Убедись, что:\n"
            f"• Ссылка действительна\n"
            f"• Канал публичный или ссылка рабочая\n"
            f"• Бот не заблокирован в канале",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    if not channel_username:
        await message.answer(
            f"{em('cross')} У канала нет юзернейма!\n"
            f"Для продажи канал должен иметь публичный юзернейм (например @username).",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    await state.update_data(
        channel_username=channel_username,
        bot_username=bot_username,
        client_session=client.session.save()
    )
    
    await message.answer(
        f"{em('check')} Бот @{bot_username} вступил в канал @{channel_username}!\n\n"
        f"{em('owner')} <b>Теперь передай боту права администратора:</b>\n\n"
        f"1. Открой канал @{channel_username}\n"
        f"2. Зайди в «Администраторы»\n"
        f"3. Добавь @{bot_username} как администратора\n"
        f"4. Выдай права: публикация сообщений\n\n"
        f"После этого нажми кнопку ниже:",
        reply_markup=get_transfer_keyboard()
    )
    await state.set_state(MarketSellStates.waiting_for_transfer)

@router.callback_query(F.data == "check_owner")
async def check_owner(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_username = data.get("channel_username")
    price = data.get("price")
    bot_username = data.get("bot_username")
    client_session = data.get("client_session")
    
    if not client_session:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return
    
    client = TelegramClient(StringSession(client_session), API_ID, API_HASH)
    await client.connect()
    
    is_owner = await check_channel_owner(client, channel_username)
    
    if not is_owner:
        await callback.answer("❌ Бот не является администратором канала!", show_alert=True)
        return
    
    # Проверяем, не выставлен ли уже
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM market_listings WHERE channel_username = ? AND status = 'active'",
            (channel_username,)
        ) as cursor:
            existing = await cursor.fetchone()
    
    if existing:
        await callback.message.edit_text(
            f"{em('cross')} Этот юзернейм уже выставлен на продажу!",
            reply_markup=get_back_button()
        )
        await state.clear()
        await callback.answer()
        return
    
    # Сохраняем лот
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO market_listings 
               (seller_id, seller_username, channel_username, price, status, bot_joined, owner_verified, bot_account_username, created_date) 
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (callback.from_user.id, callback.from_user.username, channel_username, price, True, True, bot_username, datetime.now().isoformat())
        )
        await db.commit()
    
    await callback.message.edit_text(
        f"{em('check')} <b>Объявление создано!</b>\n\n"
        f"Юзернейм: @{channel_username}\n"
        f"Цена: {price}₽\n"
        f"Ты получишь: {price * 0.9:.2f}₽ (после комиссии 10%)\n\n"
        f"{em('info')} Ожидай покупателя!",
        reply_markup=get_back_button()
    )
    await state.clear()
    await callback.answer("✅ Объявление создано!", show_alert=True)

@router.callback_query(F.data == "cancel_sell")
async def cancel_sell(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{em('cross')} <b>Продажа отменена</b>",
        reply_markup=get_back_button()
    )
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "back_to_market_menu")
async def back_to_market_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        f"{em('market')} <b>Маркетплейс</b>\n\nВыбери действие:",
        reply_markup=get_market_menu_keyboard()
    )
    await callback.answer()

# --- Поиск ---
@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    
    if not telethon_clients:
        await callback.answer("❌ Нет активных аккаунтов!", show_alert=True)
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
        await callback.answer("❌ Нет активных аккаунтов!", show_alert=True)
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
    else:
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
    
    if reserve_type == "len":
        length = reserve_value
        variants = generate_random_usernames(length, count * 3)
    else:
        length = 6
        variants = generate_with_keyword(reserve_value or "vest", length, count * 3)
    
    found = await check_many_usernames(variants)
    
    if len(found) < count:
        await message.answer(
            f"{em('cross')} Найдено только {len(found)} свободных. Нужно {count}.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    to_reserve = found[:count]
    
    await message.answer(
        f"{em('loading')} <b>Начинаю занятие {count} юзернеймов...</b>\n"
        f"Задержка: 15 секунд\n"
        f"Каналы: {message.from_user.full_name}"
    )
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    title = message.from_user.full_name or message.from_user.username or "Vest User"
    created = []
    
    for i, username in enumerate(to_reserve, 1):
        status_msg = await message.answer(f"{em('loading')} Создаю {i}/{count}: @{username}...")
        
        channel_link = await create_channel_with_username(client, username, title)
        
        if channel_link:
            await save_reserved_channel(message.from_user.id, username, channel_link)
            created.append((username, channel_link))
            await status_msg.edit_text(f"{em('check')} Канал {i}/{count}: {channel_link}")
        else:
            await status_msg.edit_text(f"{em('cross')} Ошибка @{username}")
        
        if i < count:
            await asyncio.sleep(15)
    
    if created:
        text = f"{em('check')} <b>Занято {len(created)} юзернеймов!</b>\n\n"
        for u, link in created:
            text += f"• @{u} → {link}\n"
        text += f"\n{em('info')} Каналы будут переданы через 7 дней."
    else:
        text = f"{em('cross')} Не удалось занять ни одного юзернейма."
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Админ-панель ---
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
        await message.answer(f"{em('cross')} Ошибка: неверный пароль")
    
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
        async with db.execute("SELECT SUM(balance) FROM users") as cursor:
            total_balance = (await cursor.fetchone())[0] or 0
    
    text = (
        f"{em('stats')} <b>Статистика Vest Search</b>\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💰 Общий баланс: {total_balance:.2f}₽\n"
        f"🔍 Всего поисков: {total_searches}\n"
        f"✅ Найдено ников: {total_found}\n"
        f"📦 Активных лотов: {active_listings}\n"
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
