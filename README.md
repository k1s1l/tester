# Sneaker Bot — ИИ-продавец кроссовок для Telegram

Telegram-бот интернет-магазина кроссовок: консультирует покупателей на украинском языке, понимает свободный
текст и фотографии (Claude API), проверяет реальные остатки в PostgreSQL, предлагает альтернативы,
резервирует товар и оформляет заказы, уведомляет администратора и даёт ему закрытую админ-панель.

> ⚠️ Товары из `data/demo_catalog.json` — **демонстрационные** (`is_demo=true`, бот помечает их
> «демонстраційний товар»). Это не реальный ассортимент магазина. Как заменить их — см. «Каталог».

Подробности: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Развёртывание: [docs/DEPLOY.md](docs/DEPLOY.md)

## Возможности

- Естественный диалог без команд: бренд, модель, размер, цвет, бюджет, назначение, «а 43 є?», «давайте другу».
- Поиск по фото: Claude определяет бренд/модель/цвет, Python сопоставляет с каталогом и проверяет остатки.
- Альтернативы (до 3) — только реально имеющиеся товары в нужном размере; если нет — подписка на поступление.
- Оформление: модель → размер → повторная проверка → имя → контакт → итог → подтверждение → заказ `#1001`.
- Склад: резерв при создании заказа, списание при оплате/отправке, автоосвобождение просроченных резервов,
  журнал движений, защита от продажи последней пары двум покупателям, запрет отрицательных остатков на уровне БД.
- Уведомления администратору с кнопками (подтвердить / отклонить / оплачено / отправлено / завершить / отменить),
  outbox с повторными попытками, если Telegram недоступен.
- Админ-команды: `/admin /add_product /edit_product /products /stock /set_stock /orders /order /stats /export_orders /help`.
- Работает и без Claude (нет ключа / сбой API): правило-ориентированный парсер, наличие и заказы не страдают.
- Онлайн-оплата **не реализована** (это отдельная будущая функция): статус «Оплачено» ставит администратор вручную.

## Технологии

Python 3.12+ (образ — 3.13) · aiogram 3.31 · Anthropic SDK 2.3 (`claude-opus-5-5`, structured outputs) ·
PostgreSQL 16 · SQLAlchemy 2.1 (async) + asyncpg · Alembic 1.20 · Pydantic 2 · RapidFuzz · Docker.

## Структура

```
sneaker_bot/
├── app/
│   ├── main.py                 # запуск: polling/webhook, DI, фоновые задачи, graceful shutdown
│   ├── config.py               # настройки из переменных окружения (pydantic-settings)
│   ├── logging_setup.py        # логирование с маскировкой токенов, ключей и телефонов
│   ├── texts.py                # все тексты на украинском + форматирование фактов из БД
│   ├── database/               # модели, сессии, FSM-хранилище в PostgreSQL
│   ├── schemas/                # Pydantic: ответы Claude, каталог, контакты
│   ├── services/               # бизнес-логика (без зависимостей от Telegram-хендлеров)
│   │   ├── ai.py               # Claude: анализ текста и фото, обработка ошибок API
│   │   ├── assistant.py        # оркестрация диалога продавца
│   │   ├── catalog.py          # поиск, нечеткое сопоставление, альтернативы
│   │   ├── inventory.py        # атомарные операции склада
│   │   ├── orders.py           # заказы, статусы, истечение резервов
│   │   ├── notifications.py    # outbox уведомлений с ретраями
│   │   ├── fallback_parser.py  # разбор сообщений без ИИ
│   │   └── ...                 # отчёты, импорт каталога, лист ожидания, обслуживание
│   ├── handlers/               # aiogram-роутеры: клиент, оформление, админка, общие
│   ├── keyboards/              # inline/reply клавиатуры и callback data
│   ├── middlewares/            # анти-флуд
│   └── filters/                # проверка ADMIN_IDS
├── migrations/                 # Alembic
├── scripts/                    # start.sh, seed_demo, import_catalog, backup_db.sh
├── data/demo_catalog.json      # ДЕМО-каталог
├── tests/                      # 63 автотеста (PostgreSQL + фейки Telegram/Claude)
├── Dockerfile, docker-compose.yml, railway.json
├── requirements.txt, requirements-dev.txt, pyproject.toml
└── .env.example, .gitignore
```

## Быстрый старт локально (Docker)

Нужны: Docker Desktop, токен бота от [@BotFather](https://t.me/BotFather), ваш Telegram ID
(например, от [@userinfobot](https://t.me/userinfobot)), ключ Anthropic API (https://console.anthropic.com/ → API Keys;
API оплачивается отдельно от подписки Claude.ai).

```bash
cp .env.example .env            # заполните BOT_TOKEN, ADMIN_IDS, ANTHROPIC_API_KEY
docker compose up -d --build    # PostgreSQL + бот (миграции применяются автоматически)
docker compose exec bot python -m scripts.seed_demo   # загрузить демо-каталог
docker compose logs -f bot      # логи
```

Напишите боту в Telegram: «Хочу кросівки Nike», «Є Vomero 5 у 44?», «а 47?», отправьте фото.

Тесты в Docker:

```bash
docker compose run --rm bot python -m pytest -q
```

## Запуск без Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# PostgreSQL 14+ должен быть запущен; создайте БД sneaker_bot (и sneaker_bot_test для тестов)
cp .env.example .env            # DATABASE_URL=postgresql://user:pass@localhost:5432/sneaker_bot
alembic upgrade head
python -m scripts.seed_demo
python -m app.main
```

Тесты: `TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sneaker_bot_test python -m pytest -q`
(тестовая БД очищается при каждом тесте — не указывайте рабочую!). Линтер: `ruff check .`

## Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | да | токен от @BotFather |
| `DATABASE_URL` | да | `postgresql://…` (на Railway — `${{Postgres.DATABASE_URL}}`) |
| `ADMIN_IDS` | да | Telegram ID администраторов через запятую |
| `ADMIN_CHAT_ID` | нет | чат/канал для заказов; пусто — личные сообщения каждому админу |
| `ANTHROPIC_API_KEY` | желательно | без ключа нет анализа фото, текст разбирается правилами |
| `CLAUDE_MODEL` | нет | по умолчанию `claude-opus-5-5` |
| `CLAUDE_EFFORT` | нет | `low` (по умолчанию) / `medium` / `high` |
| `CLAUDE_FALLBACKS` | нет | серверный fallback при отказе модели (beta), `true` по умолчанию |
| `BOT_MODE` | нет | `polling` (по умолчанию) или `webhook` (+ `WEBHOOK_BASE_URL`, `WEBHOOK_SECRET`, `PORT`) |
| `SEED_DEMO_DATA` | нет | `true` — загрузить демо-каталог при старте контейнера |
| `RESERVATION_TTL_MINUTES` | нет | резерв нового заказа, 1440 (24 ч) |
| `CONFIRMED_RESERVATION_TTL_HOURS` | нет | резерв после подтверждения админом, 72 |
| `DATA_RETENTION_DAYS` | нет | через сколько дней обезличивать закрытые заказы, 365 |
| `RATE_LIMIT_PER_MINUTE`, `AI_REQUESTS_PER_USER_PER_HOUR`, `MAX_PHOTO_MB` | нет | лимиты |

Полный список — `.env.example` и `app/config.py`.

## Каталог: замена демо-данных реальными

Код бота менять не нужно. Подготовьте JSON (формат как `data/demo_catalog.json`) или CSV
(одна строка на размер: `brand,model,sku,color,category,tags,price,description,is_active,is_demo,photo_urls,size,quantity`)
и выполните:

```bash
python -m scripts.import_catalog my_catalog.json --deactivate-missing   # скроет демо-товары
# Railway: запустите локально, указав публичный URL базы (см. docs/DEPLOY.md, «Импорт каталога»)
```

Либо добавляйте товары через `/add_product` прямо в Telegram (фото загружаются туда же).

## Команды

Клиент: свободный текст и фото, `/start`, `/help`, `/my_orders`, `/forget`.
Администратор (только ID из `ADMIN_IDS`):

| Команда | Действие |
|---|---|
| `/admin` | панель с кнопками |
| `/add_product` | мастер добавления товара (9 шагов, фото, размеры) |
| `/edit_product NV5` | цена, название, цвет, описание, размеры/остатки, фото, скрыть/показать |
| `/products [поиск]` | каталог с пагинацией |
| `/stock [NV5]` | остатки: доступно / на складе / в резерве |
| `/set_stock NV5 44 2` | быстро задать остаток размера (уведомит лист ожидания) |
| `/orders [pending]` | последние заказы, фильтр по статусу |
| `/order 1001` | детали и история статусов, кнопки действий |
| `/stats` | статистика продаж, топ моделей, заканчивающиеся размеры |
| `/export_orders [дней]` | CSV для Excel |

## Стоимость Claude (ориентир)

Цены `claude-opus-5-5`: $4 / 1M входных и $20 / 1M выходных токенов. Одно сообщение покупателя — примерно
2–3 тыс. входных и до 0,5–1 тыс. выходных токенов (с учётом размышлений при `effort=low`), т.е. порядка
$0.02–0.03; фото — немного дороже. Это оценка, а не гарантия — смотрите фактический расход в Console.
Дешевле: `CLAUDE_MODEL=claude-sonnet-5-5` ($2/$10) или `CLAUDE_MODEL=claude-haiku-5-5` ($0.10/$0.50,
для Haiku установите `CLAUDE_FALLBACKS=false`). Качество распознавания фото у старших моделей выше.
Лимит `AI_REQUESTS_PER_USER_PER_HOUR` защищает бюджет от злоупотреблений.
