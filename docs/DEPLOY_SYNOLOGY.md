# Развёртывание Postbot на Synology DSM

Инструкция описывает подготовку окружения и запуск очереди постинга `postbot` на NAS Synology с DSM 7.x. Репозиторий предполагается размещать в каталоге `/volume1/docker/postbot`. При необходимости замените путь во всех командах на свой.

## 1. Предварительные требования

- Учётная запись с правами администратора на DSM.
- Включённый SSH-доступ (DSM → «Панель управления» → «Терминал и SNMP» → «Включить службу SSH»).
- Установленный **Container Manager** (ранее Docker) либо Python 3.11 с модулем `venv` — в зависимости от выбранного способа запуска.
- Свежий токен Telegram-бота (`TG_BOT_TOKEN`), полученный через @BotFather.
- Идентификатор канала (`TG_CHANNEL` вида `@your_channel` или `TG_CHANNEL_ID` вида `-100…`).
- Свободное место на томе для файла очереди `queue.db` и логов.

## 2. Подготовка каталога

1. Подключитесь по SSH:
   ```bash
   ssh admin@<synology-ip>
   ```
2. Создайте рабочую папку (если ещё не существует):
   ```bash
   mkdir -p /volume1/docker/postbot
   ```
3. Клонируйте проект в каталог:
   ```bash
   cd /volume1/docker/postbot
   git clone https://github.com/Dun4ev/postbot_telegram.git .
   ```

   Допускается синхронизация через Synology Drive/SFTP. Главное — чтобы структура файлов совпадала с репозиторием.

## 3. Переменные окружения

Создайте `.env` рядом с `docker-compose.yml`. Файл используется и Docker Compose, и Python-скриптом (через `python-dotenv`).

```bash
cat > .env <<'EOF'
TG_BOT_TOKEN=your-telegram-token
# укажите одно из значений ниже
TG_CHANNEL=@your_channel
# TG_CHANNEL_ID=-1001234567890
TZ=Europe/Belgrade
# доп. параметры при необходимости
# POST_SLOTS=10:00,13:00,16:00,19:00,22:00
EOF
```

> `TG_CHANNEL` и `TG_CHANNEL_ID` взаимоисключающие: используйте только одно.

### Изменение настроек времени

Чтобы изменить часовой пояс или расписание постинга:
1. Отредактируйте `.env` (например, `TZ=Europe/Moscow` или `POST_SLOTS=10:00,14:00,18:00,22:00`)
2. Перезапустите контейнер: `docker compose restart` или через GUI Container Manager → **Действие** → **Перезапустить**

Пересобирать контейнер не нужно — код монтируется как volume.

## 4. Проверка `docker-compose.yml`

- Файл поставляется в репозитории и ожидает монтирование каталога `/volume1/docker/postbot` в контейнер `/app`.  
- Команда запуска внутри контейнера устанавливает зависимости из `requirements.txt`, что гарантирует наличие `python-dotenv`, `aiosqlite`, `pytz` и `python-telegram-bot`.
- Для валидации конфигурации выполните:
  ```bash
  docker compose config
  ```
  Команда должна завершиться без ошибок.

## 5. Запуск через Container Manager

### Вариант A: через GUI (Container Manager)

1. Откройте DSM → **Container Manager** (или **Docker** в старых версиях).
2. Выберите **Container** → **Создать** → **Создать из Compose файла**.
3. В поле "Путь к project" укажите `/volume1/docker/postbot`.
4. Нажмите **Далее**. Проверьте конфигурацию и нажмите **Готово**.
5. Дождитесь сборки образа и запуска контейнера.
6. Откройте контейнер `postbot` → **Подробности** → **Лог**.
   - Ищите строки `Запуск цикла приложения`, `Планировщик инициализирован`, `Старт long-polling`.
   - Ошибки авторизации будут помечены как `CRITICAL`.
7. Для автозапуска: **Подробности** → **Действие** → **Редактировать** → включите **Автоматически перезапускать контейнер**.

### Вариант B: через SSH (командная строка)

1. Убедитесь, что Container Manager активен:
   ```bash
   synoservice --status pkgctl-ContainerManager
   ```
2. Соберите и запустите сервис:
   ```bash
   cd /volume1/docker/postbot
   docker compose up -d --build
   ```
3. Контроль логов:
   ```bash
   docker compose logs -f
   ```
   Ищите строки `Запуск цикла приложения`, `Планировщик инициализирован`, `Старт long-polling`. Ошибки авторизации будут помечены как `CRITICAL`.
4. Настройте автозапуск: Container Manager → **Контейнер** → выберите `postbot` → **Действия** → **Включить автозапуск**.

## 6. Альтернативный запуск в виртуальном окружении Python

1. Установите Python 3.11 и модуль `venv` (через `synopkg install` или `opkg`).
2. Настройте окружение:
   ```bash
   cd /volume1/docker/postbot
   python3.11 -m venv venv
   source venv/bin/activate
   pip install --no-cache-dir -r requirements.txt
   ```
3. Запустите бота:
   ```bash
   TG_BOT_TOKEN=your-telegram-token \
   TG_CHANNEL=@your_channel \
   TZ=Europe/Belgrade \
   python bot.py
   ```
   SQLite-файл `queue.db` создаётся автоматически в текущей директории. Поменять путь можно, запуская скрипт из нужной папки (или наложив `ln -s`).
4. Для автозапуска создайте задачу в Планировщике DSM с вызовом скрипта внутри `venv`, либо используйте `systemd`/`supervisord` в chroot.

## 7. Проверка работоспособности

1. Контейнер: `docker compose ps` — статус `Up`.  
   Вариант venv: `ps -ef | grep bot.py`.
2. Отправьте тестовое сообщение боту. В логе должна появиться запись `Получено новое сообщение от`.
3. При необходимости включите debug: временно добавьте `POSTBOT_LOG_LEVEL=DEBUG` в `.env`, перезапустите контейнер и снова проверьте `logs -f`.

## 8. Резервное копирование

- Добавьте в Hyper Backup файлы `/volume1/docker/postbot/.env`, `/volume1/docker/postbot/queue.db`, а также логи (по умолчанию `postbot.log`).
- Храните токены в менеджере секретов (Synology Password Manager, Bitwarden, Vault и т.п.), а в `.env` используйте копию из хранилища.

## 9. Обновление и откат

### Обновление

```bash
cd /volume1/docker/postbot
git pull
docker compose build --pull
docker compose up -d
```

Запуск в `venv`: после `git pull` выполните `pip install -r requirements.txt` и перезапустите `bot.py`.

### Откат

```bash
cd /volume1/docker/postbot
git reset --hard <previous_commit>
docker compose up -d --build
```

Для `venv` повторно установите зависимости и перезапустите процесс.

## 10. Диагностика

- `401/403` в логах — некорректный `TG_BOT_TOKEN` или недостаточные права бота в канале.
- `429` — превышение лимитов Telegram. Уменьшите частоту постинга или расширьте слоты (`POST_SLOTS`).
- `permission denied` — проверьте владельца каталога `/volume1/docker/postbot` и права пользователя, под которым работает Docker.
- Лог запуска не появляется — убедитесь, что `.env` лежит рядом с `docker-compose.yml` и контейнеру доступна папка `/volume1/docker/postbot`.
