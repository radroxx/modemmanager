"""Лестница восстановления: повтор, переоткрытие, сброс, неисправность."""

from __future__ import annotations

import pytest
from fake_modem import FakeTransport

from modemmanager.at.errors import RecoveryExhausted
from modemmanager.at.recovery import RecoveryLadder, RecoveryPolicy, Step
from modemmanager.at.session import AtSession

FAST = RecoveryPolicy(
    command_retries=1, reopen_delay=0.0, reset_delay=0.0, probe_timeout=0.05
)


async def build(transport: FakeTransport, *, reset=None, policy=FAST):
    session = AtSession(transport)
    await session.open()
    initialised: list[int] = []

    async def initialise(current: AtSession) -> None:
        initialised.append(1)
        await current.execute("ATE0", timeout=1.0)

    ladder = RecoveryLadder(
        session=session,
        initialise=initialise,
        reset=reset,
        policy=policy,
    )
    return session, ladder, initialised


async def test_first_step_is_retrying_the_command():
    calls: list[str] = []
    transport = FakeTransport({"ATE0": "OK"})

    def answer_second_time(command: str):
        calls.append(command)
        return "+CSQ: 15,99" if len(calls) >= 2 else None

    transport.set_response("AT+CSQ", answer_second_time)
    session, ladder, _ = await build(transport)

    response = await ladder.execute("AT+CSQ", timeout=0.05)

    assert response.lines == ["+CSQ: 15,99"]
    assert len(calls) == 2
    # До переоткрытия дело не дошло.
    assert ladder.steps_taken == []
    assert transport.opened == 1
    await session.close()


async def test_reopen_is_used_when_retries_do_not_help():
    transport = FakeTransport({"ATE0": "OK", "AT": "OK"})
    session, ladder, initialised = await build(transport)

    def answer_after_reopen(command: str):
        return "+CSQ: 11,99" if transport.opened >= 2 else None

    transport.set_response("AT+CSQ", answer_after_reopen)

    response = await ladder.execute("AT+CSQ", timeout=0.05)

    assert response.lines == ["+CSQ: 11,99"]
    assert ladder.steps_taken == [Step.RETRY, Step.REOPEN]
    assert transport.opened == 2
    assert transport.closed == 1
    assert initialised == [1]  # инициализация повторена после переоткрытия
    assert ladder.reconnects == 1
    await session.close()


async def test_reset_is_used_when_reopen_does_not_help():
    transport = FakeTransport({"ATE0": "OK"})
    transport.silent.add("AT+CSQ")
    transport.silent.add("AT")
    reset_calls: list[str] = []

    async def reset(session: AtSession) -> None:
        reset_calls.append("reset")
        transport.silent.discard("AT")
        transport.silent.discard("AT+CSQ")
        transport.set_response("AT+CSQ", "+CSQ: 8,99")

    session, ladder, _ = await build(transport, reset=reset)

    response = await ladder.execute("AT+CSQ", timeout=0.05)

    assert response.lines == ["+CSQ: 8,99"]
    assert ladder.steps_taken == [Step.RETRY, Step.REOPEN, Step.RESET]
    assert reset_calls == ["reset"]
    await session.close()


async def test_exhausted_ladder_ends_in_fault():
    transport = FakeTransport({"ATE0": "OK"})
    transport.silent.update({"AT", "AT+CSQ"})

    async def useless_reset(session: AtSession) -> None:
        pass

    session, ladder, _ = await build(transport, reset=useless_reset)

    with pytest.raises(RecoveryExhausted) as excinfo:
        await ladder.execute("AT+CSQ", timeout=0.05)

    assert ladder.steps_taken == [Step.RETRY, Step.REOPEN, Step.RESET, Step.FAULT]
    assert excinfo.value.last_step == Step.FAULT.value
    await session.close()


async def test_no_reset_available_goes_straight_to_fault():
    transport = FakeTransport({"ATE0": "OK"})
    transport.silent.update({"AT", "AT+CSQ"})
    session, ladder, _ = await build(transport, reset=None)

    with pytest.raises(RecoveryExhausted):
        await ladder.execute("AT+CSQ", timeout=0.05)

    assert Step.RESET not in ladder.steps_taken
    assert ladder.steps_taken[-1] == Step.FAULT
    await session.close()


async def test_steps_are_reported_for_journal_and_metrics():
    reported: list[tuple[str, str]] = []
    transport = FakeTransport({"ATE0": "OK", "AT": "OK"})
    session, ladder, _ = await build(transport)
    ladder.on_step = lambda step, detail: reported.append((step.value, detail))

    def answer_after_reopen(command: str):
        return "+CSQ: 7,99" if transport.opened >= 2 else None

    transport.set_response("AT+CSQ", answer_after_reopen)
    await ladder.execute("AT+CSQ", timeout=0.05)

    assert [step for step, _ in reported] == ["retry", "reopen"]
    await session.close()


async def test_disappeared_device_is_not_ground_for_reset():
    """Исчезнувшее устройство не лечится сбросом: его просто нет."""
    transport = FakeTransport({"ATE0": "OK"})
    session, ladder, _ = await build(transport)
    transport.disappear("отключено физически")

    with pytest.raises(Exception) as excinfo:
        await ladder.execute("AT+CSQ", timeout=0.05)

    assert "исчез" in str(excinfo.value) or "открыт" in str(excinfo.value)
    assert Step.RESET not in ladder.steps_taken
    await session.close()
