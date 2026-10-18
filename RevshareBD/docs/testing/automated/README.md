# Автоматизированные тесты — карта

Какой файл с тестами что покрывает. Связь с партнёрскими (P-XX) и админскими (A-XX) историями — в [`../../acceptance/user-stories.md`](../../acceptance/user-stories.md), с подробными сценариями — в [`../../acceptance/scenarios-partner.md`](../../acceptance/scenarios-partner.md) и [`../../acceptance/scenarios-admin.md`](../../acceptance/scenarios-admin.md).

## Как читать

- **Файл** — путь в `tests/`.
- **Покрывает** — что проверяет этот файл (коротко).
- **User story** — какие ID защищает. Пусто = вспомогательный, к историям не привязан.
- **Запустить один файл:** `pytest tests/<файл> -v`.
- **Запустить один кейс:** `pytest tests/<файл>::<test_name> -v`.

## Главные модули

| Файл | Покрывает | User story |
|---|---|---|
| `test_bot.py` | `/start` бота, привязка chat_id к партнёру, `start_payload` | P-09, P-11 |
| `test_bot_handlers.py` | приём документов ботом, callback'и, большие файлы, плохое расширение | P-02, P-05, P-10, P-13, P-16 |
| `test_bot_keyboard.py` | кнопка «Запросить шаблон» и команда `/template` | P-01 |
| `test_bot_strict_validation.py` | жёсткая валидация после парсера: ноль, пустая сумма, один клиент | P-04, P-05, P-06, P-07 |
| `test_extract.py` | разбор Excel-шаблонов: первый лист, пустые строки, 10-колоночная форма | P-02, P-08 |
| `test_ingest.py` | извлечение сумм (метки, голое число, xlsx total) | P-02 |
| `test_report_template.py` | сборка шаблона отчёта (ru/en, xlsx + csv) | P-01 |
| `test_phase2_acceptance.py` | приёмочные тесты этапа 2: период из B3, форматы месяца | P-03, P-02 |
| `test_dispatch.py` | отправка напоминаний, блокировка до открытия бота, язык партнёра | P-09 (вспом.) |
| `test_invite_telegram.py` | приглашение в Telegram, i18n-строки, без подстановки периода | P-01 (вспом.) |
| `test_telegram_invites.py` | нормализация handle, смена контакта, fresh invite | P-11 (вспом.) |
| `test_notifications.py` | даты напоминаний (выходные → понедельник), сентябрьская коллекция за август | — (служебное) |
| `test_periods.py` | расчёт reporting_period, recent_periods | — (служебное) |
| `test_timezone.py` | Ереванский часовой пояс для дат | — (служебное) |
| `test_fx.py` | курсы валют: средний за месяц, кеш до переопределения | A-12 |
| `test_altegio_sync.py` | парсинг application_id из URL Хаба, чтение card_links | — (служебное) |
| `test_sheet_partners.py` | парсинг ставки из Google Sheets (десятичная, процент, текст) | — (служебное) |
| `test_sheets.py` | поиск колонки месяца, строки партнёра, ячейки оборота | — (служебное) |

## Админка и сверка

| Файл | Покрывает | User story |
|---|---|---|
| `test_web.py` | базовые хелперы веба: нормализация id, location_ids_from_file | P-02, P-12 |
| `test_admin_drafts.py` | месяцы, единая история с файлами, скачивание, запрет удаления | A-06 |
| `test_status_simplification.py` | четыре статуса сводки (нет/сдан/вручную/подтверждён) | A-01 |
| `test_status_timestamp_regression.py` | `status_at` не двигается без причины; пересечения не портят чужие статусы | A-09, A-10, A-11 |
| `test_revalidation.py` | воркер перепроверки: end-to-end, обнаружение изменений, отмена таска | A-05 |
| `test_revalidation_status_endpoint.py` | эндпоинт `/admin/reconciliation-base/revalidation-status/{task_id}` | A-05 (вспом.) |
| `test_admin_reconciliation_base.py` | страница `/admin/reconciliation-base/{period}` (пустое/с данными) | A-05 (вспом.) |
| `test_reconciliation_base_parser.py` | разбор CSV/XLSX сверочной базы, варианты `active` | A-05 (вспом.) |
| `test_reconciliation_base_migration.py` | миграция Store.init под три новые таблицы | — (миграция) |
| `test_assess_uses_reconciliation_base.py` | `assess` читает из новой сверочной базы, не из старой | A-05, A-11 |
| `test_pending_base_state.py` | экран review с пустой/заполненной сверочной базой | A-05 (вспом.) |
| `test_progress_widget_in_template.py` | виджет прогресса в `admin_base_upload.html` | — (UI) |

## Что НЕ покрыто автотестами

Эти истории сейчас **только ручные** (или в работе):

| User story | Почему нет автотеста | Что нужно, чтобы добавить |
|---|---|---|
| A-02 фильтр по статусу | визуальный кейс в `/admin/partners?status=…` | добавить кейс в `test_web.py` с разными `?status=` |
| A-03 фильтр риска | UI-фильтр, нет API для теста | либо обернуть в `TestClient`, либо оставить ручным |
| A-04 подтверждение/отмена | визуальный кейс, требует `approve` POST | добавить кейс в `test_web.py` |
| A-07 история версий | чтение шаблона `/reports/{id}` | добавить парсинг HTML-списка версий |
| A-08 события | то же, что A-07 | то же |
| A-13 Basic Auth | требует поднять приложение с `REVSHARE_ADMIN_AUTH=1` | параметризовать фикстуру `monkeypatch.setenv` |
| P-09..P-11, P-14..P-17 | требуют живого Telegram или end-to-end через TestClient с длинным сценарием | поднять `TestClient` + замокать `telegram.api_call` |

Если какие-то из них горят в ближайших релизах — добавлять сверху вниз: сначала A-04 (часто кликаем), потом A-13 (защита).

## Как пользоваться картой

1. Упал тест — открыл таблицу, нашёл `User story`, которой он соответствует.
2. Меняешь логику user story — обновил/добавил кейс в указанном файле.
3. Запускаешь точечно: `pytest tests/test_<файл>.py -v`.

## Полезные команды

```bash
# Все тесты
pytest

# Один файл
pytest tests/test_bot_handlers.py -v

# Один кейс
pytest tests/test_status_simplification.py::test_missing_when_no_report -v

# По маркеру (если добавите @pytest.mark.story("P-02"))
pytest -m "story(P-02)"

# Только упавшие (полезно после массовых правок)
pytest --lf

# С coverage
pytest --cov=revshare --cov-report=term-missing
```
