# Схема базы данных

База — SQLite, файл `data/revshare.db`. Доступ через `sqlite3` (без ORM). Схема и миграции описаны в `src/revshare/store.py` → `Store.init()`. Этот документ — справочник по таблицам, полям и связям.

## Общие сведения

- **Версия SQLite:** 3.x, файл создаётся автоматически при первом запуске.
- **Миграции:** идемпотентные `ALTER TABLE IF NOT EXISTS` и `CREATE INDEX IF NOT EXISTS` в `init()`. Без Alembic, без отдельного журнала миграций.
- **Часовой пояс:** все даты хранятся в UTC ISO-формате (`%Y-%m-%dT%H:%M:%S`), кроме `period` — это `YYYY-MM`.
- **JSON в текстовых полях:** `reasons`, `missing_active`, `unknown_locations`, `claims`, `application_breakdown`, `notifications` — сериализованные JSON-массивы/объекты.
- **Булевы как INTEGER:** `0` / `1` (стандарт SQLite).

## Карта таблиц

```
                    ┌─────────────┐
                    │  partners   │  ← корневая сущность
                    └──────┬──────┘
                           │1:N
        ┌──────────────────┼────────────────────┬──────────────┐
        ▼                  ▼                    ▼              ▼
   partner_apps    reports (уникальный   usage_snapshots   bot_interactions
                   partner+period+app)   usage_application
        │                  │                _snapshots
        │                  │1:N
        │        ┌─────────┼──────────┬──────────────┐
        │        ▼         ▼          ▼              ▼
        │  report_clients  report_   report_     report_
        │                  drafts    claims      versions
        │                              ▼
        │                       revalidation_tasks
        ▼
   application_breakdown (в report JSON)
   
   Глобальные:
   - app_settings (key-value)
   - notice_sends (лог уведомлений)
   - usage_imports (лог импортов)
   - report_events (workflow лог)
   - sheet_writes (очередь Sheets)
   - reconciliation_bases + _rows (независимая база)
   - revalidation_tasks (задачи пересчёта)
```

## Таблицы

### `partners`

Карточка партнёра. Корневая сущность, на ней завязано почти всё.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | внутренний идентификатор |
| `name` | TEXT UNIQUE | название (как в Google Sheets) |
| `legal_name` | TEXT | юридическое название |
| `legal_country` | TEXT | страна юрлица |
| `altegio_legal_entity` | TEXT | имя юрлица в Altegio |
| `contact_name` | TEXT | контактное лицо |
| `email` | TEXT | адрес для уведомлений |
| `telegram` | TEXT | `@username` или chat_id |
| `report_channel` | TEXT | `email`, `telegram`, `both` |
| `internal_comment` | TEXT | заметки админа |
| `token` | TEXT UNIQUE | персональная ссылка `/submit/{token}` |
| `rate` | REAL | ставка revshare (доля от оборота) |
| `currency` | TEXT | валюта расчёта, `USD` по умолчанию |
| `active` | INTEGER | `1` = активен, `0` = архив |
| `notifications` | TEXT JSON | индивидуальные настройки уведомлений |
| `report_automation_status` | TEXT | `manual`, `partial`, `auto` |

### `partner_applications`

Приложения партнёра. Связь N:1 с `partners`.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | внутренний |
| `partner_id` | INTEGER FK → `partners.id` | |
| `url` | TEXT | ссылка на приложение в Хабе |
| `comment` | TEXT | заметка админа |
| `category` | TEXT | категория из Хаба (ИИ-боты, SMS и т.д.) |

UNIQUE по `(partner_id, url)`.

### `usage_snapshots`

Снимок Altegio BI за период: подключения и активность по Location ID.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER FK → `partners.id` | |
| `period` | TEXT | `YYYY-MM` |
| `location_id` | TEXT | ID филиала |
| `is_connected` | INTEGER | подключён ли на конец месяца |
| `is_active` | INTEGER | была ли активность API |
| `api_events` | INTEGER | число событий API |
| `had_payment` | INTEGER (добавлено миграцией) | был ли платёж |
| `location_name` | TEXT (миграция) | название филиала |
| `connected_at` | TEXT (миграция) | дата подключения |
| `last_api_at` | TEXT (миграция) | дата последней активности |
| `has_api_amounts` | INTEGER (миграция) | есть ли суммы по API |

UNIQUE по `(partner_id, period, location_id)`.

### `usage_application_snapshots`

Снимок Altegio BI в разрезе приложений (для мульти-приложения).

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER FK → `partners.id` | |
| `period` | TEXT | `YYYY-MM` |
| `application_id` | INTEGER FK → `partner_applications.id` | |
| `location_id` | TEXT | |
| `is_connected` | INTEGER | |
| `is_active` | INTEGER | |
| `had_payment` | INTEGER | |
| `location_name` | TEXT | |
| `connected_at` | TEXT | |
| `last_api_at` | TEXT | |
| `has_api_amounts` | INTEGER | |

UNIQUE по `(partner_id, period, application_id, location_id)`.

### `reports`

Итоговый отчёт партнёра за период. Один на пару `(partner, period, application)`.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER FK → `partners.id` | |
| `period` | TEXT | `YYYY-MM` |
| `application_id` | INTEGER (NULL если нет явного APP) | ссылка на `partner_applications.id` |
| `turnover` | REAL | оборот в валюте партнёра |
| `currency` | TEXT | валюта |
| `reported_share` | REAL | ожидаемая сумма revshare от партнёра |
| `contract_rate` | REAL (миграция) | ставка из договора на момент отчёта |
| `client_count` | INTEGER | число Location ID |
| `status` | TEXT | workflow-статус |
| `risk_level` | TEXT | `red`, `yellow`, `green` |
| `risk_score` | INTEGER | 0–100 |
| `reasons` | TEXT JSON | причины риска |
| `missing_active` | TEXT JSON | активные Location, не попавшие в отчёт |
| `unknown_locations` | TEXT JSON | Location из отчёта, не найденные в Altegio |
| `comment` | TEXT | комментарий партнёра |
| `attachment` | TEXT | путь к файлу в `data/uploads/` |
| `report_source` | TEXT (миграция) | `partner_file`, `manual`, `bot` |
| `submitted_at` | TEXT | когда партнёр нажал «Подтвердить» |
| `status_updated_at` | TEXT (миграция) | когда последний раз менялся `status` |
| `approved_at` | TEXT (миграция) | когда админ подтвердил начисление |
| `approved_by` | TEXT (миграция) | кто подтвердил |
| `posted_at` | TEXT (миграция) | когда записано в Google Sheets |
| `application_breakdown` | TEXT JSON (миграция) | разбивка по приложениям |
| `workflow_status` | TEXT (миграция) | `auto_checked`, `needs_review`, `approved`, `posted`, `failed` |
| `file_id` | INTEGER (миграция) | ссылка на `uploaded_files` для исходного файла |
| `source_bot_id` | INTEGER (миграция) | ссылка на Telegram-бота, от имени которого пришёл отчёт |

UNIQUE по `(partner_id, period, application_id)` (после миграции). Без `application_id` — старые отчёты до введения мульти-приложений.

### `report_clients`

Связь отчёта с Location ID (многие-ко-многим).

| Поле | Тип | Что хранит |
|---|---|---|
| `report_id` | INTEGER FK → `reports.id` | |
| `location_id` | TEXT | ID филиала |

UNIQUE по `(report_id, location_id)`.

### `report_drafts`

Черновик отчёта. Создаётся при загрузке файла, после подтверждения становится `reports`.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `token` | TEXT UNIQUE | токен черновика для URL `/review/{draft_token}` |
| `partner_id` | INTEGER FK | |
| `period` | TEXT | `YYYY-MM` |
| `application_id` | INTEGER (миграция) | |
| `attachment` | TEXT | путь к файлу |
| `turnover` | REAL | извлечённый оборот (может быть NULL если не нашли) |
| `currency` | TEXT | извлечённая валюта |
| `reported_share` | REAL | извлечённая ожидаемая сумма |
| `location_ids` | TEXT JSON | список извлечённых Location ID |
| `extraction_notes` | TEXT JSON | заметки парсера: что не нашли, что пропустили |
| `application_breakdown` | TEXT JSON | разбивка по приложениям |
| `created_at` | TEXT | |
| `file_id` | INTEGER (миграция) | ссылка на исходный файл |
| `source_bot_id` | INTEGER (миграция) | бот-источник |

Частичный UNIQUE INDEX `(partner_id, period, application_id) WHERE application_id IS NOT NULL` — один черновик на партнёра+период+приложение.

### `report_claims`

Причины расхождений, которые партнёр указал при подтверждении.

| Поле | Тип | Что хранит |
|---|---|---|
| `report_id` | INTEGER FK | |
| `location_id` | TEXT | |
| `kind` | TEXT | `active_not_reported`, `connected_not_active`, `reported_not_connected` |
| `reason` | TEXT | `no_payment`, `free_trial`, `pay_next_month`, `disconnected_before_month`, `connected_after_month`, `file_error`, `other`, `unexplained` |
| `detail` | TEXT | свободный комментарий |
| `verification_status` | TEXT | `confirmed`, `contradicted`, `review`, `follow_up` |
| `verification_evidence` | TEXT | `payment_found`, `usage_after_claimed_disconnect` и т.д. |

UNIQUE по `(report_id, location_id, kind)`.

### `report_versions`

История версий отчёта. Каждое подтверждение — новая версия.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `report_id` | INTEGER FK | |
| `version` | INTEGER | 1, 2, 3… |
| `turnover`, `currency`, `reported_share`, `contract_rate` | то же, что в `reports` | |
| `client_count`, `status`, `risk_level`, `risk_score` | | |
| `reasons`, `missing_active`, `unknown_locations`, `claims` | TEXT JSON | снимок на момент подтверждения |
| `comment` | TEXT | |
| `attachment` | TEXT | путь к файлу |
| `submitted_at` | TEXT | |
| `application_breakdown` | TEXT JSON | |
| `file_id` | INTEGER (миграция) | ссылка на исходный файл |
| `source_bot_id` | INTEGER (миграция) | бот-источник |

UNIQUE по `(report_id, version)`.

### `report_events`

Лог изменения workflow-статуса отчёта.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `report_id` | INTEGER FK | |
| `report_version_id` | INTEGER FK, NULL | версия отчёта, к которой относится событие; для событий до первой отправки может быть пустым |
| `workflow_status` | TEXT | новый статус |
| `note` | TEXT | заметка |
| `actor` | TEXT | кто изменил (`partner`, `admin:NAME`, `system`) |
| `created_at` | TEXT | когда |
| `bot_id` | INTEGER (миграция) | бот, создавший событие; пусто для старых/веб-событий |
| `file_id` | INTEGER (миграция) | исходный файл, связанный с событием |

### `bot_interactions`

Лог всех взаимодействий с Telegram-ботом.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER (NULL если не привязан) | |
| `chat_id` | TEXT | |
| `update_id` | INTEGER | ID update от Telegram |
| `kind` | TEXT | `/start`, `document`, `text`, … |
| `direction` | TEXT | `incoming`, `outgoing` |
| `payload` | TEXT | тело запроса/ответа |
| `response` | TEXT | что ответил бот |
| `report_id` | INTEGER | если привязан к отчёту |
| `draft_id` | INTEGER | если привязан к черновику |
| `period` | TEXT | если относится к периоду |
| `bot_id` | INTEGER (миграция) | бот, который обработал событие |
| `file_id` | INTEGER (миграция) | связанный исходный файл |
| `created_at` | TEXT | |

### `telegram_bots`

История Telegram-ботов, которые подключались к приложению. Одновременно активен только один бот.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | внутренний идентификатор |
| `telegram_bot_id` | INTEGER UNIQUE | постоянный числовой ID бота в Telegram |
| `username` | TEXT | username на момент последней проверки |
| `first_name` | TEXT | имя бота |
| `token_fingerprint` | TEXT | отпечаток токена, сам токен не хранится в БД |
| `is_active` | INTEGER | активен ли бот сейчас |

### `uploaded_files`

Карточки файлов, которые физически лежат в `data/uploads/`. Сами файлы в SQLite не помещаются.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | идентификатор файла |
| `storage_name` | TEXT UNIQUE | внутреннее имя файла на диске |
| `original_name` | TEXT | имя файла от партнёра |
| `sha256` | TEXT | контрольная сумма |
| `size_bytes` | INTEGER | размер |
| `mime_type` | TEXT | тип файла |
| `source` | TEXT | `partner_web`, `telegram_bot`, `admin` или `unknown` |
| `partner_id` | INTEGER | партнёр |
| `bot_id` | INTEGER | бот-источник, если файл пришёл через Telegram |
| `created_at` | TEXT | время загрузки |

### `app_settings`

Глобальные настройки приложения (key-value).

| Поле | Тип | Что хранит |
|---|---|---|
| `key` | TEXT PK | |
| `value` | TEXT | JSON или строка |

Примеры ключей: `public_form_url`, `telegram_enabled`, `notice_schedule`, `notice_text_en`, `fx_rates_json`, `admin_chat_id`, `mcp_token`.

### `notice_sends`

Лог отправки уведомлений. Защита от дублей.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER FK | |
| `period` | TEXT | |
| `notice_key` | TEXT | `invite_telegram`, `first_reminder`, `second_reminder`, `manual_request` |
| `status` | TEXT | `sent`, `failed`, `skipped` |
| `detail` | TEXT | ошибка, если есть |
| `sent_at` | TEXT | |

### `usage_imports`

Лог импорта снимков Altegio.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `partner_id` | INTEGER FK | |
| `period` | TEXT | |
| `source` | TEXT | `mysql`, `mcp`, `manual_upload` |
| `connected_count` | INTEGER | |
| `active_count` | INTEGER | |
| `payment_count` | INTEGER | |
| `imported_at` | TEXT | |

### `sheet_writes`

Очередь записи в Google Sheets.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `report_id` | INTEGER FK | |
| `period` | TEXT | |
| `partner_name` | TEXT | |
| `turnover` | REAL | |
| `currency` | TEXT | |
| `expected_share` | REAL | |
| `status` | TEXT | `queued`, `written`, `failed` |
| `detail` | TEXT | ошибка или подтверждение |
| `sheet_cell` | TEXT | адрес ячейки `Partnerships Altegio Revenue Accrual!A1` |
| `posted_at` | TEXT | когда фактически записано |

### `reconciliation_bases`

Загруженный файл независимой базы сверки за период.

| Поле | Тип | Что хранит |
|---|---|---|
| `period` | TEXT PK | `YYYY-MM` |
| `uploaded_at` | TEXT | |
| `uploaded_by` | TEXT | |
| `file_path` | TEXT | путь к исходному файлу |
| `row_count` | INTEGER | |
| `notes` | TEXT | |

### `reconciliation_base_rows`

Строки независимой базы сверки.

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | INTEGER PK | |
| `period` | TEXT | `YYYY-MM` |
| `application_id` | INTEGER FK → `partner_applications.id` | |
| `location_id` | TEXT | |
| `turnover` | REAL | независимый оборот |
| `currency` | TEXT | |
| `active` | INTEGER | был ли активен в периоде |
| `source_row` | INTEGER | номер строки в исходном файле |

UNIQUE по `(period, application_id, location_id)`. INDEX на `period`.

### `revalidation_tasks`

Задача пересчёта сверки (после обновления снимка).

| Поле | Тип | Что хранит |
|---|---|---|
| `id` | TEXT PK | UUID |
| `period` | TEXT | |
| `total` | INTEGER | сколько отчётов нужно пересчитать |
| `processed` | INTEGER | сколько уже |
| `current_report` | TEXT | какой сейчас в работе |
| `status` | TEXT | `pending`, `running`, `done`, `failed` |
| `started_at` | TEXT | |
| `finished_at` | TEXT | |
| `summary` | TEXT JSON | статистика пересчёта |

INDEX `(period, status)`.

## Связи между таблицами

| Связь | Тип |
|---|---|
| `partners` → `partner_applications` | 1:N |
| `partners` → `reports` | 1:N |
| `partners` → `report_drafts` | 1:N |
| `partners` → `usage_snapshots` | 1:N |
| `partners` → `usage_application_snapshots` | 1:N |
| `partners` → `bot_interactions` | 1:N |
| `partners` → `notice_sends` | 1:N |
| `partner_applications` → `reports` | 1:N (через `application_id`) |
| `partner_applications` → `reconciliation_base_rows` | 1:N |
| `reports` → `report_clients` | 1:N |
| `reports` → `report_claims` | 1:N |
| `reports` → `report_versions` | 1:N |
| `reports` → `report_events` | 1:N |
| `report_versions` → `report_events` | 1:N, необязательная связь |
| `reports` → `sheet_writes` | 1:N |
| `report_drafts` → `partner_applications` | N:1 (через `application_id`) |

## Индексы

Явные индексы в схеме:

- `partners`: UNIQUE на `name`, `token`
- `partner_applications`: UNIQUE на `(partner_id, url)`
- `usage_snapshots`: UNIQUE на `(partner_id, period, location_id)`
- `usage_application_snapshots`: UNIQUE на `(partner_id, period, application_id, location_id)`
- `reports`: UNIQUE на `(partner_id, period, application_id)` (после миграции)
- `report_clients`: UNIQUE на `(report_id, location_id)`
- `report_drafts`: UNIQUE на `token`; частичный UNIQUE на `(partner_id, period, application_id) WHERE application_id IS NOT NULL`
- `report_claims`: UNIQUE на `(report_id, location_id, kind)`
- `report_versions`: UNIQUE на `(report_id, version)`
- `report_events`: INDEX на `report_version_id`
- `reconciliation_base_rows`: UNIQUE на `(period, application_id, location_id)`, INDEX на `period`
- `revalidation_tasks`: INDEX на `(period, status)`

## Как посмотреть схему в живую

```bash
sqlite3 data/revshare.db ".schema"
sqlite3 data/revshare.db ".tables"
sqlite3 data/revshare.db "PRAGMA table_info(reports);"
sqlite3 data/revshare.db "SELECT name FROM sqlite_master WHERE type='index';"
```

## Как добавить новую колонку

1. Откройте `src/revshare/store.py`, найдите метод `Store.init()`.
2. Добавьте блок:

```python
table_columns = {
    row["name"] for row in db.execute("PRAGMA table_info(<table>)").fetchall()
}
if "<new_column>" not in table_columns:
    db.execute(
        "ALTER TABLE <table> ADD COLUMN <new_column> <type> NOT NULL DEFAULT <default>"
    )
```

3. Запустите `revshare-web` один раз — миграция применится автоматически.
4. Проверьте:

```bash
sqlite3 data/revshare.db "PRAGMA table_info(<table>);"
```

## Бэкап

```bash
# Остановить worker (на всякий случай)
cp data/revshare.db data/backups/revshare-$(date +%F).db
# Лучше — sqlite3 .backup (атомарный снимок):
sqlite3 data/revshare.db ".backup data/backups/revshare-$(date +%F).db"
```

`data/uploads/` — отдельно (там лежат файлы партнёров). Подробнее — в `docs/runbook.md`.
