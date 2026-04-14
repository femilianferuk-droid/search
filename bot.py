import asyncio
import logging
import os
import json
import random
import string
import uuid
import re
from typing import Optional, List, Dict, Tuple, Any
from datetime import datetime, timedelta, date

from dotenv import load_dotenv
import aiohttp
import asyncpg

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
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient
from telethon.errors import (
    UsernameNotOccupiedError,
    UsernameOccupiedError,
    FloodWaitError,
    SessionPasswordNeededError
)
from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.sessions import StringSession

# --- Загрузка переменных ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")

ADMIN_ID = 7973988177
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"
SUPPORT_USERNAME = "VestSupport"

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Не указаны BOT_TOKEN или DATABASE_URL")

# --- Настройки ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

http_session: Optional[aiohttp.ClientSession] = None
db_pool: Optional[asyncpg.Pool] = None
telethon_clients: List[TelegramClient] = []
current_client_index = 0

# --- Премиум эмодзи ---
EMOJI = {
    "search": "5870982283724328568",
    "market": "5884479287171485878",
    "profile": "5870994129244131212",
    "check": "5870633910337015697",
    "cross": "5870657884844462243",
    "pencil": "5870676941614354370",
    "info": "6028435952299413210",
    "bot_icon": "6030400221232501136",
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
    "stats": "5870921681735781843",
    "broadcast": "6039422865189638057",
    "auto": "6030400221232501136",
    "wallet": "5769126056262898415",
    "buy": "5904462880941545555",
    "sell": "5890848474563352982",
    "support": "6039450962865688331",
    "owner": "5891207662678317861",
    "pro": "5870633910337015697",
    "group": "5870772616305839506",
    "channel": "5873147866364514353",
    "bot_create": "6030400221232501136",
    "key": "6037249452824072506",
    "phone": "6039450962865688331",
    "invite": "5769289093221454192",
    "transfer": "5890848474563352982",
    "people": "5870772616305839506",
    "trash": "5870875489362513438",
    "ban": "5870657884844462243",
    "unban": "5870633910337015697",
    "reset": "5345906554510012647",
}

def em(name: str) -> str:
    emoji_id = EMOJI.get(name, EMOJI["check"])
    return f'<tg-emoji emoji-id="{emoji_id}">👍</tg-emoji>'

# --- Проверка бана (вспомогательная функция) ---
async def check_ban(message_or_callback) -> bool:
    user_id = message_or_callback.from_user.id
    if await check_user_banned(user_id):
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(f"{em('cross')} Вы заблокированы!")
        else:
            await message_or_callback.answer("Вы заблокированы!", show_alert=True)
        return True
    return False

# --- Состояния FSM ---
class SearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()

class BotSearchStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_length = State()
    waiting_for_suffix = State()

class MarketSellStates(StatesGroup):
    waiting_for_price = State()
    waiting_for_invite_link = State()
    waiting_for_transfer = State()

class AdminStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()
    waiting_for_broadcast = State()
    waiting_for_balance_user = State()
    waiting_for_balance_amount = State()
    waiting_for_delete_user = State()
    waiting_for_ban_user = State()
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()

class AutoReserveStates(StatesGroup):
    waiting_for_keyword = State()
    waiting_for_count = State()

class CreateStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_count = State()
    waiting_for_days = State()
    waiting_for_bot_name = State()
    waiting_for_bot_username = State()

class BalanceStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_payment = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

# --- База данных ---
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance DECIMAL(10,2) DEFAULT 0,
                searches_count INTEGER DEFAULT 0,
                found_count INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                is_pro BOOLEAN DEFAULT FALSE,
                pro_expires_at TIMESTAMP,
                channels_created INTEGER DEFAULT 0,
                groups_created INTEGER DEFAULT 0,
                bots_created INTEGER DEFAULT 0,
                gen5_used INTEGER DEFAULT 0,
                last_gen5_reset DATE DEFAULT CURRENT_DATE,
                is_banned BOOLEAN DEFAULT FALSE,
                last_searches JSONB DEFAULT '[]',
                registered_date TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_listings (
                id SERIAL PRIMARY KEY,
                seller_id BIGINT,
                seller_username TEXT,
                channel_username TEXT UNIQUE,
                price DECIMAL(10,2),
                status TEXT DEFAULT 'pending',
                bot_account_username TEXT,
                created_date TIMESTAMP DEFAULT NOW(),
                sold_date TIMESTAMP
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telethon_accounts (
                id SERIAL PRIMARY KEY,
                phone TEXT UNIQUE,
                session_string TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                added_by BIGINT,
                added_date TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reserved_channels (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                channel_link TEXT,
                reserved_until TIMESTAMP,
                created_date TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reserved_groups (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                title TEXT,
                invite_link TEXT,
                reserved_until TIMESTAMP,
                created_date TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS created_bots (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                bot_username TEXT,
                bot_token TEXT,
                bot_name TEXT,
                created_date TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE,
                amount DECIMAL(10,2),
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                created_by BIGINT,
                created_date TIMESTAMP DEFAULT NOW()
            )
        """)
    
    logger.info("База данных инициализирована")

async def get_user(user_id: int) -> Optional[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

async def create_or_update_user(user_id: int, username: str = None):
    async with db_pool.acquire() as conn:
        user = await get_user(user_id)
        if not user:
            await conn.execute(
                "INSERT INTO users (user_id, username) VALUES ($1, $2)",
                user_id, username
            )
        elif username and user['username'] != username:
            await conn.execute(
                "UPDATE users SET username = $1 WHERE user_id = $2",
                username, user_id
            )

async def check_user_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return user['is_banned'] if user else False

async def get_user_balance(user_id: int) -> float:
    user = await get_user(user_id)
    return float(user['balance']) if user else 0

async def add_balance(user_id: int, amount: float):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
            amount, user_id
        )

async def subtract_balance(user_id: int, amount: float) -> bool:
    async with db_pool.acquire() as conn:
        user = await get_user(user_id)
        if user and float(user['balance']) >= amount:
            await conn.execute(
                "UPDATE users SET balance = balance - $1 WHERE user_id = $2",
                amount, user_id
            )
            return True
        return False

async def get_user_limits(user_id: int) -> Dict[str, int]:
    user = await get_user(user_id)
    is_pro = user['is_pro'] if user else False
    
    if user and user['pro_expires_at'] and user['pro_expires_at'] < datetime.now():
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_pro = FALSE WHERE user_id = $1", user_id)
        is_pro = False
    
    today = date.today()
    if user and (user['last_gen5_reset'] is None or user['last_gen5_reset'] < today):
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET gen5_used = 0, last_gen5_reset = $1 WHERE user_id = $2",
                today, user_id
            )
        gen5_used = 0
    else:
        gen5_used = user['gen5_used'] if user else 0
    
    daily_limit = 999 if is_pro else 10
    
    if is_pro:
        return {
            "channels": 80, "groups": 50, "bots": 10,
            "gen5_daily": daily_limit, "gen5_left": daily_limit
        }
    return {
        "channels": 20, "groups": 20, "bots": 3,
        "gen5_daily": 10, "gen5_left": max(0, 10 - gen5_used)
    }

async def increment_gen5_usage(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET gen5_used = COALESCE(gen5_used, 0) + 1 WHERE user_id = $1",
            user_id
        )

async def can_create(user_id: int, create_type: str) -> bool:
    user = await get_user(user_id)
    if not user:
        return False
    
    limits = await get_user_limits(user_id)
    field = f"{create_type}s_created"
    current = user[field] if user[field] is not None else 0
    
    return current < limits[create_type]

async def increment_created(user_id: int, create_type: str):
    async with db_pool.acquire() as conn:
        field = f"{create_type}s_created"
        await conn.execute(
            f"UPDATE users SET {field} = COALESCE({field}, 0) + 1 WHERE user_id = $1",
            user_id
        )

async def activate_pro(user_id: int, days: int = 30):
    async with db_pool.acquire() as conn:
        expires = datetime.now() + timedelta(days=days)
        await conn.execute(
            "UPDATE users SET is_pro = TRUE, pro_expires_at = $1 WHERE user_id = $2",
            expires, user_id
        )

async def get_all_users() -> List[int]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r['user_id'] for r in rows]

async def increment_search(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET searches_count = searches_count + 1 WHERE user_id = $1",
            user_id
        )

async def add_found_nick(user_id: int, count: int = 1):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET found_count = found_count + $1 WHERE user_id = $2",
            count, user_id
        )

async def delete_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)

async def toggle_ban_user(user_id: int, ban: bool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_banned = $1 WHERE user_id = $2",
            ban, user_id
        )

async def create_promo_code(code: str, amount: float, max_uses: int, created_by: int) -> bool:
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO promo_codes (code, amount, max_uses, created_by) VALUES ($1, $2, $3, $4)",
                code, amount, max_uses, created_by
            )
            return True
        except asyncpg.UniqueViolationError:
            return False

async def use_promo_code(user_id: int, code: str) -> Optional[float]:
    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT * FROM promo_codes WHERE code = $1", code)
        if not promo or promo['used_count'] >= promo['max_uses']:
            return None
        await conn.execute(
            "UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1",
            code
        )
        await add_balance(user_id, float(promo['amount']))
        return float(promo['amount'])

async def reset_all_limits():
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET channels_created = 0, groups_created = 0, bots_created = 0, gen5_used = 0"
        )

async def get_stats() -> Dict[str, Any]:
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT SUM(balance) FROM users") or 0
        total_searches = await conn.fetchval("SELECT SUM(searches_count) FROM users") or 0
        active_listings = await conn.fetchval("SELECT COUNT(*) FROM market_listings WHERE status = 'active'") or 0
        pro_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_pro = TRUE") or 0
        total_bots = await conn.fetchval("SELECT COUNT(*) FROM created_bots") or 0
        banned_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE") or 0
        
        return {
            "total_users": total_users,
            "total_balance": float(total_balance),
            "total_searches": total_searches,
            "active_listings": active_listings,
            "pro_users": pro_users,
            "total_bots": total_bots,
            "banned_users": banned_users,
            "telethon_accounts": len(telethon_clients)
        }

# --- Загрузка Telethon аккаунтов ---
async def load_telethon_accounts():
    global telethon_clients
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT phone, session_string FROM telethon_accounts WHERE is_active = TRUE"
        )
    
    for row in rows:
        if row['session_string']:
            client = TelegramClient(StringSession(row['session_string']), API_ID, API_HASH)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    telethon_clients.append(client)
                    logger.info(f"Аккаунт {row['phone']} загружен")
            except Exception as e:
                logger.error(f"Ошибка загрузки {row['phone']}: {e}")

async def save_telethon_account(phone: str, session_string: str, added_by: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO telethon_accounts (phone, session_string, added_by) 
               VALUES ($1, $2, $3) 
               ON CONFLICT (phone) DO UPDATE SET session_string = $2, is_active = TRUE""",
            phone, session_string, added_by
        )

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

# --- Проверка юзернеймов ---
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

async def check_many_usernames(usernames: List[str], max_workers: int = 5) -> List[str]:
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

# --- Генерация юзернеймов ---
def generate_random_usernames(length: int, count: int = 50) -> List[str]:
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    while len(variants) < count:
        username = ''.join(random.choices(letters, k=length))
        variants.add(username)
    
    return list(variants)

def generate_bot_usernames(keyword: str, length: int, suffix_type: str, count: int = 50) -> List[str]:
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    digits = "0123456789"
    
    suffix = "bot" if suffix_type == "bot" else "_bot"
    base_length = length - len(suffix)
    
    if base_length <= 0:
        return []
    
    while len(variants) < count:
        if keyword:
            if len(keyword) <= base_length:
                remaining = base_length - len(keyword)
                if random.choice([True, False]):
                    username = keyword + ''.join(random.choices(letters + digits, k=remaining))
                else:
                    username = ''.join(random.choices(letters + digits, k=remaining)) + keyword
            else:
                username = keyword[:base_length]
        else:
            username = ''.join(random.choices(letters, k=base_length))
        
        full_username = username + suffix
        variants.add(full_username)
    
    return list(variants)

def generate_with_keyword(keyword: str, length: int, count: int = 50) -> List[str]:
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

# --- Вступление в канал и создание ботов ---
def extract_invite_hash(link: str) -> Optional[str]:
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
    invite_hash = extract_invite_hash(invite_link)
    if not invite_hash:
        return False, None, "Неверный формат ссылки"
    
    try:
        await client(CheckChatInviteRequest(invite_hash))
        updates = await client(ImportChatInviteRequest(invite_hash))
        
        if updates.chats:
            channel = updates.chats[0]
            channel_username = getattr(channel, 'username', None)
            return True, channel_username, None
        else:
            return False, None, "Не удалось получить информацию о канале"
            
    except Exception as e:
        return False, None, f"Ошибка: {str(e)[:100]}"

async def create_bot_via_botfather(client: TelegramClient, bot_name: str, bot_username: str) -> Optional[Tuple[str, str]]:
    try:
        botfather = await client.get_entity("@BotFather")
        
        await client.send_message(botfather, "/start")
        await asyncio.sleep(4)
        
        await client.send_message(botfather, "/newbot")
        await asyncio.sleep(4)
        
        await client.send_message(botfather, bot_name)
        await asyncio.sleep(4)
        
        await client.send_message(botfather, bot_username)
        await asyncio.sleep(4)
        
        messages = await client.get_messages(botfather, limit=1)
        if messages and messages[0].message:
            text = messages[0].message
            token_match = re.search(r'(\d+:[A-Za-z0-9_-]+)', text)
            if token_match:
                token = token_match.group(1)
                return bot_username, token
        
        return None
    except Exception as e:
        logger.error(f"Ошибка создания бота: {e}")
        return None

async def save_bot_token(user_id: int, bot_username: str, bot_token: str, bot_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO created_bots (user_id, bot_username, bot_token, bot_name) VALUES ($1, $2, $3, $4)",
            user_id, bot_username, bot_token, bot_name
        )

# --- Crypto Bot API ---
async def create_crypto_invoice(user_id: int, amount_rub: float, invoice_type: str = "deposit") -> Optional[Dict]:
    if not CRYPTO_BOT_TOKEN:
        return None
    
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    
    amount_usdt = round(amount_rub / 90, 2)
    invoice_id = str(uuid.uuid4())
    
    descriptions = {
        "deposit": f"Пополнение баланса Vest Search на {amount_rub} RUB",
        "pro": "Подписка Vest Search Pro на 30 дней"
    }
    
    data = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": descriptions.get(invoice_type, f"Платеж Vest Search"),
        "payload": invoice_id,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600
    }
    
    try:
        async with http_session.post(url, headers=headers, json=data) as resp:
            result = await resp.json()
            
            if result.get("ok"):
                return {
                    "invoice_id": invoice_id,
                    "pay_url": result["result"]["pay_url"],
                    "amount_usdt": amount_usdt
                }
            return None
    except Exception as e:
        logger.error(f"Crypto Bot exception: {e}")
        return None

# --- Клавиатуры ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Поиск", icon_custom_emoji_id=EMOJI["search"]),
                KeyboardButton(text="Поиск ботов", icon_custom_emoji_id=EMOJI["bot_icon"])
            ],
            [
                KeyboardButton(text="Создание", icon_custom_emoji_id=EMOJI["add"]),
                KeyboardButton(text="Автозанятие", icon_custom_emoji_id=EMOJI["auto"])
            ],
            [
                KeyboardButton(text="Маркет", icon_custom_emoji_id=EMOJI["market"]),
                KeyboardButton(text="Профиль", icon_custom_emoji_id=EMOJI["profile"])
            ]
        ],
        resize_keyboard=True
    )

def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

def get_create_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Канал",
            callback_data="create_channel",
            icon_custom_emoji_id=EMOJI["channel"]
        )],
        [InlineKeyboardButton(
            text="Группа",
            callback_data="create_group",
            icon_custom_emoji_id=EMOJI["group"]
        )],
        [InlineKeyboardButton(
            text="Бот",
            callback_data="create_bot",
            icon_custom_emoji_id=EMOJI["bot_create"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

def get_bot_search_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="5 символов",
            callback_data="botsearch_len_5",
            icon_custom_emoji_id=EMOJI["search"]
        )],
        [InlineKeyboardButton(
            text="6 символов",
            callback_data="botsearch_len_6",
            icon_custom_emoji_id=EMOJI["search"]
        )],
        [InlineKeyboardButton(
            text="С ключевым словом",
            callback_data="botsearch_keyword",
            icon_custom_emoji_id=EMOJI["pencil"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

def get_bot_suffix_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="bot",
            callback_data="suffix_bot",
            icon_custom_emoji_id=EMOJI["bot_icon"]
        )],
        [InlineKeyboardButton(
            text="_bot",
            callback_data="suffix__bot",
            icon_custom_emoji_id=EMOJI["bot_icon"]
        )],
        [InlineKeyboardButton(
            text="◁ Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id=EMOJI["back"]
        )]
    ])

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

def get_profile_keyboard(is_pro: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Пополнить баланс",
        callback_data="profile_deposit",
        icon_custom_emoji_id=EMOJI["money"]
    ))
    builder.row(InlineKeyboardButton(
        text="Вывод средств",
        callback_data="profile_withdraw",
        icon_custom_emoji_id=EMOJI["wallet"]
    ))
    if not is_pro:
        builder.row(InlineKeyboardButton(
            text="🌟 Pro подписка",
            callback_data="profile_pro",
            icon_custom_emoji_id=EMOJI["pro"]
        ))
    builder.row(InlineKeyboardButton(
        text="🎁 Промокод",
        callback_data="profile_promo",
        icon_custom_emoji_id=EMOJI["gift"]
    ))
    builder.row(InlineKeyboardButton(
        text="Мои создания",
        callback_data="profile_creations",
        icon_custom_emoji_id=EMOJI["eye"]
    ))
    return builder.as_markup()

def get_transfer_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Я передал владельца",
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
            text="💰 Изменить баланс",
            callback_data="admin_change_balance",
            icon_custom_emoji_id=EMOJI["money"]
        )],
        [InlineKeyboardButton(
            text="🚫 Бан/Разбан",
            callback_data="admin_ban_user",
            icon_custom_emoji_id=EMOJI["ban"]
        )],
        [InlineKeyboardButton(
            text="🗑 Удалить пользователя",
            callback_data="admin_delete_user",
            icon_custom_emoji_id=EMOJI["trash"]
        )],
        [InlineKeyboardButton(
            text="🎁 Создать промокод",
            callback_data="admin_create_promo",
            icon_custom_emoji_id=EMOJI["gift"]
        )],
        [InlineKeyboardButton(
            text="🔄 Сбросить лимиты",
            callback_data="admin_reset_limits",
            icon_custom_emoji_id=EMOJI["reset"]
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
    if await check_ban(message):
        return
    await create_or_update_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"{em('bot_icon')} <b>Vest Search</b>\n"
        f"{em('info')} Поиск юзернеймов, создание каналов/групп/ботов.",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if await check_ban(message):
        return
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
    if await check_ban(message):
        return
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == "Поиск ботов")
async def menu_bot_search(message: Message):
    if await check_ban(message):
        return
    if not telethon_clients:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        return
    
    await message.answer(
        f"{em('bot_icon')} <b>Поиск юзернеймов для ботов</b>\n\n"
        f"{em('info')} Полная длина включает bot/_bot.\n"
        f"Выбери тип поиска:",
        reply_markup=get_bot_search_type_keyboard()
    )

@router.message(F.text == "Создание")
async def menu_create(message: Message):
    if await check_ban(message):
        return
    if not telethon_clients:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        return
    
    await message.answer(
        f"{em('add')} <b>Что хочешь создать?</b>",
        reply_markup=get_create_type_keyboard()
    )

@router.message(F.text == "Маркет")
async def menu_market_main(message: Message):
    if await check_ban(message):
        return
    await message.answer(
        f"{em('market')} <b>Маркетплейс</b>",
        reply_markup=get_market_menu_keyboard()
    )

@router.message(F.text == "Профиль")
async def menu_profile(message: Message):
    if await check_ban(message):
        return
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    pro_status = "🌟 Pro" if user['is_pro'] else "Обычный"
    if user['is_pro'] and user['pro_expires_at']:
        days_left = (user['pro_expires_at'] - datetime.now()).days
        pro_status += f" ({days_left} дн)"
    
    limits = await get_user_limits(message.from_user.id)
    
    text = (
        f"{em('profile')} <b>Vest Search</b>\n\n"
        f"{em('link')} @{user['username'] or 'не указан'}\n"
        f"{em('wallet')} Баланс: {float(user['balance']):.2f} RUB\n"
        f"{em('pro')} Статус: {pro_status}\n\n"
        f"{em('graph')} <b>Статистика:</b>\n"
        f"• Поисков: {user['searches_count']}\n"
        f"• Найдено: {user['found_count']}\n\n"
        f"{em('add')} <b>Лимиты:</b>\n"
        f"• Каналов: {user['channels_created'] or 0}/{limits['channels']}\n"
        f"• Групп: {user['groups_created'] or 0}/{limits['groups']}\n"
        f"• Ботов: {user['bots_created'] or 0}/{limits['bots']}\n"
        f"• 5 букв сегодня: {limits['gen5_left']}/{limits['gen5_daily']}"
    )
    
    await message.answer(text, reply_markup=get_profile_keyboard(user['is_pro']))

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    if await check_ban(callback):
        return
    await callback.message.delete()
    await callback.message.answer(
        f"{em('bot_icon')} <b>Vest Search</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# --- Профиль ---
@router.callback_query(F.data == "profile_deposit")
async def profile_deposit(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    await callback.message.edit_text(
        f"{em('money')} <b>Пополнение баланса</b>\n\n"
        f"Введи сумму в рублях (от 20 RUB):",
        reply_markup=get_back_button()
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await callback.answer()

@router.message(BalanceStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    amount = int(message.text)
    if amount < 20:
        await message.answer(f"{em('cross')} Минимальная сумма 20 RUB.")
        return
    
    if not CRYPTO_BOT_TOKEN:
        await message.answer(f"{em('cross')} Crypto Bot не настроен.")
        await state.clear()
        return
    
    invoice = await create_crypto_invoice(message.from_user.id, amount, "deposit")
    
    if not invoice:
        await message.answer(f"{em('cross')} Ошибка создания счета.")
        await state.clear()
        return
    
    await state.update_data(invoice_id=invoice["invoice_id"], amount=amount)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=invoice["pay_url"])],
        [InlineKeyboardButton(
            text="Проверить оплату",
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
        f"{em('money')} <b>Счет создан!</b>\n\n"
        f"Сумма: {amount} RUB ({invoice['amount_usdt']} USDT)\n"
        f"Нажми кнопку ниже для оплаты.",
        reply_markup=keyboard
    )
    await state.set_state(BalanceStates.waiting_for_payment)

@router.callback_query(F.data == "check_payment")
async def check_payment(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    await callback.answer("Проверьте оплату в Crypto Bot", show_alert=True)
    await state.clear()
    await callback.message.edit_text(
        f"{em('info')} Проверьте статус платежа в @CryptoBot",
        reply_markup=get_back_button()
    )

@router.callback_query(F.data == "profile_pro")
async def profile_pro(callback: CallbackQuery):
    if await check_ban(callback):
        return
    user = await get_user(callback.from_user.id)
    if user and user['is_pro']:
        await callback.answer("У вас уже есть Pro подписка!", show_alert=True)
        return
    
    balance = await get_user_balance(callback.from_user.id)
    
    if balance >= 30:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Оплатить с баланса",
                callback_data="pay_pro_balance",
                icon_custom_emoji_id=EMOJI["wallet"]
            )],
            [InlineKeyboardButton(
                text="◁ Назад",
                callback_data="back_to_main",
                icon_custom_emoji_id=EMOJI["back"]
            )]
        ])
        
        await callback.message.edit_text(
            f"{em('pro')} <b>Pro подписка</b>\n\n"
            f"• Каналов: до 80\n"
            f"• Групп: до 50\n"
            f"• Ботов: до 10\n"
            f"• 5 букв: безлимит\n\n"
            f"Стоимость: 30 RUB / 30 дней\n"
            f"Ваш баланс: {balance:.2f} RUB\n\n"
            f"Оплатить с баланса?",
            reply_markup=keyboard
        )
    else:
        await callback.message.edit_text(
            f"{em('cross')} Недостаточно средств!\n"
            f"Баланс: {balance:.2f} RUB\n"
            f"Необходимо: 30 RUB\n\n"
            f"Пополните баланс в профиле.",
            reply_markup=get_back_button()
        )
    
    await callback.answer()

@router.callback_query(F.data == "pay_pro_balance")
async def pay_pro_balance(callback: CallbackQuery):
    if await check_ban(callback):
        return
    if await subtract_balance(callback.from_user.id, 30):
        await activate_pro(callback.from_user.id)
        await callback.message.edit_text(
            f"{em('check')} <b>Pro подписка активирована на 30 дней!</b>",
            reply_markup=get_back_button()
        )
    else:
        await callback.answer("Ошибка оплаты", show_alert=True)
    
    await callback.answer()

@router.callback_query(F.data == "profile_promo")
async def profile_promo(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    await callback.message.edit_text(
        f"{em('gift')} <b>Введи промокод:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(PromoStates.waiting_for_code)
    await callback.answer()

@router.message(PromoStates.waiting_for_code)
async def process_promo(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    code = message.text.strip().upper()
    amount = await use_promo_code(message.from_user.id, code)
    
    if amount:
        await message.answer(
            f"{em('check')} <b>Промокод активирован!</b>\n"
            f"На баланс зачислено: {amount:.2f} RUB",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            f"{em('cross')} Неверный или использованный промокод.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data == "profile_creations")
async def profile_creations(callback: CallbackQuery):
    if await check_ban(callback):
        return
    async with db_pool.acquire() as conn:
        channels = await conn.fetch(
            "SELECT username, channel_link, reserved_until FROM reserved_channels "
            "WHERE user_id = $1 ORDER BY created_date DESC LIMIT 5",
            callback.from_user.id
        )
        groups = await conn.fetch(
            "SELECT title, invite_link, reserved_until FROM reserved_groups "
            "WHERE user_id = $1 ORDER BY created_date DESC LIMIT 5",
            callback.from_user.id
        )
        bots = await conn.fetch(
            "SELECT bot_username, bot_name FROM created_bots "
            "WHERE user_id = $1 ORDER BY created_date DESC LIMIT 5",
            callback.from_user.id
        )
    
    text = f"{em('eye')} <b>Мои создания</b>\n\n"
    
    if channels:
        text += f"{em('channel')} <b>Каналы:</b>\n"
        for c in channels:
            days_left = (c['reserved_until'] - datetime.now()).days if c['reserved_until'] else 0
            text += f"• @{c['username']} ({days_left} дн)\n"
    
    if groups:
        text += f"\n{em('group')} <b>Группы:</b>\n"
        for g in groups:
            days_left = (g['reserved_until'] - datetime.now()).days if g['reserved_until'] else 0
            text += f"• {g['title']} ({days_left} дн)\n"
    
    if bots:
        text += f"\n{em('bot_icon')} <b>Боты:</b>\n"
        for b in bots:
            text += f"• @{b['bot_username']}\n"
    
    if not channels and not groups and not bots:
        text += "У вас пока нет созданий."
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "profile_withdraw")
async def profile_withdraw(callback: CallbackQuery):
    if await check_ban(callback):
        return
    await callback.message.edit_text(
        f"{em('support')} <b>Вывод средств</b>\n\n"
        f"Для вывода обратитесь в поддержку:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_back_button()
    )
    await callback.answer()

# --- Поиск ---
@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    if await check_ban(callback):
        return
    length = int(callback.data.split("_")[-1])
    
    if not telethon_clients:
        await callback.answer("Нет активных аккаунтов!", show_alert=True)
        return
    
    if length == 5:
        limits = await get_user_limits(callback.from_user.id)
        if limits['gen5_left'] <= 0:
            await callback.answer(
                f"Дневной лимит исчерпан! ({limits['gen5_daily']}/день)",
                show_alert=True
            )
            return
        await increment_gen5_usage(callback.from_user.id)
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу свободные юзернеймы из {length} букв...</b>",
        reply_markup=get_back_button()
    )
    
    await increment_search(callback.from_user.id)
    
    variants = generate_random_usernames(length, 50)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(callback.from_user.id, len(found[:3]))
        text = f"{em('check')} <b>Найдены свободные юзернеймы ({length} букв):</b>\n\n"
        for u in found[:3]:
            text += f"• @{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено</b>"
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if not telethon_clients:
        await callback.answer("Нет активных аккаунтов!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('pencil')} <b>Введи ключевое слово (латиница):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(SearchStates.waiting_for_keyword)
    await callback.answer()

@router.message(SearchStates.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    if await check_ban(message):
        return
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
    if await check_ban(message):
        return
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
    
    variants = generate_with_keyword(keyword, length, 50)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(message.from_user.id, len(found[:3]))
        text = f"{em('check')} <b>Найдено с '{keyword}':</b>\n\n"
        for u in found[:3]:
            text += f"• @{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено.</b>"
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Поиск ботов ---
@router.callback_query(F.data.startswith("botsearch_"))
async def botsearch_handler(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    data = callback.data.replace("botsearch_", "")
    
    if data == "len_5":
        await state.update_data(bot_length=5, bot_keyword=None)
        await callback.message.edit_text(
            f"{em('bot_icon')} <b>Выбери суффикс:</b>",
            reply_markup=get_bot_suffix_keyboard()
        )
        await state.set_state(BotSearchStates.waiting_for_suffix)
    elif data == "len_6":
        await state.update_data(bot_length=6, bot_keyword=None)
        await callback.message.edit_text(
            f"{em('bot_icon')} <b>Выбери суффикс:</b>",
            reply_markup=get_bot_suffix_keyboard()
        )
        await state.set_state(BotSearchStates.waiting_for_suffix)
    elif data == "keyword":
        await callback.message.edit_text(
            f"{em('pencil')} <b>Введи ключевое слово:</b>",
            reply_markup=get_back_button()
        )
        await state.set_state(BotSearchStates.waiting_for_keyword)
    
    await callback.answer()

@router.message(BotSearchStates.waiting_for_keyword)
async def botsearch_keyword(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    keyword = message.text.strip().lower()
    
    if not all(c.isalnum() or c == '_' for c in keyword):
        await message.answer(f"{em('cross')} Используй только латинские буквы, цифры и _")
        return
    
    await state.update_data(bot_keyword=keyword)
    await message.answer(
        f"{em('pencil')} <b>Укажи ПОЛНУЮ длину (включая bot/_bot, от 5 до 32):</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(BotSearchStates.waiting_for_length)

@router.message(BotSearchStates.waiting_for_length)
async def botsearch_length(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    length = int(message.text)
    if not (5 <= length <= 32):
        await message.answer(f"{em('cross')} Длина должна быть от 5 до 32.")
        return
    
    await state.update_data(bot_length=length)
    await message.answer(
        f"{em('bot_icon')} <b>Выбери суффикс:</b>",
        reply_markup=get_bot_suffix_keyboard()
    )
    await state.set_state(BotSearchStates.waiting_for_suffix)

@router.callback_query(BotSearchStates.waiting_for_suffix, F.data.startswith("suffix_"))
async def botsearch_suffix(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    suffix_type = callback.data.replace("suffix_", "")
    
    data = await state.get_data()
    length = data.get('bot_length', 5)
    keyword = data.get('bot_keyword')
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу юзернеймы для ботов (полная длина {length})...</b>",
        reply_markup=get_back_button()
    )
    
    await increment_search(callback.from_user.id)
    
    variants = generate_bot_usernames(keyword or "", length, suffix_type, 50)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(callback.from_user.id, len(found[:3]))
        text = f"{em('check')} <b>Найдены свободные юзернеймы:</b>\n\n"
        for u in found[:3]:
            text += f"• @{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено.</b>"
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await state.clear()
    await callback.answer()

# --- Создание ---
@router.callback_query(F.data.startswith("create_"))
async def create_handler(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    create_type = callback.data.replace("create_", "")
    
    limits = await get_user_limits(callback.from_user.id)
    user = await get_user(callback.from_user.id)
    
    if create_type == "bot":
        current = user['bots_created'] or 0
        limit = limits['bots']
        if current >= limit:
            await callback.answer(f"Достигнут лимит ботов ({limit})!", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"{em('bot_icon')} <b>Создание бота</b>\n\n"
            f"Создано: {current}/{limit}\n\n"
            f"Введи имя для бота:",
            reply_markup=get_back_button()
        )
        await state.update_data(create_type="bot")
        await state.set_state(CreateStates.waiting_for_bot_name)
    else:
        current = user[f'{create_type}s_created'] or 0
        limit = limits[create_type]
        if current >= limit:
            await callback.answer(f"Достигнут лимит {create_type}!", show_alert=True)
            return
        
        await state.update_data(create_type=create_type)
        await callback.message.edit_text(
            f"{em(create_type)} <b>Создание {create_type}</b>\n\n"
            f"Создано: {current}/{limit}\n\n"
            f"Введи количество (от 1 до 50):",
            reply_markup=get_back_button()
        )
        await state.set_state(CreateStates.waiting_for_count)
    
    await callback.answer()

@router.message(CreateStates.waiting_for_bot_name)
async def create_bot_name(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    bot_name = message.text.strip()
    await state.update_data(bot_name=bot_name)
    
    await message.answer(
        f"{em('bot_icon')} <b>Введи юзернейм для бота (без @):</b>\n"
        f"{em('info')} Должен заканчиваться на bot или _bot",
        reply_markup=get_back_button()
    )
    await state.set_state(CreateStates.waiting_for_bot_username)

@router.message(CreateStates.waiting_for_bot_username)
async def create_bot_username(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    bot_username = message.text.strip().lower().replace("@", "")
    
    if not (bot_username.endswith("bot") or bot_username.endswith("_bot")):
        await message.answer(f"{em('cross')} Юзернейм должен заканчиваться на bot или _bot!")
        return
    
    if not (5 <= len(bot_username) <= 32):
        await message.answer(f"{em('cross')} Длина юзернейма должна быть от 5 до 32 символов!")
        return
    
    data = await state.get_data()
    bot_name = data.get('bot_name')
    
    limits = await get_user_limits(message.from_user.id)
    user = await get_user(message.from_user.id)
    current = user['bots_created'] or 0
    
    if current >= limits['bots']:
        await message.answer(f"{em('cross')} Достигнут лимит ботов!")
        await state.clear()
        return
    
    await message.answer(
        f"{em('loading')} <b>Создаю бота через BotFather...</b>\n"
        f"Это займет около 20 секунд."
    )
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    result = await create_bot_via_botfather(client, bot_name, bot_username)
    
    if result:
        username, token = result
        await save_bot_token(message.from_user.id, username, token, bot_name)
        await increment_created(message.from_user.id, "bot")
        
        await message.answer(
            f"{em('check')} <b>Бот успешно создан!</b>\n\n"
            f"🤖 @{username}\n"
            f"🔑 Токен: <code>{token}</code>\n\n"
            f"{em('info')} Сохрани токен в безопасном месте!",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            f"{em('cross')} Не удалось создать бота.\n"
            f"Возможно, юзернейм @{bot_username} уже занят.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.message(CreateStates.waiting_for_count)
async def create_count(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    count = int(message.text)
    if not (1 <= count <= 50):
        await message.answer(f"{em('cross')} Количество должно быть от 1 до 50.")
        return
    
    await state.update_data(count=count)
    await message.answer(
        f"{em('clock')} <b>Введи срок отлежки (от 7 до 200 дней):</b>\n"
        f"По истечении срока каналы будут переданы тебе.",
        reply_markup=get_back_button()
    )
    await state.set_state(CreateStates.waiting_for_days)

@router.message(CreateStates.waiting_for_days)
async def create_days(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    days = int(message.text)
    if not (7 <= days <= 200):
        await message.answer(f"{em('cross')} Срок должен быть от 7 до 200 дней.")
        return
    
    data = await state.get_data()
    create_type = data.get('create_type')
    count = data.get('count')
    
    limits = await get_user_limits(message.from_user.id)
    user = await get_user(message.from_user.id)
    current = user[f'{create_type}s_created'] or 0
    
    if current + count > limits[create_type]:
        await message.answer(
            f"{em('cross')} Превышен лимит! Можно создать ещё {limits[create_type] - current}.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    await message.answer(
        f"{em('loading')} <b>Создаю {count} {create_type}...</b>\n"
        f"Срок отлежки: {days} дней\n"
        f"Задержка между созданиями: 15 секунд"
    )
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    title = message.from_user.full_name or message.from_user.username or "Vest User"
    created = []
    transfer_date = datetime.now() + timedelta(days=days)
    
    for i in range(count):
        try:
            result = await client(CreateChannelRequest(
                title=f"{title} {i+1}",
                about=f"Создано через Vest Search. Будет передано через {days} дней.",
                megagroup=(create_type == "group")
            ))
            
            channel = result.chats[0]
            
            if create_type == "channel":
                username = f"{title.lower().replace(' ', '')}{random.randint(100, 999)}"
                try:
                    await client(UpdateUsernameRequest(channel, username))
                    link = f"https://t.me/{username}"
                except:
                    username = None
                    link = await client.export_invite_link(channel)
            else:
                username = None
                link = await client.export_invite_link(channel)
            
            async with db_pool.acquire() as conn:
                if create_type == "channel":
                    await conn.execute(
                        "INSERT INTO reserved_channels (user_id, username, channel_link, reserved_until) "
                        "VALUES ($1, $2, $3, $4)",
                        message.from_user.id, username, link, transfer_date
                    )
                else:
                    await conn.execute(
                        "INSERT INTO reserved_groups (user_id, title, invite_link, reserved_until) "
                        "VALUES ($1, $2, $3, $4)",
                        message.from_user.id, channel.title, link, transfer_date
                    )
            
            await increment_created(message.from_user.id, create_type)
            created.append(link)
            
        except Exception as e:
            logger.error(f"Ошибка создания: {e}")
        
        if i < count - 1:
            await asyncio.sleep(15)
    
    if created:
        text = f"{em('check')} <b>Создано {len(created)} {create_type}!</b>\n\n"
        text += f"Срок отлежки: {days} дней\n\n"
        text += f"{em('info')} <b>Ссылки для вступления:</b>\n"
        for link in created[:10]:
            text += f"• {link}\n"
        if len(created) > 10:
            text += f"... и ещё {len(created) - 10}\n"
        text += f"\n{em('clock')} Будут переданы через {days} дней."
    else:
        text = f"{em('cross')} Не удалось создать {create_type}."
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Маркет ---
@router.callback_query(F.data == "market_buy")
async def market_buy_list(callback: CallbackQuery):
    if await check_ban(callback):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, channel_username, price, seller_username 
               FROM market_listings 
               WHERE status = 'active' 
               ORDER BY price ASC"""
        )
    
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
        text += f"@{row['channel_username']} — {float(row['price'])} RUB\n"
        keyboard.append([InlineKeyboardButton(
            text=f"@{row['channel_username']} | {float(row['price'])} RUB",
            callback_data=f"buy_channel_{row['id']}",
            icon_custom_emoji_id=EMOJI["buy"]
        )])
    
    keyboard.append([InlineKeyboardButton(
        text="◁ Назад",
        callback_data="back_to_market_menu",
        icon_custom_emoji_id=EMOJI["back"]
    )])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("buy_channel_"))
async def buy_channel_confirm(callback: CallbackQuery):
    if await check_ban(callback):
        return
    listing_id = int(callback.data.split("_")[-1])
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_username, price, seller_id, seller_username FROM market_listings "
            "WHERE id = $1 AND status = 'active'",
            listing_id
        )
    
    if not row:
        await callback.answer("Лот не найден", show_alert=True)
        return
    
    price = float(row['price'])
    user_balance = await get_user_balance(callback.from_user.id)
    
    if user_balance < price:
        await callback.answer(f"Недостаточно средств! Баланс: {user_balance:.2f} RUB", show_alert=True)
        return
    
    if not await subtract_balance(callback.from_user.id, price):
        await callback.answer("Ошибка списания", show_alert=True)
        return
    
    seller_amount = price * 0.9
    await add_balance(row['seller_id'], seller_amount)
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE market_listings SET status = 'sold', sold_date = NOW() WHERE id = $1",
            listing_id
        )
    
    await callback.message.edit_text(
        f"{em('check')} <b>Покупка успешна!</b>\n\n"
        f"Юзернейм: @{row['channel_username']}\n"
        f"Цена: {price} RUB\n\n"
        f"Продавец: @{row['seller_username']}",
        reply_markup=get_back_button()
    )
    
    try:
        await bot.send_message(
            row['seller_id'],
            f"{em('money')} <b>Юзернейм @{row['channel_username']} продан!</b>\n"
            f"На баланс зачислено: {seller_amount:.2f} RUB"
        )
    except:
        pass
    
    await callback.answer()

@router.callback_query(F.data == "back_to_market_menu")
async def back_to_market_menu(callback: CallbackQuery):
    if await check_ban(callback):
        return
    await callback.message.edit_text(
        f"{em('market')} <b>Маркетплейс</b>",
        reply_markup=get_market_menu_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "market_sell")
async def market_sell_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if not telethon_clients:
        await callback.answer("Нет активных аккаунтов!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('money')} <b>Продажа юзернейма</b>\n\n"
        f"Введи цену (от 5 до 20000 RUB):\n"
        f"{em('info')} Комиссия платформы: 10%",
        reply_markup=get_back_button()
    )
    await state.set_state(MarketSellStates.waiting_for_price)
    await callback.answer()

@router.message(MarketSellStates.waiting_for_price)
async def market_sell_price(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    price = int(message.text)
    if not (5 <= price <= 20000):
        await message.answer(f"{em('cross')} Цена должна быть от 5 до 20000 RUB.")
        return
    
    await state.update_data(price=price)
    await message.answer(
        f"{em('invite')} <b>Отправь ссылку-приглашение в канал:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(MarketSellStates.waiting_for_invite_link)

@router.message(MarketSellStates.waiting_for_invite_link)
async def market_sell_invite_link(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    invite_link = message.text.strip()
    
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
    
    if not success or not channel_username:
        await message.answer(f"{em('cross')} Не удалось вступить в канал!")
        await state.clear()
        return
    
    await state.update_data(
        channel_username=channel_username,
        bot_username=bot_username,
        price=price
    )
    
    await message.answer(
        f"{em('check')} Бот @{bot_username} в канале @{channel_username}!\n\n"
        f"{em('owner')} Передай боту права администратора и нажми кнопку:",
        reply_markup=get_transfer_keyboard()
    )
    await state.set_state(MarketSellStates.waiting_for_transfer)

@router.callback_query(F.data == "check_owner")
async def check_owner(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    data = await state.get_data()
    channel_username = data.get("channel_username")
    price = data.get("price")
    bot_username = data.get("bot_username")
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO market_listings 
               (seller_id, seller_username, channel_username, price, status, bot_account_username) 
               VALUES ($1, $2, $3, $4, 'active', $5)""",
            callback.from_user.id, callback.from_user.username, channel_username, price, bot_username
        )
    
    await callback.message.edit_text(
        f"{em('check')} <b>Объявление создано!</b>\n\n"
        f"@{channel_username} — {price} RUB",
        reply_markup=get_back_button()
    )
    await state.clear()
    await callback.answer("Объявление создано!", show_alert=True)

@router.callback_query(F.data == "cancel_sell")
async def cancel_sell(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    await callback.message.edit_text(
        f"{em('cross')} <b>Продажа отменена</b>",
        reply_markup=get_back_button()
    )
    await state.clear()
    await callback.answer()

# --- Админ-панель ---
@router.callback_query(F.data == "admin_ban_user")
async def admin_ban_user_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('ban')} <b>Введи ID пользователя для бана/разбана:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_ban_user)
    await callback.answer()

@router.message(AdminStates.waiting_for_ban_user)
async def admin_ban_user_process(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи числовой ID.")
        return
    
    user_id = int(message.text)
    user = await get_user(user_id)
    
    if not user:
        await message.answer(f"{em('cross')} Пользователь не найден.")
        return
    
    new_status = not user['is_banned']
    await toggle_ban_user(user_id, new_status)
    
    await message.answer(
        f"{em('check')} <b>Пользователь {user_id} "
        f"{'заблокирован' if new_status else 'разблокирован'}!</b>",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('gift')} <b>Введи код промокода:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_promo_code)
    await callback.answer()

@router.message(AdminStates.waiting_for_promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    code = message.text.strip().upper()
    await state.update_data(promo_code=code)
    await message.answer(f"{em('money')} <b>Введи сумму начисления (RUB):</b>")
    await state.set_state(AdminStates.waiting_for_promo_amount)

@router.message(AdminStates.waiting_for_promo_amount)
async def admin_promo_amount(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    try:
        amount = float(message.text)
        data = await state.get_data()
        code = data['promo_code']
        
        if await create_promo_code(code, amount, 1, ADMIN_ID):
            await message.answer(
                f"{em('check')} <b>Промокод {code} создан!</b>\n"
                f"Сумма: {amount} RUB",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer(f"{em('cross')} Такой промокод уже существует!")
    except:
        await message.answer(f"{em('cross')} Введи число.")
    
    await state.clear()

@router.callback_query(F.data == "admin_reset_limits")
async def admin_reset_limits(callback: CallbackQuery):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await reset_all_limits()
    await callback.answer("Лимиты всех пользователей сброшены!", show_alert=True)

@router.callback_query(F.data == "admin_add_account")
async def admin_add_account(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
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
    if await check_ban(message):
        return
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
            f"{em('pencil')} <b>Введи код из Telegram:</b>",
            reply_markup=get_back_button()
        )
        await state.set_state(AdminStates.waiting_for_code)
    except Exception as e:
        await message.answer(f"{em('cross')} Ошибка: {e}")
        await state.clear()

@router.message(AdminStates.waiting_for_code)
async def admin_process_code(message: Message, state: FSMContext):
    if await check_ban(message):
        return
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
            f"{em('check')} <b>Аккаунт добавлен!</b>",
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
    if await check_ban(message):
        return
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
            f"{em('check')} <b>Аккаунт добавлен!</b>",
            reply_markup=get_main_keyboard()
        )
    except:
        await message.answer(f"{em('cross')} Неверный пароль")
    
    await state.clear()

@router.callback_query(F.data == "admin_list_accounts")
async def admin_list_accounts(callback: CallbackQuery):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT phone, is_active FROM telethon_accounts")
    
    text = f"{em('admin')} <b>Список аккаунтов:</b>\n\n" if rows else f"{em('info')} Нет аккаунтов"
    for row in rows:
        status = "✅" if row['is_active'] else "❌"
        text += f"{status} {row['phone']}\n"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_change_balance")
async def admin_change_balance_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('money')} <b>Введи ID пользователя:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_balance_user)
    await callback.answer()

@router.message(AdminStates.waiting_for_balance_user)
async def admin_balance_user(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи числовой ID.")
        return
    
    user_id = int(message.text)
    user = await get_user(user_id)
    
    if not user:
        await message.answer(f"{em('cross')} Пользователь не найден.")
        return
    
    await state.update_data(balance_user_id=user_id)
    await message.answer(
        f"Пользователь: @{user['username'] or user_id}\n"
        f"Баланс: {float(user['balance']):.2f} RUB\n\n"
        f"Введи новый баланс:"
    )
    await state.set_state(AdminStates.waiting_for_balance_amount)

@router.message(AdminStates.waiting_for_balance_amount)
async def admin_balance_amount(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    try:
        amount = float(message.text)
    except:
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    data = await state.get_data()
    user_id = data['balance_user_id']
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", amount, user_id)
    
    await message.answer(
        f"{em('check')} <b>Баланс обновлен!</b>",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "admin_delete_user")
async def admin_delete_user_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{em('trash')} <b>Введи ID пользователя для удаления:</b>",
        reply_markup=get_back_button()
    )
    await state.set_state(AdminStates.waiting_for_delete_user)
    await callback.answer()

@router.message(AdminStates.waiting_for_delete_user)
async def admin_delete_user_process(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи числовой ID.")
        return
    
    user_id = int(message.text)
    await delete_user(user_id)
    
    await message.answer(
        f"{em('check')} <b>Пользователь {user_id} удален!</b>",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if await check_ban(callback):
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа!", show_alert=True)
        return
    
    stats = await get_stats()
    
    text = (
        f"{em('stats')} <b>Статистика</b>\n\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"🚫 Забанено: {stats['banned_users']}\n"
        f"🌟 Pro: {stats['pro_users']}\n"
        f"💰 Баланс: {stats['total_balance']:.2f} RUB\n"
        f"🔍 Поисков: {stats['total_searches']}\n"
        f"📦 Лотов: {stats['active_listings']}\n"
        f"🤖 Ботов создано: {stats['total_bots']}\n"
        f"📱 Аккаунтов: {stats['telethon_accounts']}"
    )
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if await check_ban(callback):
        return
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
    if await check_ban(message):
        return
    if message.from_user.id != ADMIN_ID:
        return
    
    users = await get_all_users()
    success = 0
    
    await message.answer(f"{em('loading')} <b>Рассылка на {len(users)} пользователей...</b>")
    
    for user_id in users:
        try:
            await message.copy_to(user_id)
            success += 1
        except:
            pass
        await asyncio.sleep(0.05)
    
    await message.answer(
        f"{em('check')} <b>Рассылка завершена!</b>\n"
        f"✅ Успешно: {success}\n"
        f"❌ Не доставлено: {len(users) - success}",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

# --- Автозанятие ---
@router.message(F.text == "Автозанятие")
async def menu_autoreserve(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not telethon_clients:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        return
    
    await message.answer(
        f"{em('auto')} <b>Автозанятие</b>\n\n"
        f"Введи ключевое слово (или '-' для случайных):"
    )
    await state.set_state(AutoReserveStates.waiting_for_keyword)

@router.message(AutoReserveStates.waiting_for_keyword)
async def autoreserve_keyword(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    keyword = message.text.strip()
    if keyword == "-":
        keyword = None
    
    await state.update_data(keyword=keyword)
    await message.answer(f"{em('auto')} <b>Укажи количество (1-10):</b>")
    await state.set_state(AutoReserveStates.waiting_for_count)

@router.message(AutoReserveStates.waiting_for_count)
async def autoreserve_count(message: Message, state: FSMContext):
    if await check_ban(message):
        return
    if not message.text.isdigit():
        await message.answer(f"{em('cross')} Введи число.")
        return
    
    count = int(message.text)
    if not (1 <= count <= 10):
        await message.answer(f"{em('cross')} От 1 до 10.")
        return
    
    data = await state.get_data()
    keyword = data.get('keyword')
    
    length = 6
    if keyword:
        variants = generate_with_keyword(keyword, length, count * 10)
    else:
        variants = generate_random_usernames(length, count * 10)
    
    found = await check_many_usernames(variants)
    
    if len(found) < count:
        await message.answer(
            f"{em('cross')} Найдено только {len(found)}.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return
    
    to_reserve = found[:count]
    
    await message.answer(f"{em('loading')} <b>Занимаю {count} юзернеймов...</b>")
    
    client = get_next_client()
    if not client:
        await message.answer(f"{em('cross')} Нет активных аккаунтов!")
        await state.clear()
        return
    
    title = message.from_user.full_name or "Vest User"
    created = []
    
    for username in to_reserve:
        try:
            result = await client(CreateChannelRequest(
                title=title,
                about="Vest Search",
                megagroup=False
            ))
            channel = result.chats[0]
            await client(UpdateUsernameRequest(channel, username))
            link = f"https://t.me/{username}"
            
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO reserved_channels (user_id, username, channel_link, reserved_until) "
                    "VALUES ($1, $2, $3, $4)",
                    message.from_user.id, username, link, datetime.now() + timedelta(days=7)
                )
            
            created.append(username)
        except:
            pass
        
        await asyncio.sleep(15)
    
    if created:
        text = f"{em('check')} <b>Занято {len(created)} юзернеймов!</b>\n\n"
        for u in created:
            text += f"• @{u}\n"
        text += f"\nКаналы будут переданы через 7 дней."
    else:
        text = f"{em('cross')} Не удалось занять."
    
    await message.answer(text, reply_markup=get_main_keyboard())
    await state.clear()

# --- Жизненный цикл ---
@dp.startup()
async def on_startup():
    global http_session
    http_session = aiohttp.ClientSession()
    await init_db()
    await load_telethon_accounts()
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Главное меню"),
        types.BotCommand(command="admin", description="Админ-панель")
    ])
    logger.info(f"Vest Search запущен, аккаунтов: {len(telethon_clients)}")

@dp.shutdown()
async def on_shutdown():
    if http_session:
        await http_session.close()
    if db_pool:
        await db_pool.close()
    for client in telethon_clients:
        await client.disconnect()
    logger.info("Бот остановлен")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
