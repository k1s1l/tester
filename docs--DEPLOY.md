# Публикация: GitHub + Railway

## Выбор хостинга

Боту нужны: процесс, работающий 24/7, PostgreSQL, переменные окружения, автодеплой из GitHub.

| Вариант | Плюсы | Минусы |
|---|---|---|
| **Railway** (рекомендуется новичку) | деплой из GitHub в пару кликов, PostgreSQL в том же проекте, переменные-ссылки, логи в браузере, автодеплой при push | платный по потреблению; бэкапы томов — на тарифе Pro |
| VPS (Hetzner, DigitalOcean и т.п.) + `docker compose` | обычно дешевле всего, полный контроль, `docker-compose.yml` уже готов | администрирование Linux, обновления, бэкапы, мониторинг — на вас |
| Render / Fly.io | похожий PaaS-опыт | условия бесплатных/дешёвых тарифов для фоновых процессов и БД часто меняются — проверяйте актуальные |

**Рекомендация: Railway** — минимум ручной работы, и проект уже настроен под него (`railway.json`, `Dockerfile`,
`scripts/start.sh`). Если позже захотите сэкономить — тот же код запускается на любом VPS через `docker compose up -d`.

### Стоимость Railway (проверьте перед оплатой!)

На момент подготовки (октябрь 2026) по данным страницы цен Railway и сторонних обзоров:
**Hobby — $5/мес, включает $5 использования ресурсов**, сверх этого — оплата по факту потребления (CPU/RAM/диск/трафик);
**Pro — $20/мес**; для новых аккаунтов есть **пробный период с разовым кредитом $5 на 30 дней**. Бесплатного
постоянного режима 24/7 для такого проекта ожидать не стоит. Мне не удалось открыть официальные страницы
напрямую из рабочей среды, поэтому **сверьте цифры на https://railway.com/pricing и https://docs.railway.com/reference/pricing**.
Бот в простое потребляет мало (≈100–200 МБ RAM), небольшая PostgreSQL — тоже, поэтому для старта обычно хватает
Hobby, но точная сумма зависит от нагрузки. Расходы на Claude API оплачиваются отдельно в Anthropic Console.

### Polling или webhook

- **Polling (по умолчанию)** — бот сам опрашивает Telegram. Не нужен публичный домен и HTTPS, проще настроить.
  Во время деплоя старый и новый контейнер могут несколько секунд работать одновременно — в логах возможна
  ошибка `Conflict: terminated by other getUpdates request`, aiogram сам повторит запрос, это безопасно.
- **Webhook** — Telegram присылает апдейты на ваш URL. Нужны публичный домен (Railway → Settings → Networking →
  Generate Domain), `BOT_MODE=webhook`, `WEBHOOK_BASE_URL=https://<домен>`, `WEBHOOK_SECRET=<длинная случайная строка>`.
  Railway сам задаёт `PORT`. Есть `/health` для проверки.

Для одного магазина polling полностью достаточен.

---

## Шаг 0. Подготовка (один раз)

1. **Бот**: в Telegram откройте @BotFather → `/newbot` → получите токен вида `123456789:AA…`. Никому его не показывайте.
2. **Ваш Telegram ID**: напишите @userinfobot → число `Id` — это значение для `ADMIN_IDS`.
   Перед запуском напишите своему боту `/start`, иначе он не сможет присылать вам заказы.
3. **Чат для заказов (необязательно)**: создайте группу, добавьте туда бота, узнайте ID группы (например, через
   @getidsbot; ID группы начинается с `-100…`) → `ADMIN_CHAT_ID`. Без этого заказы придут вам в личку.
   Кнопки в группе работают только для пользователей из `ADMIN_IDS`.
4. **Claude API**: https://console.anthropic.com/ → Billing (пополните баланс) → API Keys → Create Key.
   Задайте лимит расходов в Console (Limits), чтобы бюджет был под контролем.

## Шаг 1. Репозиторий GitHub

1. https://github.com/new → имя `sneaker-bot` → **Private** (рекомендуется) → без README → Create repository.
2. В папке проекта:

```bash
cd sneaker_bot
git init -b main            # если репозиторий ещё не инициализирован
git add .
git status                  # убедитесь, что .env НЕТ в списке!
git commit -m "Sneaker shop bot"
git remote add origin https://github.com/<ваш-логин>/sneaker-bot.git
git push -u origin main
```

GitHub попросит авторизацию: используйте GitHub Desktop, `gh auth login` или Personal Access Token вместо пароля.

> Секреты в GitHub не попадают: `.env` исключён в `.gitignore`, в репозитории только `.env.example` без ключей.
> Если вы случайно закоммитили токен — немедленно перевыпустите его (@BotFather → `/revoke`, Anthropic Console → удалить ключ).

## Шаг 2. Проект на Railway

1. https://railway.com → Login with GitHub. Разрешите доступ Railway к репозиторию `sneaker-bot`.
2. **New Project → Deploy from GitHub repo →** выберите `sneaker-bot`. Railway найдёт `railway.json` и `Dockerfile`.
   Первый деплой упадёт без переменных — это нормально.
3. В том же проекте: **+ Create (New) → Database → Add PostgreSQL**. Появится сервис `Postgres`.

## Шаг 3. Переменные окружения

Откройте сервис бота → **Variables → Raw Editor** и вставьте (подставьте свои значения):

```
BOT_TOKEN=123456789:AA...
ADMIN_IDS=123456789
ADMIN_CHAT_ID=
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=${{Postgres.DATABASE_URL}}
CLAUDE_MODEL=claude-opus-5-5
CLAUDE_EFFORT=low
BOT_MODE=polling
SEED_DEMO_DATA=true
SHOP_NAME=Sneaker Shop
```

`${{Postgres.DATABASE_URL}}` — переменная-ссылка Railway: подставляет адрес БД по внутренней сети
(имя `Postgres` должно совпадать с названием сервиса базы). Нажмите **Deploy / Apply changes**.

## Шаг 4. Миграции и запуск

Ничего вручную делать не нужно: при каждом старте контейнер выполняет `scripts/start.sh`:

1. `alembic upgrade head` — применяет миграции (защищено advisory lock);
2. при `SEED_DEMO_DATA=true` — загружает демо-каталог;
3. запускает бота (`exec python -m app.main`, корректно получает SIGTERM).

Откройте **Deployments → View logs**. Успешный запуск выглядит так:

```
Applying database migrations...
Seeding DEMO catalog if the catalog is empty (SEED_DEMO_DATA=true)...
Created: 5, updated: 0, deactivated: 0
INFO app: Bot @your_bot started in polling mode (model claude-opus-5-5)
```

Демо-каталог загружается только в пустую базу, поэтому перезапуски не трогают остатки. Когда перейдёте на
реальный каталог, удалите `SEED_DEMO_DATA` (или поставьте `false`).

## Шаг 5. Проверка (критерий готовности)

В Telegram напишите боту:

1. `/start` → приветствие на украинском;
2. «Хочу кросівки Nike» → список моделей Nike из каталога с ценами и размерами;
3. «Є Nike Vomero 5 у 44 розмірі?» → «На жаль… немає» + другие размеры + до 3 альтернатив в наличии;
4. «а 47?» → «є в наявності», кнопка «Оформити замовлення»;
5. отправьте фото кроссовок → распознавание + проверка по каталогу;
6. оформите заказ (имя → телефон → «Підтвердити») → номер `#1001`;
7. администратор получает «НОВЕ ЗАМОВЛЕННЯ #1001» с кнопками; `/stock NV5` показывает резерв.

## Шаг 6. Автоматический деплой обновлений

Railway пересобирает и перезапускает бота при каждом `git push` в подключённую ветку (`main`):

```bash
git add . && git commit -m "Описание изменений" && git push
```

Ветку можно поменять в сервисе бота → Settings → Source. Новые миграции применятся автоматически при старте.
Политика перезапуска `ON_FAILURE` (до 10 попыток) задана в `railway.json`.

## Импорт реального каталога

С вашего компьютера (нужен Python и `pip install -r requirements.txt`):

1. Railway → сервис `Postgres` → Variables → скопируйте **`DATABASE_PUBLIC_URL`** (внутренний адрес снаружи недоступен).
2. Выполните:

```bash
DATABASE_URL="postgresql://...публичный адрес..." python -m scripts.import_catalog my_catalog.json --deactivate-missing
```

Windows PowerShell: `$env:DATABASE_URL="..."; python -m scripts.import_catalog my_catalog.json --deactivate-missing`

Или добавляйте товары через `/add_product` в Telegram.

## Резервное копирование PostgreSQL

- **Railway**: у сервиса Postgres есть вкладка **Backups** (ручные и по расписанию). По ответу сотрудника Railway
  на форуме, бэкапы томов доступны на тарифе **Pro**; проверьте актуальные условия в документации
  (https://docs.railway.com/guides/postgres-backups-restores — там же описан point-in-time recovery).
- **Самостоятельно (любой тариф)**: с компьютера или сервера, где установлен `pg_dump` (PostgreSQL client 16+):

```bash
DATABASE_URL="<DATABASE_PUBLIC_URL>" sh scripts/backup_db.sh ./backups 14     # хранить 14 дней
# восстановление в пустую БД:
gunzip -c backups/sneaker_bot_YYYYmmdd_HHMMSS.sql.gz | psql "<URL новой БД>"
```

  Запускайте по расписанию (cron / Планировщик заданий Windows). Бэкапы содержат персональные данные —
  храните их приватно и не загружайте в GitHub (`backups/` уже в `.gitignore`).
- **docker-compose (VPS)**: `docker compose --profile backup up -d` — ежедневный дамп в `./backups`.

## Логи и сбои

- Логи: Railway → сервис бота → Deployments → View logs (или `railway logs` в CLI). Токены, ключи, пароли и
  телефоны в логах маскируются.
- Если бот упал — Railway перезапустит его. Заказы и уведомления хранятся в БД: недоставленные уведомления будут
  отправлены после восстановления. Статус доставки: таблица `notifications` (`status`, `last_error`).
- Claude недоступен → бот продолжает работать в упрощённом режиме (без анализа фото), в логах `Claude unavailable`.

## Альтернатива: VPS с Docker

```bash
git clone https://github.com/<логин>/sneaker-bot.git && cd sneaker-bot
cp .env.example .env && nano .env        # заполните ключи
docker compose up -d --build
docker compose exec bot python -m scripts.seed_demo
docker compose --profile backup up -d    # ежедневные бэкапы
```

`restart: unless-stopped` поднимет бота после перезагрузки сервера. Для обновления: `git pull && docker compose up -d --build`.
В `docker-compose.yml` образ собирается с dev-зависимостями (для тестов); для production можно убрать `INSTALL_DEV`.

## Будущие функции (не реализованы)

- **Онлайн-оплата** (например, LiqPay, WayForPay, Monobank acquiring): потребуется договор с платёжным
  провайдером, ключи и обработчик callback'ов. До этого бот никогда не сообщает клиенту об оплате —
  статус «Оплачено» ставит администратор вручную после проверки.
- Интеграция с Новою Поштою (ТТН), корзина из нескольких товаров, Redis для нескольких реплик.
