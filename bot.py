#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram queue publisher for channels (polling, FIFO, 5 slots/day).
- Input: send TEXT or PHOTO to the bot in DM.
- Storage: SQLite queue (FIFO).
- Output: posts to your channel by time slots (Europe/Belgrade).
Env:
  TG_BOT_TOKEN=...               # required
  TG_CHANNEL=@your_channel       # preferred (string) OR
  TG_CHANNEL_ID=-1001234567890   # alternative (int)
  TZ=Europe/Belgrade             # optional, default Europe/Belgrade
  POST_SLOTS=10:00,13:00,16:00,19:00,22:00   # optional
"""

import os
import json
import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dtime, datetime
from typing import Optional, List

from logging.handlers import RotatingFileHandler

import aiosqlite
import sqlite3
import pytz


from telegram import (
    Update, 
    InputMediaPhoto, 
    InputMediaVideo, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

from dotenv import load_dotenv, find_dotenv  # NEW
load_dotenv(find_dotenv())                   # NEW: подхватить .env из текущей папки

import x_publisher  # NEW: интеграция с X
# если .env лежит не рядом со скриптом:
# load_dotenv("/полный/путь/к/.env")

LOGGER_NAME = "postbot"
logger = logging.getLogger(LOGGER_NAME)

ALBUM_BUFFER_KEY = "album_buffer"
ALBUM_FLUSH_JOBS_KEY = "album_flush_jobs"
ALBUM_FLUSH_DELAY = float(os.getenv("POSTBOT_ALBUM_DELAY", "1.5"))
MAX_ALBUM_ITEMS = int(os.getenv("POSTBOT_ALBUM_MAX", "10"))


def _safe_int_env(name: str, default: int) -> int:
    """
    Возвращает положительное целое значение из переменной окружения или дефолт.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def setup_logging() -> None:
    """
    Настраивает консольное логирование и ротацию лог-файла.
    """
    level_name = os.getenv("POSTBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    handlers: List[logging.Handler] = []

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(fmt))
    handlers.append(console_handler)

    log_file = (os.getenv("POSTBOT_LOG_FILE", "postbot.log") or "").strip()
    if log_file:
        max_bytes = _safe_int_env("POSTBOT_LOG_MAX_BYTES", 1_048_576)
        backup_count = _safe_int_env("POSTBOT_LOG_BACKUP_COUNT", 5)
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(fmt))
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logger.info(
        "Логирование настроено: уровень=%s, файл=%s, handlers=%d",
        logging.getLevelName(level),
        log_file or "disabled",
        len(handlers),
    )


setup_logging()

# ---------------------- Config ----------------------

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("Переменная окружения TG_BOT_TOKEN не найдена")
    raise SystemExit("Set TG_BOT_TOKEN env var")

CHANNEL = os.getenv("TG_CHANNEL")  # e.g. @your_channel
CHANNEL_ID_ENV = os.getenv("TG_CHANNEL_ID")  # e.g. -100...
CHANNEL_ID = int(CHANNEL_ID_ENV) if CHANNEL_ID_ENV else None
TARGET_CHAT = CHANNEL if CHANNEL else CHANNEL_ID
if not TARGET_CHAT:
    logger.critical("Не задан TG_CHANNEL или TG_CHANNEL_ID")
    raise SystemExit("Set TG_CHANNEL (e.g. @your_channel) or TG_CHANNEL_ID (-100...)")
logger.info("Работаем с целевым чатом: %s", TARGET_CHAT)

TZ_NAME = os.getenv("TZ", "Europe/Belgrade")
TZ = pytz.timezone(TZ_NAME)
logger.info("Таймзона запланированных публикаций: %s", TZ_NAME)

# default 5 slots/day; can override via POST_SLOTS env ("HH:MM,HH:MM,...")
def _parse_slots_from_env() -> List[dtime]:
    raw = os.getenv("POST_SLOTS", "07:30,11:30,14:05,17:30,21:34")
    slots: List[dtime] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hh, mm = chunk.split(":")
        slots.append(dtime(int(hh), int(mm), tzinfo=TZ))
    return slots

POSTBOT_SIGNATURE = os.getenv("POSTBOT_SIGNATURE", "")

def _apply_signature(caption: str) -> str:
    """Добавляет глобальную подпись к тексту."""
    if not POSTBOT_SIGNATURE:
        return caption
    return f"{caption}{POSTBOT_SIGNATURE}".strip()

DAILY_SLOTS = _parse_slots_from_env()
logger.info(
    "Активные временные слоты: %s",
    ", ".join(slot.strftime("%H:%M") for slot in DAILY_SLOTS),
)

DB_PATH = "queue.db"
STORAGE_DIR = os.getenv("POSTBOT_STORAGE_DIR", "storage")
ADMIN_ID_RAW = os.getenv("POSTBOT_ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else None
AUTO_SYNC = os.getenv("POSTBOT_AUTO_SYNC", "1") == "1"

logger.info("Файл очереди: %s, Директория хранения: %s", DB_PATH, STORAGE_DIR)
if ADMIN_ID:
    logger.info("ID администратора для отчетов: %s", ADMIN_ID)


# ---------------------- Data model / storage ----------------------

@dataclass
class QueueItem:
    id: int
    kind: str        # 'text' | 'photo' | 'video' | 'album'
    payload: str     # text or LOCAL PATH (or JSON for album)
    caption: str     # optional
    target: str      # 'tg' | 'x' | 'both'
    asset_id: Optional[int] = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS queue (
  target   TEXT    NOT NULL DEFAULT 'both',
  asset_id INTEGER,
  created  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS assets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  path         TEXT    NOT NULL,
  kind         TEXT    NOT NULL,
  caption      TEXT    NOT NULL DEFAULT '',
  source       TEXT    NOT NULL DEFAULT 'direct',
  is_published INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""


def _compute_next_slot(now: datetime) -> Optional[dtime]:
    if not DAILY_SLOTS:
        return None
    ordered = sorted(DAILY_SLOTS, key=lambda t: (t.hour, t.minute))
    for slot in ordered:
        slot_today = now.replace(
            hour=slot.hour,
            minute=slot.minute,
            second=0,
            microsecond=0,
        )
        if slot_today >= now:
            return slot
    return ordered[0]



async def db_init() -> None:
    logger.info("Инициализация базы данных: %s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        
        try:
            await db.execute("ALTER TABLE queue ADD COLUMN asset_id INTEGER")
            logger.info("База данных обновлена: добавлена колонка 'asset_id' в queue")
        except sqlite3.OperationalError:
            pass

        try:
            await db.execute("ALTER TABLE assets ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0")
            logger.info("База данных обновлена: добавлена колонка 'is_published' в assets")
        except sqlite3.OperationalError:
            pass

        await db.commit()

async def save_media_locally(bot, file_id: str, kind: str) -> str:
    """
    Скачивает файл из Telegram и сохраняет его в папку storage. 
    Возвращает относительный путь к файлу.
    """
    now = datetime.now()
    # Структура: storage/год/месяц/день_время_тип_id.ext
    rel_dir = os.path.join(STORAGE_DIR, str(now.year), f"{now.month:02d}")
    abs_dir = os.path.join(os.getcwd(), rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    
    file = await bot.get_file(file_id)
    ext = "jpg" if kind == "photo" else "mp4"
    if hasattr(file, 'file_path') and file.file_path:
        ext = file.file_path.split('.')[-1]
        
    filename = f"{now.strftime('%d_%H%M%S')}_{kind}_{file_id[:8]}.{ext}"
    rel_path = os.path.join(rel_dir, filename)
    abs_path = os.path.join(abs_dir, filename)
    
    await file.download_to_drive(abs_path)
    logger.info("Файл сохранен локально: %s", rel_path)
    return rel_path

async def enqueue(kind: str, payload: str, caption: str = "", target: str = "both", asset_id: Optional[int] = None) -> None:
    logger.info(
        "Элемент добавлен в очередь: тип=%s, цель=%s, asset_id=%s, длина_данных=%d",
        kind, target, asset_id, len(payload or "")
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO queue (kind, payload, caption, target, asset_id) VALUES (?, ?, ?, ?, ?)",
            (kind, payload, caption or "", target, asset_id)
        )
        await db.commit()


def _album_buffer(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault(ALBUM_BUFFER_KEY, {})


def _album_jobs(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault(ALBUM_FLUSH_JOBS_KEY, {})


def _schedule_album_flush(context: ContextTypes.DEFAULT_TYPE, media_group_id: str, chat_id: int) -> None:
    jobs = _album_jobs(context)
    if existing := jobs.pop(media_group_id, None):
        existing.schedule_removal()
    # Создаем задачу на сброс буфера через 3 секунды
    job = context.job_queue.run_once(
        _flush_album_buffer,
        when=3,
        data={"media_group_id": media_group_id, "chat_id": chat_id},  # Передаем chat_id
        name=f"flush_album_{media_group_id}",
    )
    jobs[media_group_id] = job


async def _flush_album_buffer(context: ContextTypes.DEFAULT_TYPE) -> None:
    media_group_id = context.job.data.get("media_group_id")
    buffer = _album_buffer(context)
    entry = buffer.pop(media_group_id, None)
    _album_jobs(context).pop(media_group_id, None)
    if not entry or not entry.get("items"):
        logger.debug("Буфер альбома %s пуст — пропуск", media_group_id)
        return
    caption = entry.get("caption") or ""
    # Для альбомов мы сохраняем JSON со списком локальных путей
    items_with_local_paths = []
    for it in entry["items"]:
        local_path = await save_media_locally(context.bot, it["file_id"], it["type"])
        items_with_local_paths.append({"type": it["type"], "path": local_path})
        
    # Мы не можем использовать update здесь напрямую, так как это Job
    # Но мы сохранили chat_id в metadata при создании Job
    chat_id = context.job.data.get("chat_id")
    
    # Сохраняем альбом в активы и спрашиваем платформу
    payload = json.dumps(items_with_local_paths)
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO assets (path, kind, caption, source) VALUES (?, ?, ?, ?)",
            (payload, "album", caption, "direct")
        )
        asset_id = cur.lastrowid
        await db.commit()
    
    context.user_data["pending_post"] = {"kind": "album", "payload": payload, "caption": caption, "asset_id": asset_id}
    
    if chat_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📚 Альбом ({len(items_with_local_paths)} медиа) сохранен. Куда его опубликовать?",
            reply_markup=_get_selection_keyboard("album", payload, caption)
        )
    else:
        # Если chat_id почему-то нет, просто в очередь (fallback)
        await enqueue("album", payload, caption, target="both")
    logger.info(
        "Альбом media_group_id=%s отправлен в очередь (%d элементов)",
        media_group_id,
        len(entry["items"]),
    )


def _handle_media_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    file_id: str,
    caption: str,
) -> bool:
    message = update.message
    if not message:
        return False
    media_group_id = message.media_group_id
    if not media_group_id:
        return False

    buffer = _album_buffer(context)
    entry = buffer.setdefault(media_group_id, {"items": [], "caption": caption or ""})
    if caption and not entry.get("caption"):
        entry["caption"] = caption

    if len(entry["items"]) >= MAX_ALBUM_ITEMS:
        logger.warning(
            "Альбом media_group_id=%s достиг лимита %d — элемент отброшен",
            media_group_id,
            MAX_ALBUM_ITEMS,
        )
    else:
        entry["items"].append({"type": kind, "file_id": file_id})
        logger.debug(
            "Альбом media_group_id=%s пополнен (%d/%d)",
            media_group_id,
            len(entry["items"]),
            MAX_ALBUM_ITEMS,
        )

    _schedule_album_flush(context, media_group_id)
    return True


async def dequeue() -> Optional[QueueItem]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, kind, payload, caption, target, asset_id FROM queue ORDER BY id LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                item = QueueItem(*row)
                await db.execute("DELETE FROM queue WHERE id = ?", (item.id,))
                await db.commit()
                logger.debug("Из очереди извлечён элемент #%s (%s)", item.id, item.kind)
                return item
    return None

async def peek_many(n: int = 10) -> List[QueueItem]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, kind, payload, caption, target, asset_id FROM queue ORDER BY id ASC LIMIT ?",
            (n,)
        )
        rows = await cur.fetchall()
        return [QueueItem(*r) for r in rows]

async def queue_size() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM queue")
        row = await cur.fetchone()
        return int(row[0] if row and row[0] is not None else 0)

async def purge() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM queue")
        await db.commit()
    logger.warning("Очередь очищена")


async def sync_storage_to_db():
    """
    Сканирует папку storage и синхронизирует её с assets и queue.
    """
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR, exist_ok=True)
        return

    logger.info("Начало синхронизации папки %s с базой данных...", STORAGE_DIR)
    added_assets = 0
    added_queue = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for root, dirs, files in os.walk(STORAGE_DIR):
            for file in files:
                if file.startswith('.') or file == "postbot.log" or "@eaDir" in root or "@eaDir" in file:
                    continue
                
                rel_path = os.path.relpath(os.path.join(root, file), os.getcwd())
                
                # Ищем в assets
                async with db.execute("SELECT id, is_published FROM assets WHERE path = ?", (rel_path,)) as cur:
                    asset = await cur.fetchone()
                
                asset_id = None
                is_published = 0
                
                if not asset:
                    # Добавляем в assets
                    ext = file.split('.')[-1].lower()
                    kind = "photo" if ext in ("jpg", "jpeg", "png", "webp") else "video"
                    cur = await db.execute(
                        "INSERT INTO assets (path, kind, source) VALUES (?, ?, ?)",
                        (rel_path, kind, "sync")
                    )
                    asset_id = cur.lastrowid
                    added_assets += 1
                    logger.debug("Синхронизация: файл %s добавлен в assets", rel_path)
                else:
                    asset_id, is_published = asset

                if not is_published:
                    # Проверяем, нет ли его уже в очереди
                    async with db.execute("SELECT id FROM queue WHERE asset_id = ?", (asset_id,)) as cur:
                        in_queue = await cur.fetchone()
                    
                    if not in_queue:
                        # Добавляем в конец очереди
                        ext = file.split('.')[-1].lower()
                        kind = "photo" if ext in ("jpg", "jpeg", "png", "webp") else "video"
                        await db.execute(
                            "INSERT INTO queue (kind, payload, asset_id) VALUES (?, ?, ?)",
                            (kind, rel_path, asset_id)
                        )
                        added_queue += 1
                        logger.debug("Синхронизация: файл %s добавлен в очередь", rel_path)
        
        await db.commit()
    
    logger.info("Синхронизация завершена: +%d в архив, +%d в очередь", added_assets, added_queue)


def _actor(update: Update) -> str:
    """
    Возвращает идентификатор пользователя для логов.
    """
    user = update.effective_user
    if user and user.id:
        return f"id={user.id}"
    return "unknown"

# ---------------------- Handlers ----------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        slots_txt = ", ".join([s.strftime("%H:%M") for s in DAILY_SLOTS])
        logger.info("Команда /start от %s", _actor(update))
        await update.message.reply_text(
            "Привет! Кидай мне текст, фото или видео с подписью — я поставлю в очередь.\n"
            f"Публикую в канале по слотам: {slots_txt} ({TZ_NAME}).\n"
            "Команды: /queue — показать очередь; /health — статус бота; /now — пост сейчас."
        )
    except Exception as e:
        logger.exception("Ошибка в cmd_start: %s", e)

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Команда /queue от %s", _actor(update))
        items = await peek_many(20)
        if not items:
            await update.message.reply_text("Очередь пуста ✅")
            return
        lines = []
        icon_by_kind = {
            "text": "📝",
            "photo": "🖼️",
            "video": "🎞️",
            "album": "📚",
        }
        for it in items:
            icon = icon_by_kind.get(it.kind, "❔")
            if it.kind == "album":
                try:
                    album_items = json.loads(it.payload)
                except json.JSONDecodeError:
                    album_items = []
                caption = (it.caption or "").replace("\n", " ")[:50]
                preview = f"{len(album_items)} media"
                if caption:
                    preview = f"{preview} — {caption}"
            else:
                has_caption = it.kind in {"photo", "video"} and it.caption
                preview_src = it.caption if has_caption else it.payload
                preview = (preview_src or "").replace("\n", " ")[:70]
            lines.append(f"{icon} #{it.id}  {preview}")
        await update.message.reply_text("Ближайшие посты:\n" + "\n".join(lines))
    except Exception as e:
        logger.exception("Ошибка в cmd_queue: %s", e)
        await update.message.reply_text("⚠️ Ошибка при чтении очереди.")

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Команда /health от %s", _actor(update))
        size = await queue_size()
        now_local = datetime.now(TZ)
        slot = _compute_next_slot(now_local)
        slot_txt = slot.strftime("%H:%M") if slot else "—"
        await update.message.reply_text(
            f"Бот жив, {size} сообщений в очереди, ближайший слот {slot_txt}"
        )
    except Exception as e:
        logger.exception("Ошибка в cmd_health: %s", e)
        await update.message.reply_text("⚠️ Ошибка при проверке статуса.")

async def cmd_publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принудительно публикует следующий элемент из очереди прямо сейчас.
    """
    logger.info("Команда /publish_now от %s", _actor(update))
    size = await queue_size()
    if size == 0:
        await update.message.reply_text("Очередь пуста, нечего публиковать 🤷‍♂️")
        return
    
    await update.message.reply_text("Запускаю внеочередную публикацию... 🚀")
    await publish_next(context)

async def cmd_restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Берет N случайных элементов из неопубликованных assets и добавляет их в очередь.
    Использование: /restock [количество]
    """
    try:
        logger.info("Команда /restock от %s", _actor(update))
        args = context.args
        count = 10
        if args and args[0].isdigit():
            count = int(args[0])
        
        count = min(max(count, 1), 100)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Берем только то, что еще не опубликовано и не в очереди
            query = """
                SELECT id, path, kind, caption 
                FROM assets 
                WHERE is_published = 0 
                  AND id NOT IN (SELECT asset_id FROM queue WHERE asset_id IS NOT NULL)
                ORDER BY id ASC 
                LIMIT ?
            """
            async with db.execute(query, (count,)) as cursor:
                rows = await cursor.fetchall()
                
            if not rows:
                await update.message.reply_text("Нет новых материалов для добавления в очередь.")
                return

            for a_id, path, kind, caption in rows:
                await enqueue(kind, path, caption, target="both", asset_id=a_id)
            await db.commit()
        
        await update.message.reply_text(f"✅ Очередь успешно пополнена: добавлено {len(rows)} новых постов.")
    except Exception as e:
        logger.exception("Ошибка в cmd_restock: %s", e)
        await update.message.reply_text("⚠️ Ошибка при пополнении очереди.")

async def h_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    logger.info("Получен текст от %s (длина=%d)", _actor(update), len(text))
    await enqueue("text", text, "")
    await update.message.reply_text("Добавил в очередь 🧾")

# ---------------------- Handlers ----------------------

def _get_selection_keyboard(kind: str, payload: str, caption: str = "") -> InlineKeyboardMarkup:
    """Создает кнопки для выбора платформы."""
    # Мы кодируем данные в callback_data. 
    # Так как лимит 64 символа, для альбомов (JSON) и длинных путей 
    # мы будем использовать временное хранилище в context или просто полагаться на assets ID.
    # Для простоты: 'pub|<kind>|<target>|<payload_or_id>'
    # Но лучше: сохранить метаданные в assets и передавать ID.
    
    keyboard = [
        [
            InlineKeyboardButton("📱 Telegram", callback_data=f"p:tg"),
            InlineKeyboardButton("🐦 X.com", callback_data=f"p:x"),
        ],
        [InlineKeyboardButton("🌍 Везде", callback_data=f"p:both")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("p:"):
        return
        
    target = data.split(":")[1]
    
    # Извлекаем сохраненные данные из user_data (потому что в callback_data не влезет всё)
    pending = context.user_data.get("pending_post")
    if not pending:
        await query.edit_message_text("❌ Ошибка: данные поста устарели. Пришлите заново.")
        return
        
    kind = pending["kind"]
    payload = pending["payload"]
    caption = pending["caption"]
    asset_id = pending.get("asset_id")
    
    await enqueue(kind, payload, caption, target, asset_id)
    context.user_data.pop("pending_post", None)
    
    target_names = {"tg": "Telegram", "x": "X.com", "both": "Везде"}
    logger.info("Пост (#%s) добавлен в очередь для %s", kind, target_names[target])
    await query.edit_message_text(f"✅ Добавлено в очередь! Назначение: {target_names[target]}")

async def h_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Получено фото от %s", _actor(update))
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    
    path = await save_media_locally(context.bot, photo.file_id, "photo")
    
    # Сохраняем в активы "золотого фонда"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO assets (path, kind, caption, source) VALUES (?, ?, ?, ?)",
            (path, "photo", caption, "direct")
        )
        asset_id = cur.lastrowid
        await db.commit()
    
    # Вместо немедленной очереди — спрашиваем
    context.user_data["pending_post"] = {"kind": "photo", "payload": path, "caption": caption, "asset_id": asset_id}
    await update.message.reply_text(
        "🖼️ Фото сохранено. Куда его опубликовать?", 
        reply_markup=_get_selection_keyboard("photo", path, caption)
    )

async def h_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Получено видео от %s", _actor(update))
    video = update.message.video
    caption = update.message.caption or ""
    
    path = await save_media_locally(context.bot, video.file_id, "video")
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO assets (path, kind, caption, source) VALUES (?, ?, ?, ?)",
            (path, "video", caption, "direct")
        )
        asset_id = cur.lastrowid
        await db.commit()

    context.user_data["pending_post"] = {"kind": "video", "payload": path, "caption": caption, "asset_id": asset_id}
    await update.message.reply_text(
        "📹 Видео сохранено. Куда его опубликовать?", 
        reply_markup=_get_selection_keyboard("video", path, caption)
    )
    # The original code had a return here, but it was removed in the diff.
    # The original code also had an enqueue call after the _handle_media_group check.
    # The diff replaces the enqueue with the new button logic.
    # I will follow the diff and remove the original enqueue and reply_text.
    # The _handle_media_group logic remains, and if it returns True, the function should exit.
    # If _handle_media_group returns False, then the new button logic applies.
    # Let's re-evaluate the h_video and h_photo structure based on the diff.

    # Original h_video:
    # if _handle_media_group(...):
    #     await update.message.reply_text("Альбом обновлён, жду остальные кадры 📚")
    #     return
    # local_path = await save_media_locally(...)
    # await enqueue(...)
    # await update.message.reply_text("Видео добавлено в локальный архив и очередь 🎞️")

    # Diff for h_video:
    # logger.info("Получено видео от %s", _actor(update))
    # video = update.message.video
    # caption = update.message.caption or ""
    # path = await save_media_locally(context.bot, video.file_id, "video")
    # async with aiosqlite.connect(DB_PATH) as db:
    #     await db.execute("INSERT INTO assets ...")
    #     await db.commit()
    # context.user_data["pending_post"] = {"kind": "video", "payload": path, "caption": caption}
    # await update.message.reply_text("📹 Видео сохранено. Куда его опубликовать?", reply_markup=_get_selection_keyboard(...))

    # The diff completely replaces the latter part of h_video.
    # This means the _handle_media_group check is implicitly removed from the new h_video logic.
    # This might be an oversight in the user's instruction, but I must follow the provided diff faithfully.
    # The diff also removes the `return` statement that was after the `_handle_media_group` call.
    # The instruction says "Обновить h_photo, h_video и _flush_album_buffer для использования кнопок."
    # The provided diff for h_video does not include the _handle_media_group check.
    # I will apply the diff as given.

    # Re-reading the diff:
    # The diff for h_video starts from `logger.info("Получено видео от %s", _actor(update))`.
    # It then completely replaces the rest of the function.
    # This means the `if _handle_media_group(...)` block is removed.
    # This is a significant change, but I must follow the diff.

    # The diff for h_photo also starts from `logger.info("Получено фото от %s", _actor(update))`.
    # It also completely replaces the rest of the function, removing the `if _handle_media_group(...)` block.

    # This implies that media group handling will no longer trigger the button selection,
    # and single photos/videos will. This seems like a logical separation.
    # The `_flush_album_buffer` is not in the diff, so I will not modify it.

    # Final check on h_video diff:
    # The diff ends with `await update.message.reply_text("📹 Видео сохранено. Куда его опубликовать?", reply_markup=_get_selection_keyboard("video", path, caption))`.
    # The original code had `return` after `_handle_media_group` and then `await enqueue` and `await update.message.reply_text`.
    # The diff replaces the `await enqueue` and `await update.message.reply_text` part.
    # The `return` statement from the original code (after `_handle_media_group`) is not present in the diff.
    # This means the `_handle_media_group` check is entirely gone from the new `h_video` and `h_photo` as per the diff.
    # I will proceed with the diff as provided.

# ---------------------- Publishing job ----------------------

async def publish_next(context: ContextTypes.DEFAULT_TYPE):
    """
    One message per slot. If queue empty — do nothing.
    On error: return item back to queue (tail) and backoff.
    """
    item = await dequeue()
    if not item:
        logger.debug("Очередь пуста — публикация пропущена")
        return

    logger.info("Начинаем публикацию элемента #%s (%s), назначение=%s", item.id, item.kind, item.target)
    try:
        # --- Публикация в Telegram ---
        if item.target in ("tg", "both"):
            if item.kind == "text":
                await context.bot.send_message(
                    chat_id=TARGET_CHAT, text=item.payload, parse_mode=ParseMode.HTML
                )
            elif item.kind == "photo":
                with open(item.payload, 'rb') as f:
                    await context.bot.send_photo(
                        chat_id=TARGET_CHAT,
                        photo=f,
                        caption=item.caption or None,
                        parse_mode=ParseMode.HTML
                    )
            elif item.kind == "video":
                with open(item.payload, 'rb') as f:
                    await context.bot.send_video(
                        chat_id=TARGET_CHAT,
                        video=f,
                        caption=item.caption or None,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                    )
            elif item.kind == "album":
                try:
                    album_items = json.loads(item.payload or "[]")
                except json.JSONDecodeError:
                    album_items = []
                    logger.error("Повреждённые данные альбома #%s — пропуск", item.id)
                if not album_items:
                    logger.warning("Альбом #%s пуст — удалён без публикации", item.id)
                    return
                media_objects: List = []
                for index, media in enumerate(album_items):
                    media_type = media.get("type")
                    file_path = media.get("path")
                    if not file_path or not os.path.exists(file_path):
                        logger.warning(
                            "Элемент альбома #%s без файла (position=%d) — пропуск",
                            item.id,
                            index,
                        )
                        continue
                    
                    f = open(file_path, 'rb')
                    if media_type == "photo":
                        obj = InputMediaPhoto(f)
                    elif media_type == "video":
                        obj = InputMediaVideo(f, supports_streaming=True)
                    else:
                        f.close()
                        logger.warning(
                            "Элемент альбома #%s с неизвестным типом '%s' — пропуск",
                            item.id,
                            media_type,
                        )
                        continue
                    if index == 0 and item.caption:
                        obj.caption = item.caption
                        obj.parse_mode = ParseMode.HTML
                    media_objects.append(obj)
                if not media_objects:
                    logger.warning("Все элементы альбома #%s отфильтрованы — пропуск", item.id)
                    return
                await context.bot.send_media_group(chat_id=TARGET_CHAT, media=media_objects)
        
        # --- Публикация в X ---
        if item.target in ("x", "both") and x_publisher.X_ENABLED:
            logger.info("Запуск параллельной публикации в X для элемента #%s", item.id)
            asyncio.create_task(x_publisher.publish_to_x(
                context.bot, 
                item.kind, 
                item.payload, 
                _apply_signature(item.caption)
            ))
        # ---------------------------
    except RetryAfter as e:
        # Telegram просит подождать e.retry_after секунд (Flood control)
        delay = int(getattr(e, "retry_after", 5)) + 1
        await enqueue(item.kind, item.payload, item.caption)  # вернуть назад
        logger.warning(
            "Публикацию #%s отложили из-за Flood control, повтор через %s сек",
            item.id,
            delay,
        )
        await asyncio.sleep(delay)
    except (TimedOut, NetworkError):
        # временный сбой сети: вернуть назад и позже повторить
        await enqueue(item.kind, item.payload, item.caption)
        logger.warning(
            "Сетевая ошибка при публикации #%s — повтор через 5 секунд",
            item.id,
        )
        await asyncio.sleep(5)
    except Exception:
        # непредвиденное: не теряем пост, возвращаем в хвост
        await enqueue(item.kind, item.payload, item.caption)
        logger.exception("Ошибка публикации элемента #%s", item.id)
    else:
        logger.info("Элемент #%s опубликован", item.id)
        if item.asset_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE assets SET is_published = 1 WHERE id = ?", (item.asset_id,))
                await db.commit()
                logger.debug("Статус актива #%s обновлен: is_published = 1", item.asset_id)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет ежедневный отчет администратору о состоянии очереди.
    """
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не настроен, отчет не отправлен")
        return

    size = await queue_size()
    slots_count = len(DAILY_SLOTS)
    days_left = size / slots_count if slots_count > 0 else 0
    
    msg = (
        "📊 <b>Ежедневный отчет по контенту</b>\n\n"
        f"🔹 Всего в очереди: <b>{size}</b> постов\n"
        f"🔹 Слотов в день: <b>{slots_count}</b>\n"
        f"🔹 Контента хватит на: <b>{days_left:.1f}</b> дн.\n\n"
    )
    
    if days_left < 3:
        msg += "⚠️ <b>ВНИМАНИЕ:</b> Контент заканчивается! Пора пополнить архив."
    else:
        msg += "✅ Всё в порядке, постинг продолжается."

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode=ParseMode.HTML)
        logger.info("Ежедневный отчет успешно отправлен админу %s", ADMIN_ID)
    except Exception as e:
        logger.error("Не удалось отправить отчет админу: %s", e)

# ---------------------- Application / Polling ----------------------

async def post_init(application: Application) -> None:
    """
    Выполняется при старте бота.
    """
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Запустить бота и справка"),
        BotCommand("queue", "Посмотреть очередь"),
        BotCommand("now", "Опубликовать следующий пост сейчас"),
        BotCommand("restock", "Пополнить очередь из архива"),
        BotCommand("health", "Статус бота и слотов"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Меню команд успешно установлено")
    
    # Инициализация БД и синхронизация при старте
    try:
        await db_init()
        if AUTO_SYNC:
            await sync_storage_to_db()
    except Exception as e:
        logger.error("Ошибка при инициализации/синхронизации: %s", e)

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(2).post_init(post_init).build()
    app.job_queue.scheduler.configure(timezone=TZ)

    logger.info("Регистрация обработчиков команд и сообщений")
    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("now", cmd_publish_now))
    app.add_handler(CommandHandler("publish_now", cmd_publish_now))
    app.add_handler(CommandHandler("restock", cmd_restock))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # контент
    app.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), h_photo))
    app.add_handler(MessageHandler(filters.VIDEO & (~filters.COMMAND), h_video))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), h_text))

    # ежедневные слоты
    for t in DAILY_SLOTS:
        app.job_queue.run_daily(
            publish_next,
            time=t,  # tz-aware: respect TZ
            days=(0, 1, 2, 3, 4, 5, 6),     # каждый день
            name=f"slot_{t.strftime('%H%M')}"
        )
    logger.info("Планировщик инициализирован: %d слотов", len(DAILY_SLOTS))

    # Планируем отчет за 30 минут до первого слота
    if DAILY_SLOTS:
        first_slot = min(DAILY_SLOTS, key=lambda t: (t.hour, t.minute))
        # Считаем время отчета (на 30 минут раньше)
        report_minutes = first_slot.hour * 60 + first_slot.minute - 30
        if report_minutes < 0:
            report_minutes += 24 * 60
        
        report_time = dtime(report_minutes // 60, report_minutes % 60, tzinfo=TZ)
        app.job_queue.run_daily(
            daily_report,
            time=report_time,
            name="daily_content_report"
        )
        logger.info("Ежедневный отчет запланирован на %s", report_time.strftime("%H:%M"))

    return app


def main():
    try:
        logger.info("Запуск приложения")
        app = build_app()
        
        logger.info("Старт long-polling...")
        # run_polling блокирует поток до завершения работы приложения
        app.run_polling(
            poll_interval=0.0,
            timeout=25,
            read_timeout=35,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )
    except Exception:
        logger.exception("Критическая ошибка при работе бота")
    finally:
        logger.info("Приложение остановлено")

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

if __name__ == "__main__":
    main()
