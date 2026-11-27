# [T008] Проверка Origin в _same_origin за обратным прокси
- приоритет: высокий
- статус: открыто
- теги: bug, auth, deploy

## Описание
`_same_origin` сравнивает `Origin`/`Referer` с `request.base_url`. За прокси `base_url` может оказаться `http://127.0.0.1:8001`, и вход по HTTPS получит 403 «Origin check failed».

Что сделать: настроить `--proxy-headers`/`--forwarded-allow-ips`, либо добавить переменную окружения со списком разрешённых origin (например `MTA_ALLOWED_ORIGINS`). Описать в `.env.example` и README.

Критерий готовности: вход и выход работают за прокси с HTTPS.
