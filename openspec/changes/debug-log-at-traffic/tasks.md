## 1. Настройка

- [x] 1.1 Добавить `at_trace: bool = False` в `DiscoverySettings` (`modemmanager/config.py`) и проверить, что `SettingsStore.load()` для файла без этого поля даёт `at_trace == False` (`tests/test_config.py`, новый кейс).
- [x] 1.2 В `settings_test.json` добавить `"at_trace": false` в блок `discovery` как пример; убедиться, что `modemmanager --check-config --config settings_test.json` сообщает «настройки в порядке».

## 2. AtSession: маскирование входящих строк

- [x] 2.1 В `AtSession._handle_line` пропускать строку через `at.guard.mask` перед `log.debug("%s < %s", ...)` при `self.trace=True`; исходящий путь `_execute_once` уже маскируется через `_display(...)`. Проверить новым модульным тестом (`tests/test_at_session.py`), что при `trace=True` эхо строки `AT+CPIN=1234` в журнал попадает как `AT+CPIN=***`.
- [x] 2.2 Проверить тестом, что при `trace=False` `log.debug` вообще не вызывается для отправки/приёма (использовать `caplog` в `tests/test_at_session.py`).

## 3. Прокидывание флага в места создания сессий

- [x] 3.1 Добавить в `Prober.__init__` параметр `trace: bool = False`, применить его в `_ask_identity` и `_can_control` при создании `AtSession`; в `_make_prober` в `Reconciler` брать значение из `settings.discovery.at_trace`. Проверить тестом (`tests/test_discovery_probe.py`), что при `trace=True` в `caplog` есть строки трассировки для пробуемого порта.
- [x] 3.2 Пробросить `trace` из настроек в `Modem` (единый конструктор `AtSession` внутри модема) и в `at/recovery.py` (при переоткрытии сессии). Проверить тестом (`tests/test_modem.py` или подобным), что обмен обслуживающей сессии тоже трассируется при включённом флаге.

## 4. Предупреждение о согласованности с уровнем журнала

- [x] 4.1 В `Application.run()` при старте, если `settings.discovery.at_trace is True` и корневой логгер настроен строже `DEBUG`, писать одну строку `logging.WARNING`: «трассировка AT-обмена включена, но уровень журнала <LEVEL>: трассировочные записи не появятся». Проверить тестом (`tests/test_app.py`), что предупреждение появляется только при этом сочетании.

## 5. Документация

- [x] 5.1 В `README.md` добавить абзац о `discovery.at_trace`: как включить, что содержит трассировка, предупреждение об объёме, ссылка на маскирование секретов. Проверить, что упомянуты и `--log-level=DEBUG`, и `at_trace`.

## 6. Прогон

- [x] 6.1 Запустить `.venv/bin/pytest` и убедиться, что все тесты проходят (в том числе новые из шагов 1-4).
- [x] 6.2 На стенде запустить `./test.sh` с `at_trace: true` и `--log-level=DEBUG`, проверить, что в консоли видны и `<port> > AT...`, и `<port> < ...` для пробы одного модема, и что при `at_trace: false` тех же строк нет.
