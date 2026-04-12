import asyncio
import logging
import os
import json
import random
import string
from typing import Optional, List

from dotenv import load_dotenv

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
from telethon.errors import UsernameNotOccupiedError, UsernameOccupiedError, FloodWaitError
from telethon.tl.functions.account import CheckUsernameRequest

# --- Загрузка переменных окружения ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Твои API данные
API_ID = 32480523
API_HASH = "147839735c9fa4e83451209e9b55cfc5"

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

# --- Telethon клиент ---
telethon_client = TelegramClient("vest_search_session", API_ID, API_HASH)

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
                search_5_count INTEGER DEFAULT 2,
                search_6_count INTEGER DEFAULT 2,
                search_mask_count INTEGER DEFAULT 3,
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
                   (user_id, username, search_5_count, search_6_count, search_mask_count, registered_date) 
                   VALUES (?, ?, 2, 2, 3, ?)""",
                (user_id, username, datetime.now().isoformat())
            )
            await db.commit()
        elif username and user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()

async def decrement_search_count(user_id: int, search_type: str) -> bool:
    """Уменьшает счётчик поисков, возвращает True если поиск разрешён"""
    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(user_id)
        if not user:
            return False
        
        # Индексы полей: 0=user_id, 1=username, 2=searches_count, 3=found_count, 
        # 4=search_5_count, 5=search_6_count, 6=search_mask_count
        field_index = 4 if search_type == "5" else (5 if search_type == "6" else 6)
        current = user[field_index]
        
        if current <= 0:
            return False
        
        field_name = f"search_{search_type}_count"
        await db.execute(
            f"UPDATE users SET {field_name} = {field_name} - 1, searches_count = searches_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
        return True

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
            last_searches = json.loads(user[8] or "[]")
            last_searches.insert(0, {"query": query, "time": datetime.now().isoformat()})
            last_searches = last_searches[:5]
            await db.execute(
                "UPDATE users SET last_searches = ? WHERE user_id = ?",
                (json.dumps(last_searches), user_id)
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
}

def em(name: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI.get(name, EMOJI["check"])}">👍</tg-emoji>'

# --- Главное меню с премиум эмодзи ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔍 Поиск"),
                KeyboardButton(text="📦 Маркет")
            ],
            [
                KeyboardButton(text="👤 Профиль")
            ]
        ],
        resize_keyboard=True
    )

# --- Реальная проверка юзернейма через Telethon ---
async def check_username_real(username: str) -> bool:
    """
    Проверяет юзернейм через официальное API Telegram
    True = свободен, False = занят
    """
    try:
        result = await telethon_client(CheckUsernameRequest(username))
        return result  # True если свободен
    except UsernameOccupiedError:
        return False  # Занят
    except FloodWaitError as e:
        logger.warning(f"Flood wait {e.seconds} сек для {username}")
        await asyncio.sleep(e.seconds)
        return await check_username_real(username)
    except Exception as e:
        logger.error(f"Ошибка проверки {username}: {e}")
        return False

async def check_many_usernames(usernames: List[str], max_workers: int = 5) -> List[str]:
    """Параллельная проверка через Telethon (осторожно с лимитами)"""
    # Telethon не любит много параллельных запросов, делаем по 5 одновременно
    found = []
    for i in range(0, len(usernames), max_workers):
        batch = usernames[i:i + max_workers]
        tasks = [check_username_real(u) for u in batch]
        results = await asyncio.gather(*tasks)
        
        for username, is_free in zip(batch, results):
            if is_free:
                found.append(username)
        
        # Пауза между батчами
        await asyncio.sleep(0.5)
    
    return found

# --- Генерация случайных комбинаций (xwdeb, hadkdn, охтцо) ---
def generate_random_usernames(length: int, count: int = 20) -> List[str]:
    """Генерирует случайные комбинации латинских букв"""
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    while len(variants) < count:
        username = ''.join(random.choices(letters, k=length))
        variants.add(username)
    
    return list(variants)

def generate_with_keyword(keyword: str, length: int, count: int = 20) -> List[str]:
    """Генерирует комбинации с ключевым словом"""
    variants = set()
    letters = "abcdefghijklmnopqrstuvwxyz"
    
    if len(keyword) >= length:
        return [keyword[:length]]
    
    remaining = length - len(keyword)
    
    while len(variants) < count:
        position = random.choice(["start", "end"])
        
        if position == "start":
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
        f"{em('bot')} <b>Pull Search</b>\n"
        f"{em('info')} Бот для поиска свободных юзернеймов Telegram.\n\n"
        f"• Поисков (5 букв): 2\n"
        f"• Поисков (6 букв): 2\n"
        f"• Поисков (маска): 3",
        reply_markup=get_main_keyboard()
    )

@router.message(F.text == "🔍 Поиск")
async def menu_search(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    await message.answer(
        f"{em('search')} <b>Выбери тип поиска:</b>\n\n"
        f"• Поисков (5 букв): {user[4]}\n"
        f"• Поисков (6 букв): {user[5]}\n"
        f"• Поисков (маска): {user[6]}",
        reply_markup=get_search_type_keyboard()
    )

@router.message(F.text == "👤 Профиль")
async def menu_profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_or_update_user(message.from_user.id, message.from_user.username)
        user = await get_user(message.from_user.id)
    
    text = (
        f"{em('profile')} <b>PULL SEARCH</b>\n\n"
        f"• Поисков (фильтр): {user[6]}\n"
        f"• Поисков (5 букв): {user[4]}\n"
        f"• Поисков (6 букв): {user[5]}\n"
        f"• Поисков (маска): {user[6]}\n\n"
        f"Всего поисков: {user[2]}\n"
        f"Найдено ников: {user[3]}\n"
        f"Рефералов: {user[7]}\n\n"
        f"[Регистрация] https://t.me/PullSearchBot\n"
        f"{datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"Кол-во выдачи за 1 поиск: 1"
    )
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
        f"{em('bot')} <b>Главное меню</b>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("search_len_"))
async def search_len_handler(callback: CallbackQuery):
    length = int(callback.data.split("_")[-1])
    search_type = str(length)
    
    # Проверяем лимиты
    if not await decrement_search_count(callback.from_user.id, search_type):
        await callback.answer("❌ У тебя закончились поиски этого типа!", show_alert=True)
        return
    
    user = await get_user(callback.from_user.id)
    
    await callback.message.edit_text(
        f"{em('loading')} <b>Ищу свободные юзернеймы из {length} букв...</b>\n"
        f"Поисков осталось: {user[4] if length == 5 else user[5] - 1}",
        reply_markup=get_back_button()
    )
    
    await add_last_search(callback.from_user.id, f"{length} букв")
    
    # Генерируем и проверяем
    variants = generate_random_usernames(length, 15)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(callback.from_user.id)
        text = f"{em('check')} <b>PULL SEARCH</b>\n\n"
        text += f"Найдено ({length} букв): {len(found)}\n"
        for u in found[:5]:
            text += f"• @{u}\n"
        text += f"\nПоисков {length} букв осталось: {user[4] if length == 5 else user[5] - 1}"
    else:
        text = f"{em('cross')} <b>Ничего не найдено</b>\nПопробуй другую длину."
    
    await callback.message.edit_text(text, reply_markup=get_back_button())
    await callback.answer()

@router.callback_query(F.data == "search_keyword")
async def search_keyword_start(callback: CallbackQuery, state: FSMContext):
    # Проверяем лимиты маски
    if not await decrement_search_count(callback.from_user.id, "mask"):
        await callback.answer("❌ У тебя закончились поиски по маске!", show_alert=True)
        return
    
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
    
    user = await get_user(message.from_user.id)
    
    await message.answer(
        f"{em('loading')} <b>Ищу юзернеймы с '{keyword}' ({length} букв)...</b>\n"
        f"Поисков маска осталось: {user[6] - 1}",
        reply_markup=get_main_keyboard()
    )
    
    await add_last_search(message.from_user.id, f"'{keyword}' ({length})")
    
    # Генерируем и проверяем
    variants = generate_with_keyword(keyword, length, 15)
    found = await check_many_usernames(variants)
    
    if found:
        await add_found_nick(message.from_user.id)
        text = f"{em('check')} <b>PULL SEARCH</b>\n\n"
        text += f"Найдено с '{keyword}': {len(found)}\n"
        for u in found[:5]:
            text += f"• @{u}\n"
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
    
    # Проверяем что юзернейм занят
    is_free = await check_username_real(username)
    if is_free:
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

# --- Жизненный цикл ---
@dp.startup()
async def on_startup():
    await init_db()
    await telethon_client.start()
    logger.info("Бот запущен, Telethon подключен")

@dp.shutdown()
async def on_shutdown():
    await telethon_client.disconnect()
    logger.info("Бот остановлен")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
