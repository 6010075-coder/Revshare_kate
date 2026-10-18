# Документация Integration Hub Revshare

Здесь — техническая документация проекта для разработчиков и админов.

## С чего начать

| Если вы... | Читайте |
|---|---|
| Впервые видите проект | [architecture.md](./architecture.md) |
| Поднимаете локально | [docs/development/dev-setup.md](./development/dev-setup.md) |
| Разбираетесь с БД | [docs/reference/database.md](./reference/database.md) |
| Делаете HTTP-ручку | [docs/reference/api.md](./reference/api.md) |
| Чините прод | [docs/operations/runbook.md](./operations/runbook.md) |

## Структура

```
docs/
├── README.md                   # этот файл — навигация
├── architecture.md             # как устроен проект
│
├── development/
│   └── dev-setup.md            # как поднять локально
│
├── reference/
│   ├── database.md             # схема БД, миграции
│   └── api.md                  # HTTP-ручки FastAPI
│
└── operations/
    └── runbook.md              # что делать когда сломалось
```

## Другие документы в проекте

Технические:

- [`../README.md`](../README.md) — верхнеуровневое описание для всех.
- [`../PROJECT_STATUS.md`](../PROJECT_STATUS.md) — аудит 32 функций (4.10.2026).
- [`../PROJECT_SNAPSHOT.md`](../PROJECT_SNAPSHOT.md) — снимок состояния проекта (17.10.2026).
- [`../DEPLOY.md`](../DEPLOY.md) — чек-лист вывода в прод.

Документы по конкретным эпикам:

- [`./product-backlog.md`](./product-backlog.md) — 9 эпиков, 61 задача.
- [`./epic-reconciliation.md`](./epic-reconciliation.md) — эпик сверки.
- [`./partner-edit-redesign.md`](./partner-edit-redesign.md) — план редизайна partner_edit/settings.
- [`./revshare-bpmn/README.md`](./revshare-bpmn/README.md) — BPMN-модель готовности.
- [`./TRANSFER_TO_ANOTHER_ENVIRONMENT.md`](./TRANSFER_TO_ANOTHER_ENVIRONMENT.md) — процедура переноса.

## Конвенции написания документации

- Пишем по-русски, без жаргона где возможно.
- Код, имена файлов, команд — оставляем как есть.
- Таблицы используем только когда они действительно помогают.
- Каждый документ начинается с одного абзаца «что это и для кого».
- Если документ ссылается на код — указываем файл и строку.