# Архитектура проекта

Документ для разработчика, который видит проект впервые и хочет понять, как всё устроено.

## Что это за проект в одном абзаце

Integration Hub Revshare — это система, в которой партнёры Хаба Интеграций Altegio каждый месяц сдают отчёт о своём обороте по приложениям. Партнёр загружает Excel или CSV, система парсит файл, сверяет с данными Altegio BI, считает revshare и кладёт оборот в итоговую Google-таблицу [Partnerships Altegio Revenue Accrual](https://docs.google.com/spreadsheets/d/1GoEn9TiJHMI_7YQ9LkzBnLAoKRB_PvtnootyjQAta5A). Поддерживает русский, английский, украинский, польский, венгерский и португальский.

## Технологический стек

| Слой | Технология |
|---|---|
| Язык | Python 3.12+ |
| Веб-фреймворк | FastAPI + Uvicorn |
| Шаблоны | Jinja2 |
| Хранилище | SQLite (`data/revshare.db`) |
| Зависимости | `uv` или `pip`, файл `uv.lock` |
| Тесты | pytest |
| CI | GitHub Actions (`.github/workflows/test.yml`) |
| Внешние API | Google Sheets API, Telegram Bot API, SMTP, Altegio BI MCP/MySQL |
| CLI-точка | `python -m revshare` |

Стоимость эксплуатации — `$0` локально, `$0–5/мес` в GCP при малой нагрузке, до `$20/мес` на пике. LLM-вызовы в проекте не используются.

## Два процесса, одна база

Проект делится на два запускаемых процесса, которые работают параллельно:

### 1. `revshare-web` — HTTP-сервер

Файл: `src/revshare/web.py`, точка входа — функция `run()`.
Запуск: `revshare-web` или `python -m revshare.web`.

Что делает:

- Принимает HTTP-запросы от партнёров (загрузка файлов) и админов.
- Рендерит HTML через Jinja2.
- Парсит загруженные файлы (Excel, CSV, PDF).
- Делает сверку с данными Altegio.
- Отдаёт CSV-выгрузки.

В одном экземпляре этого процесса НЕ должны работать планировщик и Telegram polling — иначе при нескольких воркерах будут дубли. Это переключается переменной `REVSHARE_WEB_BACKGROUND_TASKS=0` в проде.

### 2. `revshare-worker` — фоновый планировщик

Файл: `src/revshare/worker.py`, точка входа — `run()`.
Запуск: `revshare-worker` или `python -m revshare.worker`.

Что делает каждый час:

- Обновляет снимки Altegio для активных партнёров (MySQL или MCP).
- Запускает отправку запланированных уведомлений (Telegram + email).
- Обрабатывает пересчёт сверки при обновлении снимков.
- Очищает истёкшие сессии Telegram polling.

Оба процесса используют одну и ту же SQLite-базу. На проде их запускают как отдельные systemd-юниты (см. `DEPLOY.md`).

## Структура модулей

```
src/revshare/
├── web.py            # FastAPI: все ручки, формы, шаблоны
├── store.py          # Слой данных: SQLite, миграции, запросы, сверка
├── extract.py        # Парсинг Excel/CSV/PDF/текста → черновик
├── worker.py         # Фоновый планировщик
├── bot.py            # Telegram polling: /start, привязка чата
├── dispatch.py       # Маршрутизация уведомлений (Telegram + SMTP)
├── mailer.py         # SMTP-отправка писем
├── email_letter.py   # Шаблоны писем (HTML + текст)
├── notifications.py  # Расписание уведомлений и типы
├── telegram.py       # Обёртка над Telegram Bot API
├── altegio_sync.py   # Синхронизация снимков из Altegio BI
├── mcp_client.py     # OAuth-клиент для внутреннего MCP
├── mysql.py          # Подключение к MySQL (snapshots)
├── altegio.py        # Старый код Altegio (deprecating)
├── sheet_sync.py     # Запись в Google Sheets
├── sheets.py         # Низкоуровневый Google Sheets API
├── sheet_partners.py # Карта партнёров ↔ ячейки таблицы
├── cli.py            # CLI: status, ingest, preview, sheet-flush
├── revalidation.py   # Пересчёт сверки при изменении снимка
├── report_template.py# Генерация шаблонов Excel/CSV
├── application_breakdown.py # Разбивка отчёта по приложениям
├── preview.py        # Превью отчёта перед подтверждением
├── registry.py       # Справочник приложений Хаба
├── i18n.py           # Локализация (6 языков)
├── countries.py      # Страны + категории приложений
├── fx.py             # Курсы валют к USD
├── timezone.py       # Таймзоны партнёров
└── secrets.py        # Загрузка переменных из data/secrets.env
```

## Слои приложения

Код разделён на четыре слоя. Зависимости идут сверху вниз.

```
┌────────────────────────────────────────────────────────────┐
│ Слой 1. HTTP / CLI                                         │
│   web.py  ── ручки FastAPI, рендер Jinja2                  │
│   cli.py  ── команды для ручных операций                   │
└──────────────────┬─────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────┐
│ Слой 2. Сервисы / координация                              │
│   dispatch.py      ── выбор канала (TG / email)            │
│   notifications.py ── расписание, ключи уведомлений        │
│   revalidation.py  ── пересчёт сверки                      │
│   altegio_sync.py  ── синхронизация снимков                │
│   sheet_sync.py    ── запись в Sheets                       │
│   worker.py        ── планировщик                          │
│   bot.py           ── Telegram polling                     │
└──────────────────┬─────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────┐
│ Слой 3. Доменная логика                                    │
│   store.py      ── модели, валидация, сверка, миграции     │
│   extract.py    ── парсинг файлов                          │
│   report_template.py ── генерация шаблонов                 │
│   preview.py    ── превью перед подтверждением             │
│   application_breakdown.py ── разбивка по приложениям       │
└──────────────────┬─────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────┐
│ Слой 4. Инфраструктура                                     │
│   store.py (db) ── SQLite через sqlite3                     │
│   secrets.py     ── переменные окружения                   │
│   telegram.py    ── HTTP к Telegram                         │
│   sheets.py      ── google-api-python-client                │
│   mailer.py      ── SMTP                                   │
│   mcp_client.py  ── SSE к Altegio MCP                       │
│   i18n.py, fx.py, countries.py, timezone.py ── утилиты     │
└────────────────────────────────────────────────────────────┘
```

Что важно:

- **Слой 4** ничего не знает про домен. Это чистая инфраструктура.
- **Слой 3** не знает про HTTP и Telegram. Только данные и правила.
- **Слой 2** координирует: вызывает доменные функции и инфраструктуру.
- **Слой 1** принимает запросы и зовёт слой 2 или 3 напрямую.

Если вы хотите изменить логику — обычно правите слой 3 или 2. Если меняется внешний сервис (новый SMTP, новый API) — слой 4.

## Поток данных: что происходит при загрузке файла партнёром

```
1. Партнёр открывает /submit/{token}
   └── web.py → render submit.html (язык из карточки)
   
2. Выбирает месяц и файл, жмёт «Загрузить»
   └── POST /submit/{token}/parse
   └── web.py валидирует токен, размер (≤ 10 МБ), расширение
   └── extract.parse_partner_report(файл)
       └── openpyxl / csv / pypdf → строки
       └── поиск колонки Location ID по заголовку или по числам
       └── возврат черновика (Draft)
       
3. Черновик → report_drafts в SQLite
   └── если снимка Altegio за месяц нет → altegio_sync (MySQL или MCP)
   └── store.assess → discrepancy_items + verify_claim
       └── missing_active, unknown, дубликаты
       └── risk_score, risk_reasons
       └── display_status (red / yellow / green)
       
4. Партнёр видит экран сверки /review/{draft_token}
   └── может подтвердить или изменить причины расхождений
   └── POST /submit/{token}/confirm/{draft_token}
   └── черновик → reports + report_clients + report_claims + report_versions
   
5. Админ в /reports/{id} или /admin/accrual
   └── подтверждает начисление
   └── POST /admin/accrual/queue → sheet_writes
   └── sheet_sync.write → Google Sheets (Partnerships Altegio Revenue Accrual)
```

## Поток данных: уведомления

```
worker.py каждый час
  └── dispatch.dispatch_for_partner(partner, period, notice_key)
       └── выбирает каналы: telegram (если привязан), email (если есть)
       └── telegram.telegram_send_message или mailer.send
       └── notice_sends → лог отправки
       └── защита от дублей: тот же notice_key+period не шлётся повторно
```

## Поток данных: Telegram-бот

```
bot.py polling в отдельном процессе (worker)
  └── /start {token} → привязка chat_id к partner_id
  └── /start digest   → если user_id в REVSHARE_ADMIN_TELEGRAM_USER_IDS
  └── message.document (ПОКА НЕ РЕАЛИЗОВАНО) → парсинг как в web
```

## Хранилище: SQLite, миграции, бэкапы

База — один файл `data/revshare.db`. Доступ через модуль `sqlite3`, без ORM. Схема и миграции живут в `store.py` в методе `Store.init()`. Каждое изменение схемы — через `ALTER TABLE IF NOT EXISTS` с проверкой `PRAGMA table_info`.

19 таблиц, основные из них:

- `partners` — партнёры
- `partner_applications` — приложения партнёров
- `usage_snapshots` и `usage_application_snapshots` — снимки Altegio
- `reports` — отчёты
- `report_clients` — клиенты в отчёте
- `report_drafts` — черновики
- `report_claims` — причины расхождений
- `report_versions` — версии отчётов
- `report_events` — история workflow
- `bot_interactions` — лог бота
- `app_settings` — настройки (key-value)
- `notice_sends` — лог уведомлений
- `usage_imports` — лог импортов снимков
- `sheet_writes` — очередь записи в Sheets
- `reconciliation_bases` и `reconciliation_base_rows` — независимая база сверки
- `revalidation_tasks` — задачи пересчёта

Полная схема — в `docs/database.md`.

Вложения (`data/uploads`) — на диске, в git не коммитятся. Бэкап базы — копия `revshare.db` через cron. Подробнее — в `docs/runbook.md`.

## Локализация

Поддерживаются 6 языков: `en`, `uk`, `pl`, `hu`, `pt-BR`, `ru`. Язык определяется по карточке партнёра, пользователь может переключить. Все строки — в `src/revshare/i18n.py` (2.4K строк, это самый «толстый» модуль после `store` и `web`).

## Безопасность: что есть и чего нет

**Есть:**

- Basic Auth на админских страницах включается переменной `REVSHARE_ADMIN_AUTH=1` и использует `REVSHARE_ADMIN_PASSWORD`; по умолчанию на этапе MVP отключён.
- Персональные токены для каждого партнёра (`/submit/{token}`).
- Секреты в `data/secrets.env`, файл в `.gitignore`.

**Нет (по состоянию на октябрь 2026):**

- HTTPS (предполагается, что будет reverse proxy).
- Ролевая модель (все админ-действия доступны по одному паролю).
- Аудит действий администратора.
- Rate limiting на ручках партнёра.

## Точки расширения

Если вы хотите добавить новый канал приёма отчётов (например, email-вложения), это типичные точки входа:

- **Новый канал файлов.** Добавить обработчик в `extract.py`, создать черновик через `store.create_draft`, вызвать ту же сверку.
- **Новый канал уведомлений.** Добавить функцию в `dispatch.py`, обновить `notifications.py` и `app_settings`.
- **Новый источник Altegio.** Добавить модуль рядом с `mysql.py` / `mcp_client.py`, реализовать тот же интерфейс (вернуть снимок за период).
- **Новый язык.** Добавить словарь в `i18n.py`, обновить `countries.py` если нужно.

## Где искать что

| Задача | Где |
|---|---|
| Изменить логику сверки | `store.py` → `assess`, `verify_claim`, `discrepancy_items` |
| Изменить парсинг файла | `extract.py` |
| Добавить ручку | `web.py` |
| Изменить шаблон | `templates/` |
| Добавить язык | `i18n.py` |
| Поменять курсы валют | `fx.py` |
| Поменять расписание уведомлений | `notifications.py` |
| Изменить запись в Sheets | `sheet_sync.py` + `sheets.py` |
| Поменять OAuth с Altegio | `mcp_client.py` |
| Изменить шаблон письма | `email_letter.py` |
| Изменить бота | `bot.py` + `telegram.py` |
| Изменить планировщик | `worker.py` |
| Изменить схему БД | `store.py` → `init()` |
| Добавить CLI-команду | `cli.py` |
