# [T003] Пересчитывать active_error после ERROR_SET из device_errors
- приоритет: высокий
- статус: открыто
- теги: bug, server, errors

## Описание
При `ERROR_SET` вставка в `device_errors` делает `ON CONFLICT ... DO NOTHING`, но следующий `UPDATE devices SET active_error = ?` выполняется всегда. Если активны ошибки `A` и `B` и приходит повторный `ERROR_SET A` с новым `event_id`, в `devices.active_error` окажется `A`, хотя самая свежая открытая ошибка по `occurred_at` — `B`.

Что сделать: вычислять `active_error` тем же подзапросом, что в `ERROR_CLEAR` (самая свежая запись с `cleared_at IS NULL`, иначе `NONE`). Желательно вынести подзапрос в общую функцию/константу, используемую также в `heartbeat`.

Критерий готовности: порядок и повторы `ERROR_SET` не меняют вычисленное значение `active_error`; добавлен тест.
