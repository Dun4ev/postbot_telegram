# Telegram Post Queue Bot

## Обзор
Этот репозиторий содержит Telegram-бота, который принимает сообщения в личных сообщениях и публикует их в канале по расписанию. Очередь хранится в SQLite, обработка сообщений выполняется через `python-telegram-bot` в режиме long-polling. Проект реализует идею: кидаете текст, фото или видео в ЛС боту → элемент попадает в FIFO-очередь (`queue.db`) → бот публикует пост ровно 5 раз в день по слотам в `Europe/Belgrade`. Цель — минимальный когнитивный шум и развёртывание на macOS, затем на Synology (Docker).

## Требования
- Python 3.11+
- Активированный ботовый токен Telegram
- Доступ к каналу (бот должен быть администратором)
- Docker (опционально) для контейнерного деплоя

## Быстрый старт
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# создайте файл .env и заполните переменные окружения
python bot.py
```

## Конфигурация
Создайте файл `.env` в корне проекта и задайте переменные окружения:
- `TG_BOT_TOKEN` — токен бота (обязательно).
- `TG_CHANNEL` или `TG_CHANNEL_ID` — целевой канал.
- `POSTBOT_STORAGE_DIR` — путь к папке хранения (дефолт: `storage`).
- `POSTBOT_DB_PATH` — путь к SQLite-базе очереди (дефолт: `queue.db`; в Docker используется `/data/queue.db`).
- `POSTBOT_ARCHIVE_ONLY` — режим локального накопителя/хранилища (1 — отключить публикацию и просто сохранять все входящие медиа в `storage/` с AI-подписями в `assets`).
- `POSTBOT_ADMIN_ID` — Telegram ID администратора для получения ежедневных отчетов.
- `POSTBOT_AUTO_SYNC` — включить автоматическое сканирование папки `storage` при старте (1 или 0).
- `POSTBOT_AI_CAPTION` — включить короткую AI-подпись для Telegram и X.com (1 или 0).
- `POSTBOT_AI_ENDPOINT` — OpenAI-compatible endpoint генератора, например официальный Mistral API.
- `POSTBOT_AI_API_KEY`, `POSTBOT_AI_MODEL`, `POSTBOT_AI_LANGUAGE`, `POSTBOT_AI_STYLE`, `POSTBOT_AI_TIMEOUT`, `POSTBOT_AI_MAX_CHARS` — параметры AI-генерации.
- `POSTBOT_FILL_CAPTIONS_LIMIT` — размер порции для ручного заполнения старых подписей командой `/fill_captions` (дефолт: 10).
- `POSTBOT_FILL_CAPTIONS_DELAY` — пауза между AI-запросами при заполнении старой базы, чтобы не упереться в rate limit (дефолт: 60 секунд).
- `POSTBOT_FILL_CAPTIONS_RATE_LIMIT_SLEEP` — пауза фонового заполнения после ответа `429 Too Many Requests` (дефолт: 1800 секунд).
- `TZ` — часовой пояс (по умолчанию `Europe/Belgrade`).
- `POST_SLOTS` — список времён публикации (формат `HH:MM,HH:MM`).
- `X_ENABLED` — включить публикацию в X (1 или 0).
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `X_BEARER_TOKEN` — ключи X API.
- `POSTBOT_SIGNATURE` — глобальная подпись для публикаций в X.com (поддерживает ссылки и хэштеги, добавляется автоматически и защищена от обрезки).

Полный список параметров см. в `.env.example`.
Очередь хранится в `queue.db` в корне проекта.

## Работа очереди
1. Пользователь отправляет текст, фото или видео боту в DM.
2. Бот сохраняет пост и предлагает выбрать платформу через кнопки (**Telegram**, **X.com**, **Везде**). Медиа дополнительно сохраняется локально.
3. После выбора платформы кнопками элемент попадает в **конец** FIFO-очереди.
4. **Автоматизация архива**: При старте бот сканирует папку `storage` и автоматически добавляет в очередь всё, что еще не было опубликовано.
5. **Ежедневные отчеты**: Каждое утро (за 30 минут до первого поста) бот присылает админу статистику очереди.
6. По расписанию слот запускает `publish_next`, соблюдая выбор платформы и помечая пост в архиве как «опубликованный».

Если пост выбран для публикации **Везде**, а Telegram уже опубликован, но X.com вернул ошибку, бот возвращает в очередь только X.com-часть. Это защищает канал Telegram от дублей и не теряет неудачную X-публикацию.

Можно включить короткую AI-подпись для Telegram и X.com:
```env
POSTBOT_AI_CAPTION=1
POSTBOT_AI_ENDPOINT=https://api.mistral.ai/v1/chat/completions
POSTBOT_AI_API_KEY=your-mistral-api-key
POSTBOT_AI_MODEL=magistral-small-2509
POSTBOT_AI_LANGUAGE=English
POSTBOT_AI_STYLE=Short intriguing captions for a private lifestyle channel of a confident beautiful girl. Positive flirtation, light mystery, warmth, and an invitation to chat. Add 1-3 soft emojis like smiles, hearts, sparkles, or peach. No explicit promises, links, or hashtags.
POSTBOT_AI_MAX_CHARS=120
```
Бот отправляет генератору исходную подпись/текст как контекст через Mistral Chat Completions API, просит одну простую строку без ссылок и хэштегов, сохраняет ее в очередь и использует одинаково в Telegram и X.com. Если генератор недоступен или не ответил за `POSTBOT_AI_TIMEOUT`, публикация идет со старой подписью/текстом.

Для старой базы можно дозаполнить подписи вручную: `/fill_captions` обработает небольшую порцию старых записей без `ai_caption`, а `/fill_captions 25` обработает до 25 записей. Команда сначала заполняет текущую очередь, потом оставшийся архив; обновляет `assets.ai_caption`, связанные записи `queue.ai_caption`, а если у медиа вообще не было подписи, кладет тот же текст в `caption` для Telegram. Перед массовым запуском сделайте бэкап `queue.db`. Для бесплатного Mistral начинайте с `POSTBOT_FILL_CAPTIONS_DELAY=60`; если Mistral отвечает `429 Too Many Requests`, команда остановит текущую порцию, а следующий запуск стоит делать позже или с большей паузой.

Состояние очереди можно посмотреть командой `/queue`, публикация следующего поста сейчас — `/now`, пополнение из архива — `/restock`, заполнение старых подписей — `/fill_captions`.
еще добавлено автоматическое меню команд для удобства.

## Запуск через Docker Compose
```bash
docker-compose up --build
```
Файл `docker-compose.yml` устанавливает зависимости, монтирует код проекта в `/app`, а базу и медиаархив держит в Docker volumes. Это важно для локального запуска из облачной папки: SQLite, логи и медиа не должны читаться/писаться через SynologyDrive mount.

## Разработка и проверки
Перед коммитом прогоните:
```bash
ruff check .
black --check .
mypy .
pytest -q
```
Тесты рекомендуется складывать в `tests/` с зеркальной структурой относительно кода. Для интеграционных сценариев используйте заглушки Telegram API.

## Рекомендации по эксплуатации
- **Лимиты Telegram** — бот должен быть администратором канала; при ошибках прав или Flood Control мониторьте вывод `bot.py`, элементы очереди не потеряются, но будут откладываться.
- **Очередь** — периодически проверяйте размер `queue.db` (`sqlite3 queue.db 'select count(*) from queue;'`).
- **Расписание** — задавайте `TZ` и `POST_SLOTS` только валидными значениями (`Europe/Moscow`, `HH:MM`); для быстрой отладки удобно указывать ближайший слот через `export POST_SLOTS="$(date -v+5M +%H:%M)"`.
- **Мониторинг** — включите логирование stdout в файл (например, `python bot.py >> bot.log 2>&1`) и настройте уведомления при повторяющихся `Publish error`.
- **Резервное копирование** — храните бэкап `queue.db` и `.env` (без токенов в открытом виде) если очередь критична.

## Настройка X (Twitter) API

Для автоматической публикации в X (Twitter) вам необходимо получить 5 ключей на [developer.x.com](https://developer.x.com/):

1. **Создайте проект и приложение**:
   - Выберите уровень доступа **"Free"** или **"Basic"**.
   - Тип приложения: **"Web App, Automated App or Bot"**.

2. **Настройте права доступа (User authentication settings)**:
   - **ВАЖНО**: Установите App Permissions в режим **"Read and Write"**.
   - Включите **OAuth 1.0a**.
   - Callback URI / Redirect URL: `https://x.com`
   - Website URL: `https://x.com`

3. **Получите ключи (Keys and Tokens)**:
   - **API Key & Secret**: Это `Consumer Keys`.
   - **Access Token & Secret**: Генерируются в разделе `Authentication Tokens` (только после настройки "Read and Write"!).
   - **Bearer Token**: Из соответствующего раздела.

4. **Заполните .env**: Перенесите полученные значения в файл `.env` и убедитесь, что `X_ENABLED=1`.
