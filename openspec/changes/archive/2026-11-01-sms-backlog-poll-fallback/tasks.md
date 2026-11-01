## 1. Settings

- [x] 1.1 Add `backlog_poll_interval: float = 60.0` to `SmsSettings` (`modemmanager/config.py`). Test (`tests/test_config.py`) that `SettingsStore.load()` yields `backlog_poll_interval == 60.0` for a file without this field.
- [x] 1.2 Add `"backlog_poll_interval": 60.0` to the `sms` block in `settings_test.json` as an example; confirm `modemmanager --check-config --config settings_test.json` reports the settings as valid.

## 2. SmsService: periodic backup poll

- [x] 2.1 In `SmsService` (`modemmanager/sms/service.py`), make `_drain_backlog` return the number of messages processed (it currently returns nothing), without changing any other behavior.
- [x] 2.2 In `SmsService.poll()`, add a backup-poll timer modeled on `_next_expire`/`_EXPIRE_CHECK_INTERVAL`, using `store.settings.sms.backlog_poll_interval`; call `_drain_backlog()` once it elapses.
- [x] 2.3 If `_drain_backlog()` processes at least one message during a backup poll, log an `log.info` entry clearly distinguishable from the normal `+CMTI` path (e.g.: "backup poll found %d message(s) with no notification").
- [x] 2.4 Test (`tests/test_sms_service.py`): a fake modem stores a message without sending `+CMTI`; after advancing the clock/calling `poll()` past `backlog_poll_interval`, the message is read, deleted, and published as an `SMS` event.
- [x] 2.5 Test: with normal `+CMTI` delivery, a backup poll that fires afterward does not produce a duplicate event (storage is already empty on `AT+CMGL=4`).
- [x] 2.6 Test: while `backlog_poll_interval` has not yet elapsed since the last poll, repeated `poll()` calls do not issue `AT+CMGL=4` (no extra AT traffic).

## 3. Documentation

- [x] 3.1 In `README.md`, briefly describe the message-storage backup poll: why it exists (some modems, Huawei E1550 in particular, don't always send `+CMTI` even after a successful `AT+CNMI`), which setting controls it, and its default value.

## 4. Verification

- [x] 4.1 Run `.venv/bin/pytest` — all tests pass, including the new ones from section 2.
- [x] 4.2 (optional, on a live rig) Watch a Huawei E1550 modem after the update: `storage_used` on the status page no longer grows monotonically, and messages reach Telegram even during periods when they used to pile up.
