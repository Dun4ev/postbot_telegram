#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Importer for ChatExport data into Post Queue Bot storage and assets table.
"""

import os
import json
import sqlite3
import shutil
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("importer")

# Config (можно вынести в .env или аргументы)
EXPORT_DIR = "ChatExport_2026-01-29"
STORAGE_DIR = "storage"
DB_PATH = "queue.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Убедимся, что таблица assets существует
    conn.execute("""
    CREATE TABLE IF NOT EXISTS assets (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      path         TEXT    NOT NULL,
      kind         TEXT    NOT NULL,
      caption      TEXT    NOT NULL DEFAULT '',
      source       TEXT    NOT NULL DEFAULT 'direct',
      created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
    );
    """)
    return conn

def import_data():
    if not os.path.exists(EXPORT_DIR):
        logger.error(f"Папка экспорта {EXPORT_DIR} не найдена!")
        return

    result_json = os.path.join(EXPORT_DIR, "result.json")
    if not os.path.exists(result_json):
        logger.error(f"Файл {result_json} не найден!")
        return

    with open(result_json, 'r', encoding='utf-8') as f:
        data = json.load(f)

    conn = init_db()
    cursor = conn.cursor()
    
    messages = data.get("messages", [])
    count = 0
    
    for msg in messages:
        kind = None
        payload = None
        caption = ""
        
        # Определяем тип сообщения
        if "photo" in msg:
            kind = "photo"
            payload = msg["photo"]
        elif "file" in msg and msg.get("media_type") == "video_file":
            kind = "video"
            payload = msg["file"]
        
        if not kind or not payload:
            continue
            
        # Текст (подпись)
        text_data = msg.get("text", "")
        if isinstance(text_data, list):
            caption = "".join([t["text"] if isinstance(t, dict) else t for t in text_data])
        else:
            caption = str(text_data)

        # Дата для структуры папок
        dt = datetime.fromisoformat(msg["date"])
        rel_dir = os.path.join(STORAGE_DIR, str(dt.year), f"{dt.month:02d}")
        abs_dir = os.path.join(os.getcwd(), rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        
        # Копирование файла
        src_path = os.path.join(EXPORT_DIR, payload)
        if not os.path.exists(src_path):
            logger.warning(f"Файл не найден в экспорте: {src_path}")
            continue
            
        filename = f"import_{msg['id']}_{os.path.basename(payload)}"
        dest_rel_path = os.path.join(rel_dir, filename)
        dest_abs_path = os.path.join(abs_dir, filename)
        
        shutil.copy2(src_path, dest_abs_path)
        
        # Запись в БД (в таблицу assets для "Золотого фонда")
        cursor.execute(
            "INSERT INTO assets (path, kind, caption, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (dest_rel_path, kind, caption, "export", int(dt.timestamp()))
        )
        count += 1
        if count % 50 == 0:
            logger.info(f"Импортировано {count} элементов...")

    conn.commit()
    conn.close()
    logger.info(f"✅ Успешно импортировано {count} элементов в библиотеку assets!")

if __name__ == "__main__":
    import_data()
