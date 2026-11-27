# [T013] Вынести расчёт ONLINE/OFFLINE в общую функцию
- приоритет: низкий
- статус: открыто
- теги: refactor, server

## Описание
Порог 30 секунд и расчёт `seconds_since_last_seen`/`communication` продублированы в `get_device` и `monitor_device`.

Что сделать: добавить константу `ONLINE_THRESHOLD_SECONDS` и функцию `describe_communication(last_seen)`, использовать в обоих местах.

Критерий готовности: дублирование убрано, ответы API не изменились.
