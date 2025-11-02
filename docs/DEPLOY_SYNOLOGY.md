# Развёртывание Postbot на Synology DSM

Инструкция описывает подготовку окружения и запуск бота `postbot` на NAS Synology с DSM 7.x. Предполагается, что проект уже склонирован или синхронизирован в директорию `/volume1/docker/postbot` (скорректируйте путь под свою конфигурацию).

## 1. Предварительные требования

- Учётная запись с правами администратора на DSM.
- Включённый доступ по SSH (DSM → «Панель управления» → «Терминал и SNMP» → «Включить службу SSH»).
- Установленный пакет **Container Manager** (ранее Docker) для контейнерного способа, либо Python 3.11 с модулем `venv` для виртуального окружения.
- Свежий токен Telegram-бота (`POSTBOT_TELEGRAM_TOKEN`), полученный через @BotFather.
- Свободное место на томе, где будет храниться база `queue.db`.

## 2. Подготовка окружения

1. Подключитесь по SSH:
   ```bash
   ssh admin@<synology-ip>
   ```
2. Создайте рабочую директорию (если ещё не создана):
   ```bash
   mkdir -p /volume1/docker/postbot
   ```
3. Перейдите в каталог и разместите проект:
   ```bash
   cd /volume1/docker/postbot
   git clone https://<your_repo>.git .
   ```
   Допускается синхронизация через Synology Drive или SFTP — убедитесь, что финальная структура совпадает с исходным репозиторием.

## 3. Конфигурация переменных окружения

1. Создайте файл `.env` (не добавляйте в систему контроля версий):
   ```bash
   cat > .env <<'EOF'
   POSTBOT_TELEGRAM_TOKEN=your-telegram-token
   POSTBOT_DB_PATH=/data/queue.db
   EOF
   ```
2. Проверьте файл `docker-compose.yml` (если используете Docker), чтобы путь `/data/queue.db` был смонтирован в контейнер. При запуске в venv укажите абсолютный путь в переменной окружения `POSTBOT_DB_PATH`.

## 4. Запуск через Docker Compose

1. Убедитесь, что сервис Docker активен:
   ```bash
   synoservice --status pkgctl-ContainerManager
   ```
2. Запустите контейнеры:
   ```bash
   docker compose pull
   docker compose up -d --build
   ```
3. Просмотрите логи:
   ```bash
   docker compose logs -f
   ```
   В логах ожидаются строки «Bot started» без ошибок авторизации.
4. Настройте автозапуск: Container Manager → вкладка «Контейнер» → выберите `postbot_app` → «Действия» → «Включить автозапуск».

## 5. Запуск в виртуальном окружении Python

1. Установите Python 3.11 и модуль `venv` (через `synopkg install` или менеджер пакетов opkg, если требуется).
2. Создайте окружение и установите зависимости:
   ```bash
   python3.11 -m venv venv
   source venv/bin/activate
   pip install --no-cache-dir -r requirements.txt
   ```
3. Запустите бота:
   ```bash
   POSTBOT_TELEGRAM_TOKEN=your-telegram-token \
   POSTBOT_DB_PATH=/volume1/docker/postbot/queue.db \
   python bot.py
   ```
4. Для автозапуска создайте задачу в Планировщике DSM: «Созданная задача» → «Запланированная задача (пользовательская)» → команда запуска внутри виртуального окружения. Либо используйте `systemd`/`supervisord` в chroot, если предпочитаете классические службы.

## 6. Проверка работоспособности

1. Убедитесь, что процесс активен:
   - Docker: `docker compose ps`
   - venv: `ps -ef | grep bot.py`
2. Отправьте тестовое сообщение боту в Telegram и убедитесь в ответе.
3. Проверьте логи на наличие ошибок (`docker compose logs -f` или вывод скрипта).

## 7. Резервное копирование

- Добавьте в Hyper Backup каталог `/volume1/docker/postbot/.env` и файлы данных (`queue.db` или директория `/data`) для регулярного резервного копирования.
- Сохраняйте токены в менеджере секретов Synology или стороннем хранилище (Bitwarden, Vault).

## 8. Обновление и откат

### Обновление

```bash
cd /volume1/docker/postbot
git pull
docker compose build --pull
docker compose up -d
```

Для варианта с venv выполните `pip install -r requirements.txt` и перезапустите скрипт.

### Откат

```bash
cd /volume1/docker/postbot
git reset --hard <previous_commit>
docker compose up -d --build
```

При использовании venv после отката повторите установку зависимостей и перезапуск.

## 9. Диагностика

- Ошибка авторизации Telegram (`401`/`403`) — перепроверьте `POSTBOT_TELEGRAM_TOKEN`.
- Flood limit (`429`) — добавьте задержки или уменьшите интенсивность сообщений.
- Проблемы с файловой системой (permission denied) — проверьте права на том `/volume1` и пользователя, под которым работает контейнер/скрипт.

