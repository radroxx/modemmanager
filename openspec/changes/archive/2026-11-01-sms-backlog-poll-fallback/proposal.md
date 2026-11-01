## Why

SMS intake depends entirely on the unsolicited `+CMTI` notification: `SmsService.handle` reads and deletes a message only in response to it (`modemmanager/sms/service.py`), while the backup storage drain (`_drain_backlog`, `AT+CMGL=4`) runs only once, when the modem's servicing starts. There is no separate periodic poll of the message storage itself: only the occupancy counter (`AT+CPMS?`) is polled for the UI and metrics, and it neither reads nor publishes anything.

In practice this shows up on Huawei E1550 modems: the command `AT+CNMI=2,1,0,0,0` is sent through `_safe()` (`behaviors/base.py`), the modem replies `OK`, but part of the E1550 firmware range does not always send `+CMTI` afterwards when a new SMS arrives — a known quirk of this family. SIM800 is not affected. As a result, messages physically sit in SIM memory, occupancy grows on the status page ("12/20" and so on), but nothing is published outward (through `SmsService`, i.e. to Telegram) until the modem's servicing is restarted entirely (reconnect, recovery after a fault), which in practice happens rarely and unpredictably.

## What Changes

- `SmsService` periodically, independently of `+CMTI`, polls the message storage the same way it does at startup (`AT+CMGL=4` → read → delete → assemble → publish), so that a message for which the notification never arrived is still picked up and forwarded to Telegram within the next poll interval.
- A new setting `sms.backlog_poll_interval` (60 seconds by default) sets the period of the backup poll; it works the same way for every modem family, with no per-model special-casing.
- If the backup poll finds at least one message, a separate log entry records that the message was not delivered via the `+CMTI` notification — for diagnosing unreliable modems.

## Capabilities

### New Capabilities

*(none)*

### Modified Capabilities

- `sms-receive`: adds a requirement for periodic backup polling of message storage that does not rely solely on the `+CMTI` notification.

## Impact

- `modemmanager/sms/service.py` — `_drain_backlog` is used not only in `start()` but also periodically from `poll()` on a timer (similar to the existing assembly-expiry check `_EXPIRE_CHECK_INTERVAL`).
- `modemmanager/config.py` — new field `SmsSettings.backlog_poll_interval: float = 60.0`.
- `settings_test.json` — example setting.
- `tests/test_sms_service.py` — coverage: draining a message with no preceding `+CMTI` on the backup poll; no double processing if the message was already handled via notification; the backup poll does not fire earlier than the configured interval.
- `README.md` — a paragraph on the backup poll and its rationale (unreliable `+CMTI` delivery on some modems, Huawei E1550 in particular).
- No public API; behavior in the unaffected scenario (`+CMTI` delivered reliably) is unchanged, aside from one extra no-op `AT+CMGL=4` per interval.
