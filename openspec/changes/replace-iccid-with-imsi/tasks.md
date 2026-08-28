## 1. AT-уровень: чтение IMSI

- [x] 1.1 Переименовать модуль `modemmanager/sim/iccid.py` в `modemmanager/sim/imsi.py`; убедиться, что импорты в `modemmanager/sim/__init__.py`, `modemmanager/sim/service.py` и тестах указывают на новый путь — проверка: `python -m compileall modemmanager` без ошибок.
- [x] 1.2 Реализовать `read_imsi(session) -> str` через единственную команду `AT+CIMI`; убрать константу `_ICCID_COMMANDS`, функции `read_iccid`, `_extract_iccid`, `luhn_ok`, `_country_from_iccid`, `_INDUSTRY_PREFIX`, `_COUNTRY_CODES`, `_ICCID_SAFE`, `_CCID_RESPONSE`, `_BARE_ICCID` — проверка: `grep -R "CCID\|luhn" modemmanager/` не находит совпадений.
- [x] 1.3 Реализовать парсер IMSI по D2 (первая непустая строка, 6–15 цифр, ведущие/хвостовые пробелы игнорируются) и таблицу MCC по D3 — проверка: юнит-тест на массиве «сырой ответ модема → IMSI/пустая строка» и таблице MCC→ISO2.
- [x] 1.4 Переделать `identify(session, behavior=None, *, imei="") -> SimIdentity` на IMSI: возвращаемый `SimIdentity` содержит `imsi`, `country`, `country_code`, `tail`; удалить поля `iccid`, `checksum_ok`, `suspect` — проверка: `mypy` (или `python -m compileall`) и обновлённый `tests/test_sim.py` проходят.

## 2. Модель модема

- [x] 2.1 В `modemmanager/modem.py` заменить `state.iccid: str` на `state.imsi: str`, удалить `state.iccid_suspect: bool`; обновить все места создания и сериализации состояния — проверка: `grep -R "iccid" modemmanager/` не находит совпадений.
- [x] 2.2 Обновить `SimService._identify`, `_label_for`, `_is_configured`, `_report_unknown_sim`, `_pin_value`, `_pin_configured`, `_entry_payload`, чтобы ключом везде был IMSI, и добавить ветку «пустой IMSI → SIM неопознана» по D7 — проверка: `tests/test_sim.py` покрывает и штатный, и «пустой IMSI» пути.

## 3. Хранилище настроек

- [x] 3.1 В `modemmanager/config.py` переименовать параметр `iccid` → `imsi` в `Settings.sim`, `Settings.is_configured`, `SettingsStore.update_sim`; обновить docstring `SimSettings` — проверка: `grep -R "iccid" modemmanager/config.py` пусто.
- [x] 3.2 В `Settings.from_dict` при чтении файла игнорировать содержимое ключа `sims` (сбрасывать в `{}`) и однократно писать `log.warning("настройки SIM сброшены при переходе на IMSI")` если ключ был непуст; сразу вызвать `save()` в `SettingsStore.load` (уже вызывается для пустого файла), чтобы старая секция перезаписалась пустой на диске — проверка: тест на файле с `sims: {"89...": {...}}` → после `load()` `store.settings.sims == {}` и на диске то же.

## 4. События, API, веб-интерфейс, уведомления

- [x] 4.1 В `modemmanager/events.py` и во всех вызовах `modem.event(EventType.X, {...})` (в `sim/service.py`, `modem.py`, `sms/*`, `calls.py`, `network.py`) переименовать поле `iccid` на `imsi` — проверка: `grep -R '"iccid"' modemmanager/` пусто; тесты `tests/test_events.py`, `tests/test_eventlog.py` обновлены и проходят.
- [x] 4.2 В `modemmanager/web/api.py`, `modemmanager/web/pages.py`, `modemmanager/web/server.py` и связанных шаблонах заменить `iccid` на `imsi` в путях URL (если такие есть), полях JSON и колонках таблиц; убрать индикатор «сомнительный идентификатор» — проверка: `tests/test_web.py` обновлён и проходит; ручная проверка страницы SIM показывает IMSI.
- [x] 4.3 В `modemmanager/notify/format.py`, `modemmanager/notify/router.py`, `modemmanager/notify/telegram.py` заменить `iccid` на `imsi` в шаблонах текстов и в ключах маршрутизации — проверка: `tests/test_notify.py` обновлён и проходит.
- [x] 4.4 В `modemmanager/metrics.py`, `modemmanager/modem_registry.py`, `modemmanager/eventlog.py`, `modemmanager/app.py` заменить `iccid` на `imsi` в метках, ключах и логах — проверка: `tests/test_metrics.py`, `tests/test_app.py`, `tests/test_eventlog.py` проходят.
- [x] 4.5 В `modemmanager/sms/*.py` (в частности `sms/assembly.py`, `sms/service.py`) заменить `iccid` на `imsi` там, где он попадает в события и в API — проверка: `tests/test_sms_service.py`, `tests/test_sms_assembly.py` проходят.

## 5. Документация

- [x] 5.1 Обновить `README.md`: заменить упоминания ICCID/CCID на IMSI/CIMI, поправить пример JSON настроек (`sims` ключ теперь IMSI), добавить абзац о том, что при обновлении раздел `sims` в `settings.json` очищается — проверка: `grep -i "iccid\|ccid" README.md` пусто.

## 6. Интеграционная проверка

- [x] 6.1 Прогнать полный тестовый набор `pytest` — проверка: все тесты зелёные. Результат: 371 passed.
- [x] 6.2 Запустить приложение на тестовой конфигурации `settings_test.json` с непустой секцией `sims`; убедиться, что после первого запуска раздел `sims` в файле пустой, в логе есть предупреждение о сбросе, а модем в UI отображается со своим IMSI и автоименем вида `RU-...12345` — проверка: наблюдение в веб-интерфейсе и в `events.jsonl`. **Частично**: миграция настроек проверена вживую (сброс `sims` и запись `schema_version: 2` в файл, повторный запуск идемпотентен, предупреждение в логе есть). Наблюдение модема в UI выполнить не удалось: в этом окружении нет физического модема; проверить нужно на целевом хосте.