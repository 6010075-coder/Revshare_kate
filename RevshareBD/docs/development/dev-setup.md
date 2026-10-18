# Установка и запуск для разработчика

Документ для человека, который впервые открывает проект и хочет поднять его локально за 15 минут.

## Требования

- **Python 3.12+** (проверьте `python3 --version`).
- **Git**.
- **uv** или **pip** (uv быстрее, но pip тоже работает).
- **sqlite3 CLI** (опционально, для отладки БД).

Операционная система — macOS, Linux или WSL2. На «голом» Windows будут проблемы с путями — лучше через WSL.

## Установка за 5 минут

```bash
# Клонировать
git clone https://github.com/altegio/RevshareBD.git
cd RevshareBD

# Виртуальное окружение + зависимости (вариант 1: uv, рекомендуется)
uv venv .venv
uv pip install -e ".[dev]"

# Или вариант 2: pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`pip install -e ".[dev]"` поставит пакет в режиме редактирования (правки в `src/revshare/` подхватываются сразу) + dev-зависимости (pytest, httpx).

## Конфигурация

Скопируйте шаблон секретов:

```bash
cp secrets.env.example data/secrets.env
```

Откройте `data/secrets.env`. **Минимальный набор для локального запуска без интеграций:**

```bash
REVSHARE_ADMIN_PASSWORD=localdev
# MVP: auth is disabled by default; set REVSHARE_ADMIN_AUTH=1 to enable it.
REVSHARE_ADMIN_AUTH=0
REVSHARE_SECRET_KEY=local-secret-not-for-prod
```

**Для реальной отправки уведомлений добавьте:**

```bash
TELEGRAM_BOT_TOKEN=123456:abcdef...           # от @BotFather
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=revshare@altegio.com
SMTP_FROM_NAME="Integration Hub"
SMTP_MODE=starttls
```

**Для Google Sheets:**

```bash
GOOGLE_SERVICE_ACCOUNT_FILE=/полный/путь/к/ключу.json
# или
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
REVSHARE_GOOGLE_SHEET_ID=1GoEn9TiJHMI_7YQ9LkzBnLAoKRB_PvtnootyjQAta5A
```

**Для Altegio BI через MCP:**

```bash
REVSHARE_ALTEGIO_MCP_URL=https://mcp.altegio.dev/bi-data/mcp
REVSHARE_ALTEGIO_MCP_AUTH_SERVER=...
REVSHARE_ALTEGIO_MCP_RESOURCE=...
```

Файл `data/secrets.env` в `.gitignore` — **никогда не коммитьте**.

## Запуск

### Веб-сервер (с UI)

```bash
revshare-web
# или
python -m revshare.web
```

По умолчанию слушает `127.0.0.1:43127`. Откройте в браузере:

```
http://127.0.0.1:43127
```

Демо-партнёр:

```
http://127.0.0.1:43127/submit/demo-partner
```

На этапе MVP логин в админку не требуется. Для возврата Basic Auth задайте `REVSHARE_ADMIN_AUTH=1`; пароль берётся из `REVSHARE_ADMIN_PASSWORD`.

### Фоновый worker (планировщик + Telegram polling)

В отдельном терминале:

```bash
revshare-worker
# или
python -m revshare.worker
```

Без worker'а:

- Не отправляются уведомления (Telegram, email).
- Не обновляются снимки Altegio по расписанию.
- Но всё остальное работает (сверка срабатывает при ручном импорте).

### CLI

```bash
# Статус за месяц
python -m revshare status --month 2026-08

# Импорт файлов из папки (без UI)
python -m revshare ingest inbox/2026-08 --month 2026-08

# Превью без записи
python -m revshare preview --month 2026-08

# Импорт usage из файла
python -m revshare import-usage --month 2026-08 --file data/altegio/2026-08.json

# Проверить доступ к Google Sheets
python -m revshare sheet-probe --month 2026-08

# Записать очередь в Sheets
python -m revshare sheet-flush
```

## Тесты

```bash
pytest
```

В репозитории 104 теста (по состоянию на октябрь 2026). Покрытие: парсинг файлов, сверка, маршруты FastAPI, ручка через TestClient.

Только быстрые:

```bash
pytest -x
```

Конкретный файл:

```bash
pytest tests/test_extract.py
```

С логами:

```bash
pytest -s -v
```

## Структура файлов

```
RevshareBD/
├── src/revshare/         # весь код
├── templates/            # Jinja2-шаблоны HTML
├── static/               # CSS, JS, шрифты
├── content/              # контентные материалы (статьи)
├── docs/                 # документация ← вы здесь
├── tests/                # pytest
├── data/                 # БД, секреты, загрузки (НЕ в git)
├── inbox/                # входящие файлы для CLI-импорта
├── partners.yaml         # карта партнёров ↔ ячейки Sheets
├── pyproject.toml        # зависимости + скрипты
├── uv.lock               # зафиксированные версии
├── secrets.env.example   # шаблон секретов
├── README.md             # верхнеуровневое описание
├── DEPLOY.md             # чек-лист вывода в прод
├── PROJECT_STATUS.md     # аудит 32 функций
└── .github/workflows/    # CI (только test.yml)
```

## Типичные сценарии разработки

### Добавить новую ручку

1. Откройте `src/revshare/web.py`.
2. Найдите подходящую секцию (например, `# --- /admin/partners ---`).
3. Скопируйте существующую ручку как шаблон.
4. Измените `@app.get(...)` → `@app.post(...)` если это POST.
5. Добавьте логику. Если нужна БД — через `Store()`.
6. Добавьте шаблон в `templates/` если нужно.
7. Тест в `tests/test_web.py`.

### Изменить парсинг файла

1. Откройте `src/revshare/extract.py`.
2. Найдите `parse_partner_report`.
3. Поправьте регулярку или логику.
4. Тест в `tests/test_extract.py` — там есть фикстуры `samples/`.

### Изменить логику сверки

1. `src/revshare/store.py` → функции `discrepancy_items`, `verify_claim`, `assess`.
2. **Осторожно**: эти функции влияют на оценку риска отчёта. Меняйте только если понимаете последствия.
3. Тесты в `tests/test_store.py`.

### Изменить схему БД

1. `src/revshare/store.py` → `Store.init()`.
2. Добавьте блок миграции (см. `docs/database.md` → «Как добавить новую колонку»).
3. Запустите `revshare-web` один раз — миграция применится.
4. Проверьте `sqlite3 data/revshare.db ".schema"`.

### Добавить новый язык

1. `src/revshare/i18n.py` — словарь `STRINGS[<new_lang>]`.
2. Проверьте, что все ключи заполнены.
3. Тест в `tests/test_i18n.py` (если есть).

## Отладка

### Логи

По умолчанию — `INFO` в stdout. Включить `DEBUG`:

```bash
REVSHARE_LOG_LEVEL=DEBUG revshare-web
```

### Инспекция БД

```bash
sqlite3 data/revshare.db

# Внутри:
.tables
.schema partners
SELECT * FROM partners;
SELECT id, partner_id, period, status FROM reports WHERE period = '2026-08';
.quit
```

### Сбросить базу (только локально!)

```bash
rm data/revshare.db
revshare-web  # база пересоздастся при старте
```

`data/uploads/` не трогайте — там лежат файлы партнёров.

### Почему не запускается worker?

Скорее всего, не настроены секреты. Проверьте:

```bash
python -m revshare.cli check-config
```

(или просто запустите и посмотрите логи).

## Что НЕ делать

- **Не редактируйте файлы в `data/` руками** (кроме `secrets.env`). Это ломает миграции.
- **Не удаляйте `data/uploads/`** — там файлы, на которые ссылаются отчёты.
- **Не коммитьте `secrets.env` или `revshare.db`** — оба в `.gitignore`.
- **Не редактируйте миграции задним числом.** Если миграция уже в `init()`, она должна быть no-op для существующих баз.

## Полезные однострочники

```bash
# Запустить тесты и сразу UI
pytest && revshare-web

# Логи в фоне + UI
revshare-web &> /tmp/revshare-web.log &
revshare-worker &> /tmp/revshare-worker.log &

# Проверить, что в админке
curl -u admin:localdev http://127.0.0.1:43127/admin/partners

# Загрузить файл через curl
curl -X POST -F "period=2026-08" -F "file=@test.xlsx" \
  http://127.0.0.1:43127/submit/demo-partner/parse

# Узнать, какие таблицы в БД
sqlite3 data/revshare.db ".tables"
```

## Что дальше

- Архитектура — `docs/architecture.md`.
- API — `docs/api.md`.
- Схема БД — `docs/database.md`.
- Что делать когда сломалось — `docs/runbook.md`.
- План вывода в прод — `DEPLOY.md` (в корне проекта).
