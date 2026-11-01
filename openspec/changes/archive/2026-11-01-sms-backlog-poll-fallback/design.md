## Context

`SmsService` (`modemmanager/sms/service.py`) is the modem-servicing component responsible for SMS intake. Today it reads message storage in two cases:

1. When the modem's servicing starts — `start()` calls `_drain_backlog()`, which issues `AT+CMGL=4`, and for each index found deletes the message and hands it off for assembly/publication.
2. On the unsolicited `+CMTI` notification (`Kind.SMS_STORED`) — `handle()` calls `_read_and_publish(index)` for that specific index.

`poll()` is called often (the modem's poll loop wakes up every 0.5–30 s depending on what's due soonest), but right now `SmsService.poll()` only checks whether the multipart-assembly timeout has expired (`_EXPIRE_CHECK_INTERVAL = 30.0`); it never touches message storage.

The `+CMTI` notification is enabled with `AT+CNMI=2,1,0,0,0` through `_safe()` (`behaviors/base.py`), so a modem's refusal of that command is swallowed silently. Worse: part of the Huawei E1550 firmware range replies `OK` to it but does not reliably send `+CMTI` afterwards — the command is accepted, but the behavior is not guaranteed. This is corroborated by the fact that the problem reproduces specifically on Huawei E1550 and not on SIM800, even though both families inherit the same notification-setup code (`GenericAtBehavior.enable_notifications`).

## Goals

- A message must not sit in modem memory longer than one backup-poll interval, even if `+CMTI` never arrived.
- The behavior is the same across all modem families — a general safeguard against missed notifications, not a patch for one specific model.
- No duplicate publication of the same message to Telegram if `+CMTI` does arrive.
- Reasonable cost: the backup poll must not add noticeable extra load on the AT session during normal operation.

## Non-Goals

- Fixing the E1550 firmware itself, or trying to coax reliable `+CMTI` out of it (switching `AT+CNMI` modes, re-initializing notifications on the fly) — we work around the defect at the application level rather than curing the modem.
- Revisiting Huawei control-port selection and probe stability (`discovery/probe.py`) — a separate problem, unrelated to this change.
- A Prometheus metric/alert for "missed notification" — a journal entry is enough for this iteration.
- Changing the storage-occupancy poll interval (`intervals.storage`, used for UI/metrics) — it serves a different purpose and can stay infrequent.

## Decisions

### Reuse `_drain_backlog` instead of writing a separate read path

`_drain_backlog()` already does exactly what's needed: `AT+CMGL=4`, then for each message found, delete and process it in the same order as for a notification. The backup poll is just the same call, repeated on an interval for as long as the modem is being serviced. A separate implementation would duplicate the `AT+CMGL` parsing and the "delete before publish" ordering that's already covered by tests.

### A dedicated `sms.backlog_poll_interval` setting, not tied to `intervals.storage`

`intervals.storage` (300 s by default) only polls the occupancy counter for status and metrics — it never reads the messages themselves. The backup poll serves a different purpose: its frequency determines how long a message can sit unread if `+CMTI` is missed, so it needs its own, shorter interval (60 s by default). The alternative — "poll `AT+CMGL=4` every time `AT+CPMS?` is read" — was rejected: it conflates two polls with different purposes and removes the ability to tune them independently.

### Race with a genuine `+CMTI`

A theoretical race: `+CMTI` queues an event on the modem's `_queue` while the backup poll is already draining the same message via `AT+CMGL=4` + `AT+CMGD`. Since `AtSession` executes AT commands strictly one at a time on a single port (`_lock`), the commands themselves never physically overlap; the only possible outcome is that the backup poll has already deleted the message by the time the queued notification gets handled. In that case `_read_and_publish` gets an error on `AT+CMGR=<index>` (the message is already gone) — already handled by existing code as a `log.warning` with no crash. Decision: leave it as is, don't suppress it — such a warning is rare and not misleading: the next occupancy poll confirms the message was processed, not lost.

### Diagnostics: distinguish a backup-poll find from normal delivery

To confirm or rule out suspicion of a specific modem, the backup poll, when it finds at least one message, logs an `log.info` entry explicitly stating "backup poll found unprocessed messages — notification was not delivered." This is purely diagnostic, not user-visible behavior, so it is not carried into the spec.

## Risks / Trade-offs

- An extra `AT+CMGL=4` on every backup-poll interval for every modem — the cost already exists at startup; here it becomes ongoing. Mitigation: a moderate default interval (60 s) that doesn't interfere with the regular signal/registration polls, which run on their own timers on the same port.
- If `AT+CMGL=4` itself fails (modem busy, transient error), the backup poll behaves exactly like `_drain_backlog` does today at startup: `except AtError: log.info(...); return` — it simply tries again on the next interval.
- Under very heavy SMS traffic, the race described above may produce a few extra journal warnings — not critical, does not affect delivery.

## Migration Plan

- Deploy: adding `sms.backlog_poll_interval: 60.0` is not required — the field is new, with a default value when absent from the settings file.
- Rollback: remove the periodic call from `poll()`, keep `_drain_backlog()` only in `start()` — behavior reverts to the current state.
- `README.md` — a paragraph on the backup poll: what it is, why it's needed, which setting controls it and its default.
