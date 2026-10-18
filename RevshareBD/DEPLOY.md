# RevShare Reports: вывод в продакшен

Чеклист по фидбеку Миши (сентябрь 2026). Цель: не локальный прототип, а живой сервис с GitHub, CI/CD и сервером.

## Текущее состояние

- Проект: `integration-hub-revshare` (FastAPI + SQLite + Telegram + SMTP + Google Sheets)
- Локальный запуск: `revshare-web` → `http://127.0.0.1:43127`
- Git: `https://github.com/altegio/RevshareBD.git`, ветка `main`
- Данные: `data/revshare.db`, `data/uploads/`, `data/secrets.env` (не в git)
- Health check: `GET /health`

---

## Фаза 0. Перед стартом

- [ ] **Сходить к Ване** (или Панову) за консультацией по инфраструктуре
- [ ] **Занести заявку Юре** (тестирование перед выкладкой)
- [ ] Уточнить у Вани: домен/поддомен, SSL, кто апрувит PR на деплой, где лежат секреты

---

## Фаза 1. GitHub

- [x] Создать private-репозиторий в org Altegio
- [x] Перенести код на GitHub:

```bash
git remote set-url origin https://github.com/altegio/RevshareBD.git
git push -u origin main
```

- [ ] Добавить `.github/CODEOWNERS` (Kate + Ваня/ответственный за деплой)
- [ ] Включить branch protection на `main`: PR required, 1 approval
- [ ] Убедиться, что в git не попали `data/`, `.venv/`, секреты

---

## Фаза 2. CI (GitHub Actions)

- [x] Workflow `test.yml`: на каждый PR и push в main

```yaml
# .github/workflows/test.yml
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest
```

- [ ] Workflow `deploy.yml`: деплой после merge в main (схема зависит от сервера Вани)
- [ ] Секреты в GitHub Secrets: `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `REVSHARE_ADMIN_PASSWORD`, и т.д.

---

## Фаза 3. Сервер

### Минимальные требования к серверу

| Компонент | Что нужно |
|-----------|-----------|
| Python | 3.12+ |
| Процесс | systemd unit или Docker |
| Порт | 43127 (внутренний) → nginx/caddy на 443 |
| Данные | Персистентный volume: `data/revshare.db`, `data/uploads/` |
| Секреты | `data/secrets.env` на сервере, не в git |
| Cron | Плановые уведомления (бот шлёт каждый час сам, но проверить) |

Веб-процесс не должен запускать фоновые задачи. Запускайте отдельный `revshare-worker`, чтобы Telegram polling и планировщик существовали в единственном экземпляре.

### Переменные окружения (prod)

Обязательные:

```
REVSHARE_PUBLIC_URL=https://revshare.example.altegio.com
REVSHARE_ADMIN_PASSWORD=<strong password>
REVSHARE_ADMIN_AUTH=1
REVSHARE_ADMIN_USER=kate
```

Из `secrets.env.example` (по необходимости):

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
GOOGLE_SERVICE_ACCOUNT_FILE=...
SMTP_HOST=...
REVSHARE_ADMIN_TELEGRAM_USER_IDS=
ALTEGIO_SNAPSHOT_URL=   # когда появится автосинхронизация
```

### Пример systemd unit

```ini
[Unit]
Description=Integration Hub RevShare
After=network.target

[Service]
User=revshare
WorkingDirectory=/opt/revshare-reports
EnvironmentFile=/opt/revshare-reports/data/secrets.env
Environment=REVSHARE_PUBLIC_URL=https://revshare.example.altegio.com
Environment=REVSHARE_WEB_BACKGROUND_TASKS=0
ExecStart=/opt/revshare-reports/.venv/bin/revshare-web
Restart=always

[Install]
WantedBy=multi-user.target
```

Отдельный unit для фоновых задач:

```ini
[Unit]
Description=Integration Hub RevShare worker
After=network.target

[Service]
User=revshare
WorkingDirectory=/opt/revshare-reports
EnvironmentFile=/opt/revshare-reports/data/secrets.env
ExecStart=/opt/revshare-reports/.venv/bin/revshare-worker
Restart=always

[Install]
WantedBy=multi-user.target
```

### Nginx (reverse proxy)

```nginx
server {
    listen 443 ssl;
    server_name revshare.example.altegio.com;

    location / {
        proxy_pass http://127.0.0.1:43127;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Деплой-скрипт (вариант через SSH)

```bash
#!/bin/bash
set -euo pipefail
cd /opt/revshare-reports
git fetch origin main
git checkout main
git pull origin main
uv sync --locked --extra dev
uv run pytest
systemctl restart revshare
systemctl restart revshare-worker
curl -sf http://127.0.0.1:43127/health
```

---

## Фаза 4. Миграция данных с локалки

- [ ] Остановить локальный `revshare-web`
- [ ] Скопировать на сервер:
  - `data/revshare.db`
  - `data/uploads/`
  - `data/secrets.env`
  - `partners.yaml` (если менялся локально)
- [ ] В Настройках обновить `REVSHARE_PUBLIC_URL` на prod-домен
- [ ] Проверить Telegram webhook (если используется) на новый URL
- [ ] Тест: открыть форму партнёра, админку, отправку тестового уведомления

---

## Фаза 5. Приёмка

- [ ] `GET /health` → `{"status":"ok"}`
- [ ] Админка открывается по prod URL с паролем
- [ ] Персональная ссылка партнёра работает
- [ ] Telegram-бот шлёт напоминание (тестовому партнёру)
- [ ] SMTP письмо уходит (тест)
- [ ] Google Sheets: `python -m revshare sheet-probe --month YYYY-MM`
- [ ] `pytest` зелёный в CI
- [ ] PR → merge → автодеплой без ручного вмешательства

Перед включением production проверьте права на секреты (`chmod 600 data/secrets.env`) и настройте регулярный backup:

```bash
python scripts/backup_data.py --destination /var/backups/revshare
```

---

## Фаза 6. После деплоя (не блокер, но держать в голове)

По Мише, сервис должен развиваться как продукт:

1. **Автоданные:** локации, активность API, payment callback из Altegio (сейчас вручную)
2. **AI-бот:** вместо шаблонных напоминаний, контекстная коммуникация с партнёрами
3. **Онбординг интеграторов:** чат-бот для клиентов, которые делают решение «для себя»
4. **Консолидация данных:** единое место для аналитики (Power BI / Superset / другое, решение за инфраструктурой)

---

## Контакты и ресурсы

- **Ваня:** сервер, деплой, чат AIDevCoder, вебинары для вайб-кодеров
- **Панов:** альтернатива по инфраструктуре
- **Юра:** заявка на тестирование перед prod
- **Андрей Моргунов:** единое хранилище данных (на будущее)

---

## Черновик сообщения Ване

> Ваня, привет!
>
> Я сделала сервис для автоматизации сбора RevShare-отчётов от партнёров Хаба Интеграций: бот рассылает ссылки 1-го числа, партнёры загружают отчёт, система сверяет Location ID и revshare с данными Altegio, есть дашборд и запись в Google Sheets.
>
> Сейчас это крутится локально, Миша сказал, что так нельзя: нужен нормальный продакшен. Он рекомендовал обратиться к тебе, у тебя уже размещены проекты Данилы и Стаса.
>
> Стек: Python 3.12, FastAPI, SQLite, Telegram bot, SMTP, Google Sheets API. Есть `/health`, данные в `data/` (БД + uploads + secrets).
>
> Можешь подсказать:
> 1. Куда лучше развернуть (твой сервер / Docker / другое)?
> 2. Как у вас устроен CI/CD (GitHub → deploy)?
> 3. Домен/SSL: что нужно с нашей стороны?
> 4. Как правильно хранить секреты?
>
> Репозиторий готов перенести на GitHub. Могу созвониться или написать в AIDevCoder, как удобнее.
>
> Спасибо!

---

## Черновик заявки Юре

> Юра, привет!
>
> Готовлю к выводу в продакшен сервис сбора RevShare-отчётов партнёров Хаба Интеграций (Integration Hub revshare). Автоматизирует ежемесячный сбор отчётов, сверку Location ID и revshare, уведомления партнёрам через Telegram/email, запись в Google Sheets Revenue Accrual.
>
> Сейчас прототип работает локально. План: GitHub + CI/CD + сервер (консультация с Ваней по инфраструктуре).
>
> Нужно ли что-то с вашей стороны перед выкладкой (тестирование, security review, регистрация сервisa)? Подскажи процесс и сроки.
>
> Спасибо!
