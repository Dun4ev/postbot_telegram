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
from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional, List

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

# ---------------------- Config ----------------------

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Set TG_BOT_TOKEN env var")

CHANNEL = os.getenv("TG_CHANNEL")  # e.g. @your_channel
CHANNEL_ID_ENV = os.getenv("TG_CHANNEL_ID")  # e.g. -100...
CHANNEL_ID = int(CHANNEL_ID_ENV) if CHANNEL_ID_ENV else None
TARGET_CHAT = CHANNEL if CHANNEL else CHANNEL_ID
if not TARGET_CHAT:
    raise SystemExit("Set TG_CHANNEL (e.g. @your_channel) or TG_CHANNEL_ID (-100...)")

TZ_NAME = os.getenv("TZ", "Europe/Belgrade")
TZ = pytz.timezone(TZ_NAME)

# default 5 slots/day; can override via POST_SLOTS env ("HH:MM,HH:MM,...")
def _parse_slots_from_env() -> List[dtime]:
    raw = os.getenv("POST_SLOTS", "10:00,13:00,16:00,23:41,23:42")
    slots: List[dtime] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        hh, mm = chunk.split(":")
        slots.append(dtime(int(hh), int(mm), tzinfo=TZ))
    return slots

DAILY_SLOTS = _parse_slots_from_env()

DB_PATH = "queue.db"

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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def enqueue(kind: str, payload: str, caption: str = "") -> None:
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

# ---------------------- Handlers ----------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slots_txt = ", ".join([s.strftime("%H:%M") for s in DAILY_SLOTS])
    await update.message.reply_text(
        "Привет! Кидай мне текст или фото с подписью — я поставлю в очередь.\n"
        f"Публикую в канале по слотам: {slots_txt} ({TZ_NAME}).\n"
        "Команды: /queue — показать очередь; /purge — очистить."
    )

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = await peek_many(20)
    if not items:
        await update.message.reply_text("Очередь пуста ✅")
        return
    lines = []
    for it in items:
        icon = "📝" if it.kind == "text" else "🖼️"
        preview_src = it.caption if it.kind == "photo" and it.caption else it.payload
        preview = (preview_src or "").replace("\n", " ")[:70]
        lines.append(f"{icon} #{it.id}  {preview}")
    await update.message.reply_text("Ближайшие посты:\n" + "\n".join(lines))

async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await purge()
    await update.message.reply_text("Очередь очищена 🧹")

async def h_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    await enqueue("text", text, "")
    await update.message.reply_text("Добавил в очередь 🧾")

async def h_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # берем самое большое фото (последний элемент) и сохраняем file_id
    photo = update.message.photo[-1]
    file_id = photo.file_id
    caption = update.message.caption or ""
    await enqueue("photo", file_id, caption)
    await update.message.reply_text("Фото добавлено в очередь 🖼️")

# ---------------------- Publishing job ----------------------

async def publish_next(context: ContextTypes.DEFAULT_TYPE):
    """
    One message per slot. If queue empty — do nothing.
    On error: return item back to queue (tail) and backoff.
    """
    item = await dequeue()
    if not item:
        return

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
    except RetryAfter as e:
        # Telegram просит подождать e.retry_after секунд (Flood control)
        delay = int(getattr(e, "retry_after", 5)) + 1
        await enqueue(item.kind, item.payload, item.caption)  # вернуть назад
        await asyncio.sleep(delay)
    except (TimedOut, NetworkError):
        # временный сбой сети: вернуть назад и позже повторить
        await enqueue(item.kind, item.payload, item.caption)
        await asyncio.sleep(5)
    except Exception as e:
        # непредвиденное: не теряем пост, возвращаем в хвост
        await enqueue(item.kind, item.payload, item.caption)
        print("Publish error:", repr(e))

# ---------------------- Application / Polling ----------------------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(2).build()
    app.job_queue.scheduler.configure(timezone=TZ)

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("purge", cmd_purge))

    # контент
    app.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), h_photo))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), h_text))

    # ежедневные слоты
    for t in DAILY_SLOTS:
        app.job_queue.run_daily(
            publish_next,
            time=t,  # tz-aware: respect TZ
            days=(0, 1, 2, 3, 4, 5, 6),     # каждый день
            name=f"slot_{t.strftime('%H%M')}"
        )
    return app

def main():
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
    app.run_polling(
        poll_interval=0.0,
        timeout=25,
        read_timeout=35,
        allowed_updates=["message"],
        drop_pending_updates=True,
        stop_signals=None,   # корректно завершится по Ctrl+C/kill
    )

    asyncio.set_event_loop(None)

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

if __name__ == "__main__":
    # Универсально: работает корректно в консольном режиме
    try:
        main()
    except KeyboardInterrupt:
        pass
