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
    ReplyKeyboardMarkup,
    KeyboardButton,
)
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

# --- Премиум эмодзи ID ---
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
}

def em(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(name, EMOJI["check"])}">👍</tg-emoji>'

# --- Главное меню с премиум эмодзи ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Поиск", icon_custom_emoji_id=EMOJI["search"]),
                KeyboardButton(text="Маркет", icon_custom_emoji_id=EMOJI["market"])
            ],
            [
                KeyboardButton(text="Профиль", icon_custom_emoji_id=EMOJI["profile"])
            ]
        ],
        resize_keyboard=True
    )

# --- Генерация случайных комбинаций букв (xwdeb, hadkdn) ---
def generate_random_usernames(length: int, count: int = 100) -> List[str]:
    """Генерирует случайные комбинации букв типа xwdeb, hadkdn"""
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    while len(variants) < count:
        username = ''.join(random.choices(letters, k=length))
        variants.add(username)
    
    return list(variants)

def generate_with_keyword(keyword: str, length: int, count: int = 100) -> List[str]:
    """Генерирует комбинации с ключевым словом типа xwkeyword, keywordabc"""
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    # Если ключевое слово уже длиннее или равно длине
    if len(keyword) >= length:
        return [keyword[:length]]
    
    remaining = length - len(keyword)
    
    while len(variants) < count:
        # Случайно решаем, где будет ключевое слово
        position = random.choice(["start", "end", "middle"])
        
        if position == "start":
            suffix = ''.join(random.choices(letters, k=remaining))
            variants.add(keyword + suffix)
        elif position == "end":
            prefix = ''.join(random.choices(letters, k=remaining))
            variants.add(prefix + keyword)
        else:  # middle
            left = random.randint(1, remaining - 1)
            right = remaining - left
            prefix = ''.join(random.choices(letters, k=left))
            suffix = ''.join(random.choices(letters, k=right))
            variants.add(prefix + keyword + suffix)
    
    return list(variants)

# --- Параллельная проверка юзернеймов ---
async def check_single_username(session: aiohttp.ClientSession, username: str) -> Optional[str]:
    """Проверяет один юзернейм, возвращает его если свободен"""
    url = f"https://t.me/{username}"
    try:
        async with session.head(url, timeout=3, allow_redirects=True) as resp:
            if resp.status == 404:
                return username
            return None
    except:
        return None

async def check_many_usernames(usernames: List[str], max_workers: int = 50) -> List[str]:
    """Параллельная проверка множества юзернеймов"""
    connector = aiohttp.TCPConnector(limit=max_workers, limit_per_host=max_workers)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_single_username(session, u) for u in usernames]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]

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

@router.message(F.text == "Поиск")
async def menu_search(message: Message):
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == "Профиль")
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

@router.message(F.text == "Маркет")
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
        f"{em('bot')} <b>Главное меню</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу свободные юзернеймы из {length} букв...</b>\n"
        f"{em('info')} Проверяю 100 вариантов параллельно...",
        reply_markup=get_back_button()
    )
    
    await increment_search(callback.from_user.id, f"Длина {length}")
    
    # Генерируем 100 случайных комбинаций типа xwdeb
    variants = generate_random_usernames(length, 100)
    
    # Параллельно проверяем все 100 вариантов
    found = await check_many_usernames(variants, max_workers=50)
    
    if found:
        text = f"{em('check')} <b>Найдены свободные юзернеймы ({length} букв):</b>\n\n"
        for u in found[:10]:
            text += f"• @{u} → t.me/{u}\n"
    else:
        text = f"{em('cross')} <b>Ничего не найдено</b>\nПопробуй другую длину."
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{em('pencil')} <b>Введи ключевое слово (латиница):</b>\n"
        f"Например: vest, moon, sol",
        reply_markup=get_back_button()
    )
    await state.set_state(SearchStates.waiting_for_keyword)
    await callback.answer()

@router.message(SearchStates.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext):
    keyword = message.text.strip().lower()
    
    if not all(c.isalpha() for c in keyword):
        await message.answer(
            f"{em('cross')} Используй только латинские буквы",
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
        f"{em('info')} Проверяю 100 вариантов параллельно...",
        reply_markup=get_main_keyboard()
    )
    
    await increment_search(message.from_user.id, f"'{keyword}' ({length})")
    
    # Генерируем 100 вариантов с ключевым словом
    variants = generate_with_keyword(keyword, length, 100)
    
    # Параллельно проверяем
    found = await check_many_usernames(variants, max_workers=50)
    
    if found:
        text = f"{em('check')} <b>Найдены свободные юзернеймы с '{keyword}':</b>\n\n"
        for u in found[:10]:
            text += f"• @{u} → t.me/{u}\n"
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
    
    # Проверяем, что юзернейм занят (существует)
    result = await check_many_usernames([username])
    if result:
        await message.answer(
            f"{em('cross')} Этот юзернейм свободен! Продавать можно только существующие.",
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
