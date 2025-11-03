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
import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional, List

from logging.handlers import RotatingFileHandler

import aiosqlite
import pytz


from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from dotenv import load_dotenv, find_dotenv  # NEW
load_dotenv(find_dotenv())                   # NEW: подхватить .env из текущей папки
# если .env лежит не рядом со скриптом:
# load_dotenv("/полный/путь/к/.env")

LOGGER_NAME = "postbot"
logger = logging.getLogger(LOGGER_NAME)


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

DAILY_SLOTS = _parse_slots_from_env()
logger.info(
    "Активные временные слоты: %s",
    ", ".join(slot.strftime("%H:%M") for slot in DAILY_SLOTS),
)

DB_PATH = "queue.db"
logger.info("Файл очереди: %s", DB_PATH)

# ---------------------- Data model / storage ----------------------

@dataclass
class QueueItem:
    id: int
    kind: str        # 'text' | 'photo'
    payload: str     # text or file_id
    caption: str     # optional (photo)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS queue (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  kind     TEXT    NOT NULL,
  payload  TEXT    NOT NULL,
  caption  TEXT    NOT NULL DEFAULT '',
  created  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

async def db_init() -> None:
    logger.info("Инициализация базы данных: %s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def enqueue(kind: str, payload: str, caption: str = "") -> None:
    logger.info(
        "Элемент добавлен в очередь: тип=%s, длина_данных=%d, длина_подписи=%d",
        kind,
        len(payload or ""),
        len(caption or ""),
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO queue(kind, payload, caption) VALUES (?, ?, ?)",
            (kind, payload, caption or "")
        )
        await db.commit()

async def dequeue() -> Optional[QueueItem]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, kind, payload, caption FROM queue ORDER BY id ASC LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM queue WHERE id = ?", (row[0],))
        await db.commit()
        logger.debug("Из очереди извлечён элемент #%s (%s)", row[0], row[1])
        return QueueItem(id=row[0], kind=row[1], payload=row[2], caption=row[3] or "")

async def peek_many(n: int = 10) -> List[QueueItem]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, kind, payload, caption FROM queue ORDER BY id ASC LIMIT ?",
            (n,)
        )
        rows = await cur.fetchall()
        return [QueueItem(*r) for r in rows]

async def purge() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM queue")
        await db.commit()
    logger.warning("Очередь очищена")


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
    slots_txt = ", ".join([s.strftime("%H:%M") for s in DAILY_SLOTS])
    logger.info("Команда /start от %s", _actor(update))
    await update.message.reply_text(
        "Привет! Кидай мне текст, фото или видео с подписью — я поставлю в очередь.\n"
        f"Публикую в канале по слотам: {slots_txt} ({TZ_NAME}).\n"
        "Команды: /queue — показать очередь; /purge — очистить."
    )

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    }
    for it in items:
        icon = icon_by_kind.get(it.kind, "❔")
        has_caption = it.kind in {"photo", "video"} and it.caption
        preview_src = it.caption if has_caption else it.payload
        preview = (preview_src or "").replace("\n", " ")[:70]
        lines.append(f"{icon} #{it.id}  {preview}")
    await update.message.reply_text("Ближайшие посты:\n" + "\n".join(lines))

async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Команда /purge от %s", _actor(update))
    await purge()
    await update.message.reply_text("Очередь очищена 🧹")

async def h_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    logger.info("Получен текст от %s (длина=%d)", _actor(update), len(text))
    await enqueue("text", text, "")
    await update.message.reply_text("Добавил в очередь 🧾")

async def h_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # берем самое большое фото (последний элемент) и сохраняем file_id
    photo = update.message.photo[-1]
    file_id = photo.file_id
    caption = update.message.caption or ""
    logger.info(
        "Получено фото от %s (caption_len=%d)",
        _actor(update),
        len(caption),
    )
    await enqueue("photo", file_id, caption)
    await update.message.reply_text("Фото добавлено в очередь 🖼️")

async def h_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    if not video:
        return
    file_id = video.file_id
    caption = update.message.caption or ""
    logger.info(
        "Получено видео от %s (duration=%s, file_size=%s, caption_len=%d)",
        _actor(update),
        getattr(video, "duration", "unknown"),
        getattr(video, "file_size", "unknown"),
        len(caption),
    )
    await enqueue("video", file_id, caption)
    await update.message.reply_text("Видео добавлено в очередь 🎞️")

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

    logger.info("Начинаем публикацию элемента #%s (%s)", item.id, item.kind)
    try:
        if item.kind == "text":
            await context.bot.send_message(
                chat_id=TARGET_CHAT, text=item.payload, parse_mode=ParseMode.HTML
            )
        elif item.kind == "photo":
            await context.bot.send_photo(
                chat_id=TARGET_CHAT,
                photo=item.payload,  # file_id
                caption=item.caption or None,
                parse_mode=ParseMode.HTML
            )
        elif item.kind == "video":
            await context.bot.send_video(
                chat_id=TARGET_CHAT,
                video=item.payload,
                caption=item.caption or None,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
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

# ---------------------- Application / Polling ----------------------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(2).build()
    app.job_queue.scheduler.configure(timezone=TZ)

    logger.info("Регистрация обработчиков команд и сообщений")
    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("purge", cmd_purge))

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
    return app

def main():
    logger.info("Запуск цикла приложения")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db_init())
    app = build_app()

    # Бережный long-polling:
    # - poll_interval=0.0 — без пауз между вызовами getUpdates (сервер держит соединение)
    # - timeout=25        — длинный таймаут long-poll на стороне Telegram
    # - read_timeout=35   — ждём сеть подольше (NAT, DSM)
    # - allowed_updates   — только "message", чтобы не тянуть лишнее
    # - drop_pending_updates=True — не забирать старые апдейты из истории при рестарте
    logger.info("Старт long-polling")
    app.run_polling(
        poll_interval=0.0,
        timeout=25,
        read_timeout=35,
        allowed_updates=["message"],
        drop_pending_updates=True,
        stop_signals=None,   # корректно завершится по Ctrl+C/kill
    )

    logger.info("Пуллинг завершён, очищаем контекст event loop")
    asyncio.set_event_loop(None)

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

if __name__ == "__main__":
    # Универсально: работает корректно в консольном режиме
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Получен KeyboardInterrupt — завершаемся по запросу оператора")
