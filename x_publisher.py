#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X (Twitter) publisher module using Tweepy (API v2 for tweets, v1.1 for media).
"""

import os
import logging
import json
import tweepy
from typing import Optional, List

logger = logging.getLogger("postbot.x_publisher")

# Настройки из .env через os.getenv (подгружаются в bot.py)
X_ENABLED = os.getenv("X_ENABLED", "0") == "1"
X_API_KEY = os.getenv("X_API_KEY")
X_API_SECRET = os.getenv("X_API_SECRET")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

def get_x_clients():
    """
    Инициализирует клиенты Tweepy для API v1.1 (медиа) и API v2 (твиты).
    """
    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        logger.error("Ключи X API не настроены полностью.")
        return None, None

    try:
        # API v1.1 для загрузки медиа
        auth = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth)

        # API v2 для публикации твитов
        client_v2 = tweepy.Client(
            bearer_token=X_BEARER_TOKEN,
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
        return api_v1, client_v2
    except Exception as e:
        logger.exception("Ошибка инициализации клиентов X API: %s", e)
        return None, None

async def publish_to_x(bot, kind: str, payload: str, caption: str = "") -> bool:
    """
    Асинхронная обертка для публикации в X.
    Поскольку tweepy блокирующий, в идеале использовать run_in_executor, 
    но для 5-6 постов в день достаточно обычного вызова.
    
    :param bot: Объект бота Telegram (для скачивания медиа, если нужно, или логов)
    :param kind: 'text', 'photo', 'video', 'album'
    :param payload: Текст или file_id (или JSON для альбома)
    :param caption: Подпись для медиа
    """
    if not X_ENABLED:
        return False

    api_v1, client_v2 = get_x_clients()
    if not client_v2:
        return False

    try:
        text_to_post = ""
        media_ids = []

        if kind == "text":
            text_to_post = payload
        elif kind in ("photo", "video"):
            text_to_post = caption
            # Для скачивания из Telegram нужен доступ к bot
            media_id = await _upload_media_by_file_id(bot, api_v1, payload, kind)
            if media_id:
                media_ids.append(media_id)
        elif kind == "album":
            text_to_post = caption
            try:
                items = json.loads(payload)
                for item in items[:4]:  # X разрешает до 4 фото
                    m_id = await _upload_media_by_file_id(bot, api_v1, item['file_id'], item['type'])
                    if m_id:
                        media_ids.append(m_id)
            except Exception:
                logger.error("Ошибка парсинга альбома для X")

        # Ограничение 280 символов
        if len(text_to_post) > 280:
            text_to_post = text_to_post[:277] + "..."

        # Публикация
        response = client_v2.create_tweet(
            text=text_to_post or None,
            media_ids=media_ids or None
        )
        logger.info("Пост опубликован в X: id=%s", response.data['id'])
        return True

    except Exception as e:
        logger.exception("Ошибка при публикации в X: %s", e)
        return False

async def _upload_media_by_file_id(bot, api_v1, file_id: str, kind: str) -> Optional[str]:
    """
    Скачивает файл из Telegram и загружает в X.
    """
    tmp_path = f"/tmp/x_upload_{file_id}"
    try:
        # Получаем файл из TG
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)

        # Загружаем в X (API v1.1)
        # Для видео нужен chunked=True
        is_video = kind == "video"
        media = api_v1.media_upload(filename=tmp_path, media_category='tweet_video' if is_video else 'tweet_image')
        
        return media.media_id_string
    except Exception as e:
        logger.error("Ошибка загрузки медиа в X (%s): %s", file_id, e)
        return None
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
