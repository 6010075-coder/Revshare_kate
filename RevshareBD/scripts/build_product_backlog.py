"""Build product-backlog.xlsx from the same source data as docs/product-backlog.md.

Run: python scripts/build_product_backlog.py
Output: docs/product-backlog.xlsx
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo


# ---------- source data ----------

EPICS = [
    (
        "E1",
        "Парсинг и чек-лист файла",
        "Партнёр прислал файл → система точно и понятно извлекла данные, сказала «всё ок» или «вот что не так».",
    ),
    (
        "E2",
        "Сверка",
        "Админ видит не «форма прошла», а «сумма похожа на правду». Детальный план — epic-reconciliation.md.",
    ),
    (
        "E3",
        "Telegram-бот",
        "Партнёр открывает бота, отправляет отчёт, видит вердикт, подтверждает.",
    ),
    (
        "E4",
        "Email-канал (входящие)",
        "Партнёр может прислать отчёт по почте, не открывая бота/форму.",
    ),
    (
        "E5",
        "Начисления → Google Sheets",
        "Подтверждённые отчёты попадают в «Partnerships Altegio — Revenue Accrual» с правильной ячейкой и без перезаписи.",
    ),
    (
        "E6",
        "Партнёрская форма",
        "Партнёр по своей ссылке загружает файл, видит сверку, отправляет — без путаниц.",
    ),
    (
        "E7",
        "UI админки / навигация",
        "Админ тратит 2 минуты на партнёра вместо 10. Без разброса, без поиска.",
    ),
    (
        "E8",
        "Инфраструктура и прод",
        "Сервис работает 24/7 в проде, не падает, логи пишутся, бекапы есть.",
    ),
    (
        "E9",
        "Защита данных и комплаенс",
        "Партнёрские данные защищены, доступ только уполномоченным, retention ясен.",
    ),
]


TASKS: list[dict] = [
    # ---- E1 ----
    {"epic": "E1", "id": "T01", "title": "Парсер Excel/CSV/PDF/текста", "links": "F09, MVP 1.4",
     "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E1", "id": "T02", "title": "Резервное чтение Location ID из произвольных форматов",
     "links": "F12", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E1", "id": "T03", "title": "Чек-лист обязательных полей как отдельный шаг",
     "links": "US-5, MVP 1.6", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E1", "id": "T04", "title": "Человеческие объяснения расхождений (не ключи i18n)",
     "links": "US-5", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E1", "id": "T05", "title": "Отправка шаблона таблицы партнёру до начала периода",
     "links": "MVP 1.5", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E1", "id": "T06", "title": "Шаблон вызова «с переполнить» — регенерация с преcетом",
     "links": "US-5", "status": "🔵 Не начата", "priority": "Could", "owner": "", "due": ""},

    # ---- E2 ----
    {"epic": "E2", "id": "R01", "title": "Базовая store.assess (структурная сверка)",
     "links": "F17, F18, F19", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R02", "title": "Построчная verify_claim для статусов оплаты",
     "links": "F18", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R03", "title": "Независимый оборот из BI / Elastic",
     "links": "Epic Rec. §1", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R04", "title": "Impsipi Elastic как источник «активности»",
     "links": "Epic Rec. §2", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R05", "title": "Кросс-партнёрское пересечение клиентов",
     "links": "Epic Rec. §3", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R06", "title": "Отображение причин расхождений в UI партнёра",
     "links": "F22, US-5", "status": "🟡 В работе", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E2", "id": "R07", "title": "Оценка риска (red/yellow/green) в UI",
     "links": "F19", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R08", "title": "Отслеживание аномалий оборота (скачок/падение)",
     "links": "assess() turnover_drop/spike", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R09", "title": "Подключение Altegio MCP (mcp.altegio.dev)",
     "links": "README", "status": "🟡 В работе", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E2", "id": "R10", "title": "Сравнение остальных значений в отчётах (оборот, возвраты, штрафы)",
     "links": "MVP 2.3", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},

    # ---- E3 ----
    {"epic": "E3", "id": "B01", "title": "Привязка чата по /start {token}",
     "links": "F19", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E3", "id": "B02", "title": "Исходящие напоминания (планировщик)",
     "links": "F02", "status": "🟡 В работе", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E3", "id": "B03", "title": "Приём файлов (message.document)",
     "links": "MVP 1.1, Epic Bot", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E3", "id": "B04", "title": "Inline-кнопки «Подтвердить» / «Посмотреть расхождения»",
     "links": "MVP 1.1, US-5", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E3", "id": "B05", "title": "Привязка chat_id → partner_id (после загрузки файла)",
     "links": "F19", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E3", "id": "B06", "title": "Отправка результата сверки в чат",
     "links": "F29, US-5", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E3", "id": "B07", "title": "Очередь в Sheets по «Подтвердить» в боте",
     "links": "MVP 2.5, US-10", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},

    # ---- E4 ----
    {"epic": "E4", "id": "M01", "title": "Входящий почтовый ящик / IMAP poller",
     "links": "F04", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E4", "id": "M02", "title": "Приём вложений Excel",
     "links": "MVP 1.2", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E4", "id": "M03", "title": "Сопоставление отправителя с партнёром",
     "links": "F04", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E4", "id": "M04", "title": "Сохранение оригинала и канала приёма",
     "links": "F04", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},

    # ---- E5 ----
    {"epic": "E5", "id": "S01", "title": "Service account + creds в data/secrets.env",
     "links": "F24", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E5", "id": "S02", "title": "Sheet probe (поиск колонки месяца)",
     "links": "F24", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E5", "id": "S03", "title": "Очередь записи + статус queued/written/failed",
     "links": "F24", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E5", "id": "S04", "title": "sheet-flush ручной и авто",
     "links": "F24", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E5", "id": "S05", "title": "Показ статуса записи в карточке (US-10)",
     "links": "US-10", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},

    # ---- E6 ----
    {"epic": "E6", "id": "P01", "title": "Форма загрузки по токену",
     "links": "F05, US-5", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E6", "id": "P02", "title": "Парсер + показ сверки (/submit/{token}/review)",
     "links": "F13, US-5", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E6", "id": "P03", "title": "Шаблон Excel для скачивания",
     "links": "F07, MVP 1.5", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E6", "id": "P04", "title": "Степпер «Загрузить → сверка → отправка»",
     "links": "US-5", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E6", "id": "P05", "title": "Контакт поддержки в форме",
     "links": "US-5", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E6", "id": "P06", "title": "6 локалей формы",
     "links": "F12", "status": "🟢 Готово в коде", "priority": "Should", "owner": "", "due": ""},

    # ---- E7 ----
    {"epic": "E7", "id": "UI-01", "title": "Единая карточка партнёра (текущий отчёт, сверка, Sheets, Altegio внутри)",
     "links": "US-1", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-02", "title": "Глобальный месяц в шапке",
     "links": "US-2", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-03", "title": "Операционный дашборд (3 блока внимания)",
     "links": "US-3", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-04", "title": "Прозрачный статус отчёта в карточке",
     "links": "US-4", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-05", "title": "Ручной ввод отчёта (один сценарий)",
     "links": "US-6", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-06", "title": "Кандидаты на отключение как видимый блок",
     "links": "US-7", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-07", "title": "Настройки по разделам (не по endpoint)",
     "links": "US-8", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-08", "title": "Архив ≠ активный список (фильтр)",
     "links": "US-9", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E7", "id": "UI-09", "title": "Связь отчёт ↔ Sheets в карточке",
     "links": "US-10", "status": "🔵 Не начата", "priority": "Could", "owner": "", "due": ""},
    {"epic": "E7", "id": "DASH-01", "title": "Overview — таблица по месяцу",
     "links": "F18", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "DASH-02", "title": "Список партнёров с быстрыми ссылками",
     "links": "F18", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E7", "id": "ACCR-01", "title": "Очередь начислений и подтверждение",
     "links": "F24", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},

    # ---- E8 ----
    {"epic": "E8", "id": "INF-01", "title": "Развёртывание в проде (Ваня, GCP/Cloud Run)",
     "links": "F32, DEPLOY.md", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-02", "title": "CI: test.yml запускает pytest",
     "links": "DEPLOY.md", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-03", "title": "CI: deploy.yml (нет в репо)",
     "links": "DEPLOY.md", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-04", "title": "/health эндпоинт",
     "links": "F28", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-05", "title": "Логирование в проде + централизованный сбор",
     "links": "F28", "status": "🔴 Блокер", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-06", "title": "Мониторинг и алерты",
     "links": "F28", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-07", "title": "Backup SQLite + uploads",
     "links": "F32", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E8", "id": "INF-08", "title": "Регулярный backup по backup_data.py",
     "links": "scripts/", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},

    # ---- E9 ----
    {"epic": "E9", "id": "SEC-01", "title": "Basic Auth + ролевая модель",
     "links": "F26", "status": "🔴 Блокер", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E9", "id": "SEC-02", "title": "HTTPS в проде (Let's Encrypt / corp-nginx)",
     "links": "DEPLOY.md", "status": "🔵 Не начата", "priority": "Must", "owner": "", "due": ""},
    {"epic": "E9", "id": "SEC-03", "title": "Retention для data/uploads и БД",
     "links": "DEP-TRANSFER", "status": "🔵 Не начата", "priority": "Should", "owner": "", "due": ""},
    {"epic": "E9", "id": "SEC-04", "title": "Защита секретов в data/secrets.env (chmod 600, не в git)",
     "links": ".gitignore", "status": "🟢 Готово в коде", "priority": "Must", "owner": "", "due": ""},
]


USER_STORIES = [
    {"id": "US-1", "title": "Единая карточка партнёра", "epic": "E7",
     "summary": "Всё о партнёре на одной странице: контакт, ставка, текущий отчёт, сверка, Sheets, Altegio, история, уведомления.",
     "acceptance": "Один URL = один партнёр. Переключение вперёд/назад по соседнему. Без возврата к списку."},
    {"id": "US-2", "title": "Глобальный месяц в шапке", "epic": "E7",
     "summary": "Один селектор месяца применяется везде, сохраняется при переходе по разделам.",
     "acceptance": "?month=YYYY-MM сохраняется при переходе по разделам."},
    {"id": "US-3", "title": "Операционный дашборд", "epic": "E7",
     "summary": "3 блока: «требует внимания» (топ-10), «готово к отправке в Sheets», «просрочено сдать».",
     "acceptance": "Клик по строке → сразу карточка партнёра за месяц (US-1)."},
    {"id": "US-4", "title": "Прозрачный статус отчёта", "epic": "E7 + E2",
     "summary": "В карточке партнёра одна строка статуса за месяц: нет файла / черновик / прислан / подтверждён / отправлен в Sheets / расхождение / риск.",
     "acceptance": "Блок «Отчёт за {месяц}» со статусом и кнопкой следующего действия."},
    {"id": "US-5", "title": "Партнёрская форма = партнёрский путь", "epic": "E6 + E1",
     "summary": "По ссылке партнёр видит степпер, объяснения расхождений, контакт поддержки.",
     "acceptance": "Степпер, человеческие объяснения, контакт."},
    {"id": "US-6", "title": "Ручной ввод — один сценарий", "epic": "E7",
     "summary": "Кнопка Enter report manually с преcетом периода и обратной связью.",
     "acceptance": "После сохранения — статус меняется (US-4)."},
    {"id": "US-7", "title": "Кандидаты на отключение как видимый сигнал", "epic": "E7 + E2",
     "summary": "Блок «Риск отключения» в карточке партнёра.",
     "acceptance": "Количество филиалов под риском + ссылка."},
    {"id": "US-8", "title": "Настройки по смыслу", "epic": "E7",
     "summary": "Левое меню с 4 разделами: Подключения, Уведомления, Валюты, Сервис.",
     "acceptance": "Единая навигация; MCP connect не спрятан."},
    {"id": "US-9", "title": "Архив ≠ список", "epic": "E7",
     "summary": "Фильтр «Активные / Архив / Все».",
     "acceptance": "Архивные — с подсказкой «последний отчёт N месяцев назад»."},
    {"id": "US-10", "title": "Связь отчёт ↔ Sheets", "epic": "E5 + E7",
     "summary": "Бейдж состояния записи (нет / в очереди / записано / ошибка) в карточке партнёра и в Overview.",
     "acceptance": "Кнопка «Открыть в Google Sheets» (если есть ссылка на ячейку)."},
]


MVP_TASKS = [
    {"id": "1.1", "title": "Напоминания + сбор через Telegram-бот", "epic": "E3",
     "summary": "Бот присылает ссылку, партнёр скидывает файл, парсер, вердикт."},
    {"id": "1.2", "title": "Напоминания + сбор через Email + загрузка", "epic": "E4",
     "summary": "Партнёр присылает отчёт вложением по почте."},
    {"id": "1.3", "title": "Интерфейс по ссылке для загрузки", "epic": "E6",
     "summary": "Веб-форма по персональной ссылке."},
    {"id": "1.4", "title": "Парсинг таблиц", "epic": "E1",
     "summary": "extract.py: Excel/CSV/PDF/текст."},
    {"id": "1.5", "title": "Отправка шаблона партнёру", "epic": "E1 + E6",
     "summary": "Шаблон Excel с преcетом приложений и обязательных колонок."},
    {"id": "1.6", "title": "Чек-лист валидности отчётов", "epic": "E1",
     "summary": "Проверка колонок и заполненных ячеек."},
    {"id": "2.1", "title": "Сопоставление ID с Би МСП", "epic": "E2",
     "summary": "Сверка Location ID из отчёта с внутренней базой."},
    {"id": "2.2", "title": "Активные партнёры через властик МСП", "epic": "E2",
     "summary": "Эластик / BI логи для определения активности."},
    {"id": "2.3", "title": "Сравнение остальных значений в отчётах", "epic": "E2 (R10)",
     "summary": "Декомпозиция: какие именно значения (оборот, revshare, возвраты, штрафы, активные клиенты)."},
    {"id": "2.4", "title": "Сводная страница по всем партнёрам", "epic": "E7 (DASH-01)",
     "summary": "Overview — таблица по месяцу."},
    {"id": "2.5", "title": "Карточки конкретных партнёров", "epic": "E7 (UI-01)",
     "summary": "Сейчас табы main/settings/history, нужно объединить."},
    {"id": "2.6", "title": "Сводка по одному партнёру", "epic": "E7 (UI-01)",
     "summary": "Явный статус жизненного цикла."},
    {"id": "2.7", "title": "Отправка в Google Таблицу", "epic": "E5",
     "summary": "sheet_sync.write + queue + flush."},
]


# ---------- styling helpers ----------

STATUS_FILL = {
    "🔵 Не начата": "DCE7F5",          # light blue
    "🟡 В работе": "FFF2C8",           # light yellow
    "🟢 Готово в коде": "D8F0D8",      # light green
    "✅ В проде": "7CBF7C",            # green
    "🔴 Блокер": "F5C6C0",             # light red
    "⚪ Не подтверждено": "E7E7E7",     # grey
}

PRIORITY_FILL = {
    "Must": "F5C6C0",
    "Should": "FFF2C8",
    "Could": "DCE7F5",
}

EPIC_FILL = "F2F2F2"


# ---------- workbook ----------

def build_workbook(path: Path) -> None:
    wb = Workbook()

    # Summary sheet
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Эпик", "Название", "Цель", "Задач", "Готово в коде", "В работе", "Блокеры", "🔵 Не начата"])
    summary.append([])

    totals = {key: 0 for key in ["total", "done", "wip", "block", "todo"]}
    for code, name, goal in EPICS:
        epic_tasks = [t for t in TASKS if t["epic"] == code]
        done = sum(1 for t in epic_tasks if t["status"] == "🟢 Готово в коде")
        wip = sum(1 for t in epic_tasks if t["status"] == "🟡 В работе")
        block = sum(1 for t in epic_tasks if t["status"] == "🔴 Блокер")
        todo = sum(1 for t in epic_tasks if t["status"] == "🔵 Не начата")
        totals["total"] += len(epic_tasks)
        totals["done"] += done
        totals["wip"] += wip
        totals["block"] += block
        totals["todo"] += todo
        summary.append([code, name, goal, len(epic_tasks), done, wip, block, todo])
        # colour the epic cell
        cell = summary.cell(row=summary.max_row, column=1)
        cell.fill = PatternFill("solid", fgColor=EPIC_FILL)
        cell.font = Font(bold=True)

    summary.append([])
    summary.append(["ИТОГО", "", "", totals["total"], totals["done"], totals["wip"], totals["block"], totals["todo"]])
    summary.cell(row=summary.max_row, column=1).font = Font(bold=True)

    # adjust column widths
    for col, width in zip("ABCDEFGH", [8, 32, 60, 8, 14, 12, 10, 12]):
        summary.column_dimensions[col].width = width

    # legend
    legend_row = summary.max_row + 2
    summary.cell(row=legend_row, column=1, value="Легенда статусов:").font = Font(bold=True)
    for i, status in enumerate(STATUS_FILL.keys(), start=2):
        cell = summary.cell(row=legend_row + i - 1, column=1, value=status)
        cell.fill = PatternFill("solid", fgColor=STATUS_FILL[status])

    legend_row2 = legend_row + len(STATUS_FILL) + 2
    summary.cell(row=legend_row2, column=1, value="Легенда приоритетов:").font = Font(bold=True)
    for i, prio in enumerate(PRIORITY_FILL.keys(), start=2):
        cell = summary.cell(row=legend_row2 + i - 1, column=1, value=prio)
        cell.fill = PatternFill("solid", fgColor=PRIORITY_FILL[prio])

    # Backlog sheet
    ws = wb.create_sheet("Backlog")
    headers = ["Epic", "ID", "Title", "Links", "Status", "Priority", "Owner", "Due", "Notes"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
        cell.alignment = Alignment(horizontal="left", vertical="center")

    for t in TASKS:
        ws.append([t["epic"], t["id"], t["title"], t["links"], t["status"], t["priority"], t["owner"], t["due"], ""])
        row = ws.max_row
        ws.cell(row=row, column=5).fill = PatternFill("solid", fgColor=STATUS_FILL.get(t["status"], "FFFFFF"))
        ws.cell(row=row, column=6).fill = PatternFill("solid", fgColor=PRIORITY_FILL.get(t["priority"], "FFFFFF"))

    # column widths
    for col, width in zip("ABCDEFGHI", [6, 8, 56, 24, 18, 10, 14, 12, 32]):
        ws.column_dimensions[col].width = width

    # data validations
    statuses = ",".join(STATUS_FILL.keys())
    priorities = ",".join(PRIORITY_FILL.keys())
    dv_status = DataValidation(type="list", formula1=f'"{statuses}"', allow_blank=True)
    dv_priority = DataValidation(type="list", formula1=f'"{priorities}"', allow_blank=True)
    ws.add_data_validation(dv_status)
    ws.add_data_validation(dv_priority)
    dv_status.add(f"E2:E{ws.max_row}")
    dv_priority.add(f"F2:F{ws.max_row}")

    # freeze header
    ws.freeze_panes = "A2"

    # filter
    ws.auto_filter.ref = f"A1:I{ws.max_row}"

    # User Stories sheet
    us = wb.create_sheet("User Stories")
    us.append(["ID", "Title", "Epic", "Summary", "Acceptance criteria"])
    for cell in us[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
    for s in USER_STORIES:
        us.append([s["id"], s["title"], s["epic"], s["summary"], s["acceptance"]])
    for col, width in zip("ABCDE", [8, 36, 8, 56, 56]):
        us.column_dimensions[col].width = width
    us.auto_filter.ref = f"A1:E{us.max_row}"

    # MVP sheet
    mv = wb.create_sheet("MVP")
    mv.append(["ID", "Title", "Epic", "Summary"])
    for cell in mv[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
    for m in MVP_TASKS:
        mv.append([m["id"], m["title"], m["epic"], m["summary"]])
    for col, width in zip("ABCD", [8, 40, 12, 64]):
        mv.column_dimensions[col].width = width
    mv.auto_filter.ref = f"A1:D{mv.max_row}"

    # Blockers sheet
    bk = wb.create_sheet("Blockers")
    bk.append(["Epic", "ID", "Title", "Links", "Owner", "Notes"])
    for cell in bk[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9D9D9")
    blocked_tasks = [t for t in TASKS if t["status"] == "🔴 Блокер"]
    for t in blocked_tasks:
        bk.append([t["epic"], t["id"], t["title"], t["links"], t["owner"], ""])
    for col, width in zip("ABCDEF", [8, 8, 56, 24, 14, 32]):
        bk.column_dimensions[col].width = width
    bk.auto_filter.ref = f"A1:F{bk.max_row}"

    wb.save(path)


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "docs" / "product-backlog.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(out)
    print(f"wrote {out}")