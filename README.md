# modemmanager

Наблюдение за USB-модемами: приём SMS, входящих вызовов, уведомления в
Telegram, страница статуса и метрики для Prometheus. Отправка сообщений и
исходящие вызовы не поддерживаются -- запрещены на уровне транспорта.

## Что оно делает

- Обнаруживает подключённые USB-модемы (семейства Huawei и SIM800 через мост
  CH340) и обслуживает их независимо друг от друга.
- Принимает SMS в двоичном режиме, собирает многочастные, немедленно освобождает
  память SIM.
- Регистрирует входящие вызовы, определяет номер вызывающего, отклоняет вызов
  (не отвечает).
- Уведомляет в Telegram: сообщения и вызовы -- в чат SIM, события оборудования
  -- в чат администратора.
- Ведёт журнал событий в `events.jsonl`, отдаёт страницу статуса и историю по
  HTTP, метрики -- на `/metrics`.

Все настройки живут в одном `settings.json`; собственного файла состояния нет.

## Требования

- Linux (наблюдается через sysfs), Python 3.11+.
- Пользователь службы должен иметь доступ на чтение и запись к `/dev/ttyUSB*`.
  Правило udev в `deploy/99-modemmanager.rules` даёт группе `modemmanager`
  права `0660` на порты соответствующих устройств.
- На хостах с системным `ModemManager` его нужно либо отключить, либо
  исключить порты из его управления. Та же udev-правило выставляет флаги
  `ID_MM_DEVICE_IGNORE`.

## Установка

```bash
sudo groupadd -f modemmanager
sudo useradd -r -g modemmanager -d /var/lib/modemmanager -s /usr/sbin/nologin modemmanager

sudo install -d -m 0755 -o modemmanager -g modemmanager /opt/modemmanager
sudo install -d -m 0700 -o modemmanager -g modemmanager /etc/modemmanager
sudo install -d -m 0755 -o modemmanager -g modemmanager /var/lib/modemmanager

sudo -u modemmanager python3 -m venv /opt/modemmanager/.venv
sudo -u modemmanager /opt/modemmanager/.venv/bin/pip install -e /path/to/source

sudo install -m 0644 deploy/99-modemmanager.rules /etc/udev/rules.d/
sudo install -m 0644 deploy/modemmanager.service /etc/systemd/system/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=usb --action=add
sudo systemctl daemon-reload
```

Заведите файл настроек:

```bash
sudoedit /etc/modemmanager/settings.json  # владелец modemmanager, права 0600
```

Обязательные поля -- см. следующий раздел. Затем:

```bash
sudo systemctl enable --now modemmanager.service
```

## Формат `settings.json`

```json
{
  "web": {
    "host": "127.0.0.1",
    "port": 8080,
    "password": "какой-нибудь длинный пароль"
  },
  "telegram": {
    "token": "1234567890:AAA...",
    "admin_chat_id": "123456789",
    "api_base": "https://api.telegram.org",
    "max_retry_delay": 300.0
  },
  "discovery": {
    "drivers": ["option1", "ch341-uart"],
    "sysfs_root": "/sys",
    "dev_root": "/dev",
    "scan_interval": 2.0,
    "gone_debounce": 5.0,
    "probe_baudrates": [115200, 9600],
    "probe_timeout": 5.0,
    "probe_max_attempts": 5,
    "probe_retry_backoff": 2.0,
    "port_baudrate": {},
    "fault_retry_interval": 60.0,
    "at_trace": false
  },
  "intervals": {
    "signal": 30.0,
    "registration": 30.0,
    "storage": 300.0,
    "no_service_alert": 900.0
  },
  "sms": {
    "assembly_timeout": 600.0
  },
  "calls": {
    "clip_wait": 2.0,
    "ring_dedup": 15.0
  },
  "sims": {
    "250010123456789": {
      "label": "Роуминг МТС",
      "msisdn": "+79990001122",
      "pin": "",
      "plmn": "",
      "chat_id": "-1001234567890"
    }
  }
}
```

Поля, без которых приложение не запускается (см. `Settings.missing_required`):

- `web.password` -- пароль веб-интерфейса. Basic-Auth, метки `/metrics` без него.
- `telegram.token` -- маркер бота.
- `telegram.admin_chat_id` -- чат для событий оборудования и SIM без чата.

Остальные поля имеют значения по умолчанию из кода. Ключ `sims` пуст на
старте; SIM появляются автоматически, о каждой ненастроенной приходит
уведомление администратору с автоименем. Ключом каждой записи `sims`
служит IMSI карты (15 цифр, читается командой `AT+CIMI`).

При обновлении со старой версии, где ключами `sims` были другие
идентификаторы SIM, весь раздел `sims` при первом запуске обнуляется:
сопоставить старые записи с новыми ключами без ручного вмешательства
нельзя. В журнале при этом появляется предупреждение
`настройки SIM сброшены при переходе на IMSI`.

`pin` в API и на страницах доступен только для записи: при чтении настроек
возвращается `pin_set: true|false`, само значение никогда не отдаётся.

## Первичная настройка

1. Заведите бота в Telegram (`@BotFather`) и получите `token`.
2. Узнайте `admin_chat_id`: `curl "https://api.telegram.org/bot<token>/getUpdates"` и
   найдите `"chat":{"id":...}` после первого сообщения боту.
3. Заполните `web.password`, `telegram.token`, `telegram.admin_chat_id`.
4. Установите права на файл настроек: `sudo chown modemmanager:modemmanager
   /etc/modemmanager/settings.json && sudo chmod 0600
   /etc/modemmanager/settings.json`.
5. Запустите службу и откройте `http://localhost:8080/`. Basic-Auth: логин
   любой, пароль -- из `web.password`.
6. Подключите модем. В админ-чат придёт уведомление о новой SIM-карте с
   автоименем; на странице `/sims` можно ей задать имя, номер, чат и,
   если требуется, PIN.

## Секреты

`settings.json` содержит `web.password`, `telegram.token` и `pin` карт открытым
текстом -- это принято осознанно (см. `openspec/changes/modem-monitor/proposal.md`,
Non-goals). Права `0600` и учётная запись `modemmanager` без домашнего каталога
-- вся защита. Секреты не появляются в журнале, метках метрик, HTTP-ответах и
уведомлениях: за это отвечает разбирающая логика (`at.guard.mask`,
`notify.format._mask_secrets`, `web.api._sim_payload`).

## Диагностика

- Логи службы: `journalctl -u modemmanager -f`.
- Метрики: `curl http://localhost:8080/metrics`.
- История: `curl -u ':<пароль>' http://localhost:8080/api/history?limit=200`.
- Проверить, что порты закреплены за нужной группой:
  `ls -l /dev/ttyUSB*`.

### Построчная трассировка AT-обмена

Когда «модем не отвечает», а нужно понять, что именно уходит в порт и что
приходит обратно, включается построчная запись обмена в журнал уровня
`DEBUG`. Для этого нужно два условия одновременно:

1. В `settings.json` поставить `discovery.at_trace: true`.
2. Запустить приложение с `--log-level=DEBUG`.

С этим сочетанием на каждую AT-команду в журнале появляются как минимум две
строки: `<port> > <command>` перед отправкой и `<port> < <line>` для каждой
ответной или незапрошенной строки. Трассируется и обмен работающего модема,
и обмен пробы (см. `discovery.probe`). Значения PIN-кодов и паролей в обоих
направлениях заменяются маской `***` через `at.guard.mask`.

Трассировка выключена по умолчанию: у Huawei с включёнными `^RSSI`/`^HCSQ`
это десятки строк в минуту на каждый модем. Если `at_trace: true` оставить,
а уровень журнала оставить `INFO`, приложение при запуске напишет одну
строку `WARNING` -- трассировочных записей всё равно не будет.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Планируемые изменения оформляются в `openspec/changes/`; сама спецификация
поведения -- в `openspec/changes/modem-monitor/specs/`.
