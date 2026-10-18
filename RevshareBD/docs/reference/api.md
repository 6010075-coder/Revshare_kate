# API-документация

HTTP-ручки FastAPI приложения. Основано на `src/revshare/web.py`. Все ручки рендерят HTML (Jinja2) кроме случаев, помеченных `→ файл` (CSV/Excel).

База — `http://127.0.0.1:43127` локально, в проде — за reverse proxy.

## Группировка

Ручки делятся на четыре зоны:

| Префикс | Назначение | Доступ |
|---|---|---|
| `/` | Главная | открыто |
| `/language/{lang}` | Переключение языка | открыто |
| `/submit/{token}` | Партнёрская форма | по персональной ссылке |
| `/admin/*` | Админка | Basic Auth |
| `/reports/{id}` | Публичный отчёт | по ссылке |

## Аутентификация

- **Партнёры** — по уникальному токену из URL (`/submit/{token}`). Токен — 32 hex-символа, генерируется при создании партнёра.
- **Админ** — на этапе MVP без логина. Basic Auth включается переменной `REVSHARE_ADMIN_AUTH=1` и использует пароль из `REVSHARE_ADMIN_PASSWORD`; middleware защищает все админские ручки.

## Главная и язык

### `GET /`

Главная страница. Если пользователь — админ, редирект на `/admin/partners`. Иначе — на логина админки (или показывает сообщение «нет доступа»).

### `GET /language/{language}`

Переключает язык интерфейса. Ставит cookie `language` и редиректит обратно.

- `language`: `en`, `ru`, `uk`, `pl`, `hu`, `pt-BR`

## Партнёрская форма

### `GET /submit/{token}`

Открывает форму партнёра.

- **Параметры:** `token` — уникальный токен партнёра.
- **Поведение:**
  - Если партнёр архивирован — 404.
  - Подставляет язык из карточки партнёра.
  - Показывает форму выбора месяца и загрузки файла.

### `GET /submit/{token}/template.xlsx`

Скачивает шаблон Excel с примером заполнения.

### `GET /submit/{token}/template.csv`

То же в CSV.

### `POST /submit/{token}/parse`

Загружает файл и парсит его.

- **Content-Type:** `multipart/form-data`
- **Поля формы:**
  - `period` — `YYYY-MM`
  - `application_id` — ID приложения (если у партнёра несколько)
  - `file` — файл Excel/CSV/PDF, до 10 МБ
- **Поведение:**
  - Валидирует расширение и размер.
  - `extract.parse_partner_report` извлекает оборот, валюту, Location ID.
  - Если снимка Altegio за период нет — запускает синхронизацию.
  - Сохраняет черновик в `report_drafts`.
  - Редиректит на `/submit/{token}/review/{draft_token}`.

### `GET /submit/{token}/review/{draft_token}`

Экран сверки черновика. Показывает:

- Извлечённые данные (оборот, валюта, ожидаемая сумма, Location ID).
- Расхождения (active_not_reported, connected_not_active, reported_not_connected).
- Форму для указания причин расхождений.
- Итоговый риск-скор.

### `GET /submit/{token}/review/{draft_token}/full-report.csv`

→ CSV. Полный отчёт по черновику со всеми строками.

### `GET /submit/{token}/review/{draft_token}/differences.csv`

→ CSV. Только расхождения.

### `POST /submit/{token}/confirm/{draft_token}`

Подтверждение черновика партнёром.

- **Поля формы:**
  - `comment` — общий комментарий
  - по одному полю на каждое расхождение: `claim[{location_id}][reason]`, `claim[{location_id}][detail]`
- **Поведение:**
  - Создаёт запись в `reports`, `report_clients`, `report_claims`, `report_versions` (версия 1).
  - Логирует событие в `report_events` (`workflow_status=auto_checked`).
  - Удаляет черновик.
  - Показывает страницу «Спасибо, отчёт принят».

## Админка: партнёры

### `GET /admin/partners`

Список партнёров с фильтрами.

### `POST /admin/partners`

Создание нового партнёра.

- **Поля формы:**
  - `name` — название (обязательно, уникально)
  - `legal_name`, `legal_country`, `altegio_legal_entity`, `contact_name`, `email`, `telegram`, `report_channel`, `internal_comment`
  - `rate` — ставка revshare
  - `currency` — `USD`, `EUR`, …
  - `active` — `1` или `0`

### `GET /admin/partners/{partner_id}/edit`

Карточка партнёра с тремя вкладками: основная информация, настройки, история.

### `POST /admin/partners/{partner_id}/edit`

Сохранение изменений карточки.

### `GET /admin/partners/{partner_id}/drafts/{draft_id}/download`

Скачать исходный файл неподтверждённого отчёта. Удаление черновика из админки не предусмотрено.

### `POST /admin/partners/{partner_id}/archive`

Архивирует партнёра (`active=0`). Ссылка перестаёт работать.

### `POST /admin/partners/{partner_id}/delete`

Удаляет партнёра. Каскадно. Только если нет связанных отчётов. Иначе — soft-delete.

### `POST /admin/partners/{partner_id}/restore`

Восстанавливает из архива.

### `GET /admin/partners/{partner_id}/report`

Превью отчёта партнёра (как `/reports/{id}`, но с правами админа).

### `POST /admin/partners/{partner_id}/report`

Ручное создание отчёта (без файла). Используется, если партнёр не прислал данные.

- **Поля:** `period`, `turnover`, `currency`, `reported_share`, `comment`, `source`

### `POST /admin/partners/{partner_id}/notifications`

Сохраняет индивидуальные настройки уведомлений для партнёра.

### `POST /admin/partners/{partner_id}/notify`

Отправляет ручное уведомление (тест).

### `POST /admin/partners/{partner_id}/telegram/unlink`

Отвязывает Telegram-чат от партнёра.

## Админка: начисления

### `GET /admin/accrual`

Страница «Начисления». Два списка: «Готовы к отправке» и «Очередь записи».

### `POST /admin/accrual/queue`

Подтверждает начисление: добавляет в очередь `sheet_writes`.

- **Поля:** `report_ids[]` — несколько ID отчётов.

### `POST /admin/accrual/flush`

Запускает запись очереди в Google Sheets.

## Админка: данные Altegio

### `GET /admin/usage`

Просмотр снимков Altegio BI.

### `POST /admin/usage`

Загрузка JSON-снимка вручную.

- **Поля:** `period`, `partner_id`, `file` или `text` — JSON с блоком `locations`.

### `POST /admin/settings/altegio/sync`

Запускает синхронизацию с Altegio BI через MCP или MySQL.

### `POST /admin/settings/altegio/import`

Импорт ранее загруженного файла.

## Админка: сверка

### `GET /admin/reconciliation-base/{period}`

Просмотр независимой базы сверки за период.

### `GET /admin/reconciliation-base/{period}/upload`

Форма загрузки независимой базы (Excel/CSV).

### `POST /admin/reconciliation-base/{period}`

Загрузка независимой базы.

- **Поля:** `file`, `notes`.

### `GET /admin/disconnect-candidates`

Кандидаты на отключение (по всем периодам).

### `GET /admin/disconnect-candidates/{period}.csv`

→ CSV. Кандидаты на отключение за конкретный период.

## Админка: отключения

Уже упомянуто в сверке.

## Админка: настройки

### `GET /admin/settings`

Страница всех настроек.

### `POST /admin/settings/general`

Сохраняет общие настройки:

- `public_form_url`
- `telegram_enabled`
- `mcp_enabled`

### `POST /admin/settings/altegio`

Настройки Altegio:

- `mcp_url`
- `mcp_auth_server`
- `mcp_resource`

### `POST /admin/settings/telegram`

Настройки Telegram:

- `bot_token`
- `admin_user_ids` (через запятую)

### `POST /admin/settings/bot`

Проверяет новый Telegram-токен через `getMe`, сохраняет его в `data/secrets.env`, фиксирует Telegram ID бота и даёт воркеру команду перезапустить polling. Сам токен в БД и журнал не записывается.

- `bot_token` — новый токен BotFather.

### `POST /admin/settings/bot/remove`

Удаляет токен из управляемого файла секретов, деактивирует текущего бота и останавливает polling.

### `GET /admin/files/{file_id}`

Скачивает исходный файл партнёрского отчёта по внутреннему ID из журнала. Файл берётся из `data/uploads/`, а не из SQLite.

### `POST /admin/settings/schedule`

Расписание уведомлений (по партнёру или глобально).

### `POST /admin/rates`

Обновить курсы валют к USD вручную.

### `POST /admin/rates/refresh`

Загрузить актуальные курсы из источника.

## Админка: уведомления

### `GET /admin/notices`

Список уведомлений и шаблонов.

### `POST /admin/notices`

Сохранить шаблон уведомления:

- `notice_key`, `language`, `subject`, `body_html`, `body_text`

### `POST /admin/notices/run`

Запустить отправку уведомлений сейчас (без ожидания планировщика).

### `GET /admin/partners/{partner_id}/letter/{notice_key}`

Превью письма для партнёра по конкретному типу уведомления.

## Админка: OAuth MCP

### `GET /admin/settings/mcp/connect`

Инициирует OAuth-процедуру с Altegio MCP. Редиректит на страницу входа Google.

### `GET /admin/settings/mcp/callback`

Callback после OAuth. Сохраняет токен в `data/mcp_token.json`.

## Публичные отчёты

### `GET /reports/{report_id}`

Открывает итоговый отчёт по его ID. Доступ по прямой ссылке (без токена).

- Показывает: оборот, ожидаемую сумму, статус, риск, причины, историю версий.

### `GET /reports/{report_id}/versions/{version_number}/download`

Скачивает исходный файл выбранной версии. Для ручной версии без вложения
возвращает CSV-снимок сохранённых значений версии.

## Коды ответов

| Код | Когда |
|---|---|
| 200 | OK |
| 302 | Редирект (после POST, смена языка) |
| 400 | Ошибка валидации формы |
| 401 | Нет доступа к админке (нет Basic Auth) |
| 403 | Доступ запрещён (например, архивированный партнёр) |
| 404 | Токен не найден, отчёт не найден |
| 413 | Файл больше 10 МБ |
| 415 | Неподдерживаемый тип файла |
| 500 | Внутренняя ошибка (логируется) |

## Скачиваемые файлы

| URL | Формат | Что внутри |
|---|---|---|
| `/submit/{token}/template.xlsx` | Excel | Шаблон для партнёра |
| `/submit/{token}/template.csv` | CSV | То же |
| `/submit/{token}/review/{draft_token}/full-report.csv` | CSV | Полный черновик |
| `/submit/{token}/review/{draft_token}/differences.csv` | CSV | Только расхождения |
| `/admin/disconnect-candidates/{period}.csv` | CSV | Кандидаты на отключение |

## Cookies

| Имя | Назначение | Срок |
|---|---|---|
| `language` | Текущий язык интерфейса | 1 год |
| `Authorization` | Basic Auth для админки | сессия |

## CSRF

Защита от CSRF в проекте не реализована (TODO). Все POST-ручки должны будут получать проверку перед выходом в прод, особенно админские. Сейчас считается, что админка закрыта Basic Auth, а партнёрские формы принимают файл (multipart) — для них CSRF менее критичен, но всё равно стоит добавить.

## OpenAPI

FastAPI генерирует OpenAPI-схему, но она не отдаётся через `/docs` или `/openapi.json` потому что все ручки — HTML, а не JSON. Если нужна авто-документация — можно добавить отдельные JSON-эндпоинты как обёртки над сервисами. Сейчас этот документ — единственный источник правды.

## Где в коде искать ручку

```bash
grep -n "@app\." src/revshare/web.py
```

Числа в начале строк — номера строк в `web.py` (по состоянию на октябрь 2026). Используйте их как быструю навигацию.
