# Runbook: что делать когда сломалось

Этот документ — для админа/дежурного, который столкнулся с проблемой в работающей системе. Не для разработки.

## Структура

- **Симптом → причина → действие.** Каждый сценарий в этом формате.
- **Не нужно читать подряд.** Ищите по симптому.

## Сначала: проверьте базовое

Прежде чем лезть в логи, проверьте три вещи:

```bash
# 1. Процессы живы?
ps aux | grep -E "revshare-(web|worker)" | grep -v grep

# 2. База на месте?
ls -la data/revshare.db

# 3. Секреты настроены?
cat data/secrets.env | grep -E "^[A-Z]" | head -20
```

Если все три ОК — идите к симптому.

## Симптомы

### «502 Bad Gateway» / сайт не открывается

**Причина:** `revshare-web` упал или не запущен.

**Действие:**

```bash
# Проверить
systemctl status revshare-web
# или
ps aux | grep revshare-web

# Посмотреть последние логи
journalctl -u revshare-web -n 50
# или
tail -50 /var/log/revshare-web.log

# Перезапустить
systemctl restart revshare-web
```

**Если не помогло:** смотрите конкретную ошибку в логах. Типичные:

- `Address already in use` — порт занят другим процессом: `lsof -i :43127`.
- `Permission denied` на `data/secrets.env` — `chmod 600 data/secrets.env`.
- `sqlite3.OperationalError: database is locked` — см. ниже.

---

### «database is locked»

**Причина:** SQLite не любит параллельные записи. Если `revshare-web` и `revshare-worker` пишут одновременно — будет lock.

**Действие:**

1. Убедитесь, что оба процесса пишут, но не в одну таблицу одновременно.
2. Проверьте `data/revshare.db-journal` или `data/revshare.db-wal` — если они висят, значит транзакция не закрылась.
3. Подождите 5–10 секунд и попробуйте снова.
4. Если не проходит — перезапустите оба процесса:

```bash
systemctl restart revshare-web revshare-worker
```

**Профилактика:** в проде используется `WAL` режим (write-ahead log). Если у вас не так — добавьте в `Store.init()`:

```python
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
```

---

### «TELEGRAM_BOT_TOKEN not set»

**Причина:** В `data/secrets.env` нет токена бота или файл не читается.

**Действие:**

```bash
# Проверить, что переменная есть
grep TELEGRAM_BOT_TOKEN data/secrets.env

# Проверить, что worker читает этот файл
python -c "from revshare.secrets import load; print(load())"
```

Если переменная есть, но не читается — проверьте права:

```bash
chmod 600 data/secrets.env
chown revshare:revshare data/secrets.env
```

---

### Telegram-бот не отвечает

**Причины:**

1. Токен неверный.
2. Webhook конфликтует с polling.
3. Worker не запущен.

**Действие:**

```bash
# 1. Проверить токен
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe" | jq .

# 2. Проверить, что worker работает
ps aux | grep revshare-worker

# 3. Проверить, не висит ли webhook
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo" | jq .
# Если есть webhook — удалите:
curl "https://bot$TELEGRAM_BOT_TOKEN/deleteWebhook"
```

---

### SMTP не отправляет (исторический раздел)

Рассылка партнёрам по email отключена. Для текущего процесса используйте Telegram; сохранённые SMTP-настройки и адреса не удаляются.

**Причины:**

1. Неправильный хост/порт/креды.
2. Брандмауэр блокирует порт.
3. Gmail блокирует «небезопасные приложения».

**Действие:**

```bash
# Проверить SMTP вручную
python -c "
from revshare.mailer import send
send(
    subject='Test',
    body_html='<p>Test</p>',
    body_text='Test',
    to=['you@example.com'],
)
"
```

**Если ошибка аутентификации:**

- Gmail → нужен App Password, не основной пароль.
- Яндекс → нужен пароль приложения в настройках.
- Корпоративная почта → уточните у IT, нужен ли отдельный SMTP-user.

---

### Google Sheets не пишет

**Причины:**

1. Service account JSON не загружен.
2. У `client_email` нет прав на таблицу.
3. ID таблицы неверный.

**Действие:**

```bash
# 1. Проверить credentials
python -c "
from revshare.secrets import load
cfg = load()
print('FILE:', cfg.get('GOOGLE_SERVICE_ACCOUNT_FILE'))
print('JSON:', 'yes' if cfg.get('GOOGLE_SERVICE_ACCOUNT_JSON') else 'no')
"

# 2. Проверить доступ к таблице
python -m revshare sheet-probe --month 2026-08

# 3. Проверить, что в sheet_writes
sqlite3 data/revshare.db \
  "SELECT id, partner_name, status, detail FROM sheet_writes ORDER BY id DESC LIMIT 10;"
```

**Типичные ошибки:**

- `403 The caller does not have permission` → дайте `client_email` права редактора на таблицу.
- `404 requested entity not found` → неверный `REVSHARE_GOOGLE_SHEET_ID`.
- `JWT verification failed` → JSON ключ повреждён или истёк.

---

### Altegio MCP не отвечает

**Причины:**

1. OAuth-токен истёк (нужно переподключить).
2. URL сменился (был `mcp.alteg.io`, стал `mcp.altegio.dev/bi-data/mcp`).
3. Сетевые проблемы.

**Действие:**

```bash
# 1. Проверить URL в настройках
python -c "
from revshare.secrets import load
cfg = load()
print('URL:', cfg.get('REVSHARE_ALTEGIO_MCP_URL'))
"

# 2. Переподключить OAuth
# Зайти в /admin/settings → Altegio → Reconnect
# Следовать инструкциям (войти через Google)

# 3. Проверить после переподключения
python -m revshare status --month 2026-08
```

---

### Файл партнёра не парсится

**Причины:**

1. Неподдерживаемый формат (например, .docx).
2. Колонки называются нестандартно.
3. Файл больше 10 МБ.

**Действие:**

1. Попросите партнёра прислать шаблон (`/submit/{token}/template.xlsx`).
2. Если формат экзотический — добавьте правило в `extract.py`.
3. Проверьте логи:

```bash
# В логах будет traceback
journalctl -u revshare-web -n 100 | grep -i "extract"
```

**Типичные случаи:**

- Колонка «Location» называется «Branch ID» → добавьте синоним в `extract.py`.
- Валюта в формате «USD$» → `extract.normalize_currency`.
- Суммы через запятую («1,234.56») → `extract.parse_number`.

---

### Отчёт показывает красный статус, хотя всё ОК

**Причина:** Сработала сверка. Не всегда это значит проблему — «красный» = «существенное расхождение», а не «доказанный обман» (цитата из README).

**Что делать:**

1. Откройте отчёт в `/reports/{id}`.
2. Посмотрите причины (`risk_score.reasons`).
3. Если причины — старый снимок Altegio → обновите снимок (`/admin/settings/altegio/sync`).
4. Если причины — реальные расхождения → свяжитесь с партнёром.

**Не надо:** сразу переклассифицировать в «зелёный» без разбора. Это ломает аудит-трейл.

---

### База разрослась, всё тормозит

**Причины:**

- Много `report_events` (лог).
- Много `bot_interactions`.
- Не запускается `VACUUM`.

**Действие:**

```bash
# 1. Посмотреть размер
ls -lh data/revshare.db

# 2. Сжать (безопасно, можно на работающей базе)
sqlite3 data/revshare.db "VACUUM;"

# 3. Почистить старые логи
sqlite3 data/revshare.db \
  "DELETE FROM bot_interactions WHERE created_at < datetime('now', '-90 days');"
sqlite3 data/revshare.db \
  "DELETE FROM report_events WHERE created_at < datetime('now', '-180 days');"
sqlite3 data/revshare.db "VACUUM;"
```

**Профилактика:** настройте cron для `VACUUM` раз в неделю.

---

### Бэкап не делается

**Проверить:**

```bash
ls -la data/backups/ 2>/dev/null
```

Если папки нет или она пустая — бэкапы не настроены. Нужно:

```bash
mkdir -p data/backups
# Добавить в crontab:
0 3 * * * sqlite3 /opt/revshare/data/revshare.db ".backup /opt/revshare/data/backups/revshare-$(date +\%F).db"
# Плюс чистка старых (оставлять 30 дней):
0 4 * * * find /opt/revshare/data/backups/ -name "*.db" -mtime +30 -delete
```

---

### Worker не обновляет снимки Altegio

**Причины:**

1. Worker не запущен.
2. Ошибка в MCP/MySQL.
3. Нет данных для активных партнёров.

**Действие:**

```bash
# 1. Проверить, что worker жив
ps aux | grep revshare-worker

# 2. Проверить логи worker'а
journalctl -u revshare-worker -n 100

# 3. Запустить синхронизацию вручную
python -m revshare.cli altegio-sync --month 2026-08
```

---

### Telegram polling дублирует сообщения

**Причина:** Запущено несколько worker'ов одновременно.

**Действие:**

```bash
# Найти все процессы
ps aux | grep revshare-worker
# Убить лишние (оставить один)
kill <PID>
```

**Профилактика:** используйте systemd, он сам следит за одним экземпляром.

---

### «404 Not Found» при открытии ссылки партнёра

**Причины:**

1. Токен устарел или был перегенерирован.
2. Партнёр архивирован (`active=0`).
3. URL скопирован с пробелом или переносом.

**Действие:**

1. Зайдите в `/admin/partners/{id}/edit`.
2. Проверьте поле `token`.
3. Если партнёр архивирован — восстановите (`POST /admin/partners/{id}/restore`).

---

### «Снимок за этот месяц уже есть, не обновляем»

**Поведение:** Система использует существующий снимок, даже если он старый.

**Где:** `web.py` → логика загрузки.

**Действие:**

1. Если нужен свежий снимок — удалите записи:

```bash
sqlite3 data/revshare.db \
  "DELETE FROM usage_snapshots WHERE period = '2026-08' AND partner_id = X;"
sqlite3 data/revshare.db \
  "DELETE FROM usage_application_snapshots WHERE period = '2026-08' AND partner_id = X;"
```

2. Затем запустите синхронизацию заново.

---

## Эксплуатационные процедуры

### Развёртывание новой версии

```bash
cd /opt/revshare
git pull
source .venv/bin/activate
pip install -e ".[dev]"

# Миграции применятся автоматически при старте.
# Проверить, что нет ошибок:
pytest

# Перезапустить
systemctl restart revshare-web
systemctl restart revshare-worker

# Проверить, что оба поднялись
systemctl status revshare-web
systemctl status revshare-worker
```

### Добавление нового партнёра

1. Зайдите в `/admin/partners` → «Создать».
2. Заполните карточку.
3. Скопируйте ссылку `/submit/{token}` и передайте партнёру.
4. Если есть Telegram — привяжите чат через `/start {token}`.

### Удаление партнёра

1. Проверьте, что нет связанных отчётов.
2. Soft delete: `/admin/partners/{id}/archive`.
3. Hard delete (только если ничего не привязано): `/admin/partners/{id}/delete`.

### Ручной запуск уведомлений

```bash
# Все партнёры за месяц
python -m revshare.cli notices-run --month 2026-08

# Конкретный партнёр
python -m revshare.cli notices-run --partner X --month 2026-08
```

### Сброс кэша шаблонов Jinja

Шаблоны Jinja кэшируются в проде. После правки шаблона — рестарт:

```bash
systemctl restart revshare-web
```

---

## Куда смотреть логи

| Что | Где |
|---|---|
| Веб-запросы | `journalctl -u revshare-web` или stdout |
| Worker (планировщик) | `journalctl -u revshare-worker` или stdout |
| Telegram-бот | внутри worker'а |
| SMTP | внутри worker'а + в почтовом ящике отправителя |
| Google Sheets | внутри worker'а + `sheet_writes.status` |
| Altegio MCP | внутри worker'а + лог OAuth в `data/mcp_token.json` |

---

## Контакты

- Продукт: Kate (Integration Hub Revshare Lead).
- Инфраструктура: канал `#dev-ai-coding`.
- Код: issues в GitHub `altegio/RevshareBD`.
- Аварии: `@oncall-revshare` (если настроен).

---

## Что НЕ делать в продакшене

- Не удалять `data/revshare.db` без бэкапа.
- Не редактировать `data/secrets.env` под нагрузкой (можно потерять токен).
- Не запускать `rm -rf data/uploads/`.
- Не применять миграции руками без тестов.
- Не включать `REVSHARE_WEB_BACKGROUND_TASKS=1` на проде при нескольких web-worker'ах — будут дубли в Telegram и очереди.
