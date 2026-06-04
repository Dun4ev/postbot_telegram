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
            text_to_post = caption or payload
        elif kind in ("photo", "video"):
            text_to_post = caption
            media_id = _upload_media_by_path(api_v1, payload, kind)
            if media_id:
                media_ids.append(media_id)
            else:
                return False
        elif kind == "album":
            text_to_post = caption
            try:
                items = json.loads(payload)
                for item in items[:4]:  # X разрешает до 4 фото
                    m_id = _upload_media_by_path(api_v1, item['path'], item['type'])
                    if m_id:
                        media_ids.append(m_id)
            except Exception:
                logger.error("Ошибка парсинга альбома для X")
                return False
            if not media_ids:
                logger.error("Нет загруженных медиа для публикации альбома в X")
                return False

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

def _upload_media_by_path(api_v1, file_path: str, kind: str) -> Optional[str]:
    """
    Загружает файл с диска в X.
    """
    if not os.path.exists(file_path):
        logger.error("Файл не найден для загрузки в X: %s", file_path)
        return None
        
    try:
        # Загружаем в X (API v1.1)
        is_video = kind == "video"
        media = api_v1.media_upload(filename=file_path, media_category='tweet_video' if is_video else 'tweet_image')
        return media.media_id_string
    except Exception as e:
        logger.error("Ошибка загрузки медиа в X (%s): %s", file_path, e)
        return None
