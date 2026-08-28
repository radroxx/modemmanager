"""Сессия AT: очередь команд, разбор ответов, незапрошенные сообщения, таймауты."""

from __future__ import annotations

import asyncio
import logging

import pytest

from modemmanager.at.errors import (
    CommandError,
    CommandTimeout,
    ForbiddenCommand,
    PortGone,
)
from modemmanager.at.guard import MASK
from modemmanager.at.session import AtSession, expected_prefix

from fake_modem import FakeTransport


async def open_session(transport: FakeTransport, **kwargs) -> AtSession:
    session = AtSession(transport, **kwargs)
    await session.open()
    return session


# --------------------------------------------------------------- одна команда

async def test_multiline_response_returns_all_data_lines():
    transport = FakeTransport({"AT+CPMS?": '+CPMS: "SM",5,20,"SM",5,20'})
    session = await open_session(transport)

    response = await session.execute("AT+CPMS?")

    assert response.lines == ['+CPMS: "SM",5,20,"SM",5,20']
    assert response.final == "OK"
    await session.close()


async def test_error_response_raises_with_code():
    transport = FakeTransport({"AT+CPIN?": "+CME ERROR: 10"})
    session = await open_session(transport)

    with pytest.raises(CommandError) as excinfo:
        await session.execute("AT+CPIN?")

    assert excinfo.value.code == 10
    assert excinfo.value.kind == "CME"
    assert session.error_count == 1
    await session.close()


async def test_plain_error_has_no_code():
    transport = FakeTransport({"AT^SYSCFG?": "ERROR"})
    session = await open_session(transport)

    with pytest.raises(CommandError) as excinfo:
        await session.execute("AT^SYSCFG?")

    assert excinfo.value.final == "ERROR"
    assert excinfo.value.code is None
    await session.close()


async def test_textual_error_code_does_not_break_parsing():
    """При AT+CMEE=2 модем отвечает текстом вместо номера."""
    transport = FakeTransport({"AT+CPIN?": "+CME ERROR: SIM not inserted"})
    session = await open_session(transport)

    with pytest.raises(CommandError) as excinfo:
        await session.execute("AT+CPIN?")

    assert excinfo.value.code is None
    assert excinfo.value.kind == "CME"
    await session.close()


# ------------------------------------------------------------- очередь команд

async def test_two_concurrent_commands_do_not_mix_answers():
    transport = FakeTransport(
        {"AT+CSQ": "+CSQ: 17,99", "AT+CMGR=1": ['+CMGR: 0,,20', "07911234"]},
        delay=0.01,
    )
    session = await open_session(transport)

    signal, message = await asyncio.gather(
        session.execute("AT+CSQ"),
        session.execute("AT+CMGR=1"),
    )

    assert signal.lines == ["+CSQ: 17,99"]
    assert message.lines == ["+CMGR: 0,,20", "07911234"]
    await session.close()


async def test_commands_run_in_arrival_order():
    transport = FakeTransport(
        {"AT+A": "+A: 1", "AT+B": "+B: 2", "AT+C": "+C: 3"},
        delay=0.005,
    )
    session = await open_session(transport)

    await asyncio.gather(
        session.execute("AT+A"),
        session.execute("AT+B"),
        session.execute("AT+C"),
    )

    assert transport.commands == ["AT+A", "AT+B", "AT+C"]
    await session.close()


# --------------------------------------------------- незапрошенные сообщения

async def test_unsolicited_during_command_does_not_pollute_response():
    events: list[str] = []
    transport = FakeTransport(delay=0.01)

    def slow_reply(command: str):
        transport.queue_unsolicited("RING", "+CMTI: \"SM\",3")
        return "+CSQ: 20,99"

    transport.set_response("AT+CSQ", slow_reply)
    session = await open_session(transport, on_unsolicited=events.append)

    response = await session.execute("AT+CSQ")

    assert response.lines == ["+CSQ: 20,99"]
    assert events == ["RING", '+CMTI: "SM",3']
    await session.close()


async def test_unsolicited_while_idle_is_dispatched():
    events: list[str] = []
    transport = FakeTransport()
    session = await open_session(transport, on_unsolicited=events.append)

    transport.queue_unsolicited("RING")
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0)

    assert events == ["RING"]
    await session.close()


async def test_own_response_wins_over_same_named_event():
    """На AT+CREG? модем отвечает строкой +CREG:, которую он же присылает сам."""
    events: list[str] = []
    transport = FakeTransport({"AT+CREG?": "+CREG: 0,1"})
    session = await open_session(transport, on_unsolicited=events.append)

    response = await session.execute("AT+CREG?")

    assert response.lines == ["+CREG: 0,1"]
    assert events == []
    await session.close()


async def test_family_prefixes_are_recognised():
    events: list[str] = []
    transport = FakeTransport()
    session = await open_session(
        transport, on_unsolicited=events.append, unsolicited_prefixes=("^RSSI:", "^MODE:")
    )

    transport.queue_unsolicited("^RSSI:19", "^MODE:5,4")
    for _ in range(100):
        if len(events) == 2:
            break
        await asyncio.sleep(0)

    assert events == ["^RSSI:19", "^MODE:5,4"]
    assert session.unknown_lines == []
    await session.close()


async def test_unknown_line_is_recorded_and_does_not_break_session():
    transport = FakeTransport({"AT": "OK"})
    session = await open_session(transport, on_unsolicited=lambda line: None)

    transport.queue_unsolicited("^SOMETHING WEIRD: 42")
    for _ in range(100):
        if session.unknown_lines:
            break
        await asyncio.sleep(0)

    assert session.unknown_lines == ["^SOMETHING WEIRD: 42"]
    assert (await session.execute("AT")).final == "OK"
    await session.close()


async def test_unsolicited_handler_can_claim_following_lines():
    """PDU после +CMT: -- продолжение события, а не строка без контекста."""
    events: list[str] = []
    transport = FakeTransport()

    def handler(line: str):
        events.append(line)
        return 1 if line.startswith("+CMT:") else 0

    session = await open_session(transport, on_unsolicited=handler)
    transport.queue_raw("\r\n+CMT: ,20\r\n07911234567890\r\n")
    for _ in range(100):
        if len(events) == 2:
            break
        await asyncio.sleep(0)

    assert events == ["+CMT: ,20", "07911234567890"]
    assert session.unknown_lines == []
    await session.close()


async def test_failing_unsolicited_handler_does_not_kill_session():
    transport = FakeTransport({"AT": "OK"})

    def broken(line: str):
        raise RuntimeError("разбор не удался")

    session = await open_session(transport, on_unsolicited=broken)
    transport.queue_unsolicited("RING")
    await asyncio.sleep(0)

    assert (await session.execute("AT")).final == "OK"
    await session.close()


# ------------------------------------------------------------------- таймауты

async def test_timeout_frees_the_port_for_the_next_command():
    transport = FakeTransport({"AT+CSQ": "+CSQ: 12,99"})
    transport.silent.add("AT+COPS=?")
    session = await open_session(transport)

    with pytest.raises(CommandTimeout):
        await session.execute("AT+COPS=?", timeout=0.05)

    assert session.error_count == 1
    response = await session.execute("AT+CSQ", timeout=1.0)
    assert response.lines == ["+CSQ: 12,99"]
    await session.close()


async def test_long_command_gets_its_own_timeout():
    transport = FakeTransport({"AT+COPS=?": "+COPS: (2,\"MTS\",\"MTS\",\"25001\")"}, delay=0.2)
    session = await open_session(transport)

    with pytest.raises(CommandTimeout):
        await session.execute("AT+COPS=?", timeout=0.05)
    response = await session.execute("AT+COPS=?", timeout=5.0)

    assert response.lines == ['+COPS: (2,"MTS","MTS","25001")']
    await session.close()


async def test_retries_are_applied_before_failing():
    calls: list[str] = []
    transport = FakeTransport()

    def answer_third_time(command: str):
        calls.append(command)
        return "+CSQ: 9,99" if len(calls) >= 3 else None

    transport.set_response("AT+CSQ", answer_third_time)
    session = await open_session(transport)

    response = await session.execute("AT+CSQ", timeout=0.05, retries=2)

    assert len(calls) == 3
    assert response.lines == ["+CSQ: 9,99"]
    await session.close()


# ------------------------------------------------------------ запрет и секреты

FORBIDDEN = [
    'AT+CMGS=20',
    "AT+CMSS=1",
    "ATD+79990001122;",
    "atd*100#",
    "AT+CGDATA=\"PPP\",1",
    "AT+CGACT=1,1",
    "ATA",
    "AT+CUSD=1,\"*100#\"",
]


@pytest.mark.parametrize("command", FORBIDDEN)
async def test_forbidden_command_never_reaches_the_port(command):
    transport = FakeTransport()
    session = await open_session(transport)

    with pytest.raises(ForbiddenCommand):
        await session.execute(command)

    assert transport.written == []
    assert transport.commands == []
    assert session.forbidden_attempts == 1
    await session.close()


async def test_hangup_is_allowed():
    transport = FakeTransport({"ATH": "OK", "AT+CHUP": "OK"})
    session = await open_session(transport)

    assert (await session.execute("ATH")).final == "OK"
    assert (await session.execute("AT+CHUP")).final == "OK"
    assert transport.commands == ["ATH", "AT+CHUP"]
    await session.close()


async def test_hangup_after_caller_dropped_reports_no_carrier():
    transport = FakeTransport({"ATH": "NO CARRIER"})
    session = await open_session(transport)

    with pytest.raises(CommandError) as excinfo:
        await session.execute("ATH")

    assert excinfo.value.final == "NO CARRIER"
    await session.close()


async def test_transport_rejects_forbidden_bytes_directly():
    """Защита стоит и на самом транспорте, не только в сессии."""
    from modemmanager.at.transport import SerialTransport

    serial = SerialTransport("/dev/ttyUSB0")

    class Writer:
        def write(self, data):
            raise AssertionError("байты не должны уйти в порт")

        async def drain(self):
            pass

    serial._writer = Writer()
    with pytest.raises(ForbiddenCommand):
        await serial.write(b"AT+CMGS=20\r")


async def test_pin_value_is_masked_in_trace(caplog):
    transport = FakeTransport({'AT+CPIN="4321"': "OK"})
    session = await open_session(transport, trace=True)

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        await session.execute('AT+CPIN="4321"', secret=True)

    diagnostics = "\n".join(record.getMessage() for record in caplog.records)
    assert "4321" not in diagnostics
    assert MASK in diagnostics
    # В порт при этом ушло настоящее значение.
    assert b'AT+CPIN="4321"\r' in transport.written
    await session.close()


async def test_pin_value_is_masked_in_echo_when_trace_is_on(caplog):
    """Эхо команды с PIN приходит из порта -- в трассировке оно тоже под маской."""
    # Порт эхоит команду до OK, чтобы попасть в путь входящего логирования.
    transport = FakeTransport({'AT+CPIN="4321"': "OK"}, echo=True)
    session = await open_session(transport, trace=True)

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        await session.execute('AT+CPIN="4321"', secret=True)

    incoming = [
        record.getMessage()
        for record in caplog.records
        if "< " in record.getMessage()
    ]
    assert incoming, "нет ни одной входящей строки в трассировке"
    joined = "\n".join(incoming)
    assert "4321" not in joined
    assert MASK in joined
    await session.close()


async def test_trace_off_produces_no_line_logs(caplog):
    """Без явного включения трассировки построчных записей обмена нет."""
    transport = FakeTransport({"AT": "OK", "AT+CPMS?": "+CPMS: \"SM\",1,20"})
    session = await open_session(transport, trace=False)

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        await session.execute("AT")
        await session.execute("AT+CPMS?")

    line_traces = [
        record.getMessage()
        for record in caplog.records
        if " > " in record.getMessage() or " < " in record.getMessage()
    ]
    assert line_traces == []
    await session.close()


async def test_pin_value_is_masked_in_command_error():
    transport = FakeTransport({'AT+CPIN="4321"': "+CME ERROR: 16"})
    session = await open_session(transport)

    with pytest.raises(CommandError) as excinfo:
        await session.execute('AT+CPIN="4321"', secret=True)

    assert "4321" not in str(excinfo.value)
    assert "4321" not in excinfo.value.command
    await session.close()


async def test_pin_value_is_masked_in_timeout_error():
    transport = FakeTransport()
    transport.silent.add('AT+CPIN="4321"')
    session = await open_session(transport)

    with pytest.raises(CommandTimeout) as excinfo:
        await session.execute('AT+CPIN="4321"', secret=True, timeout=0.05)

    assert "4321" not in str(excinfo.value)
    await session.close()


# --------------------------------------------------------- исчезновение порта

async def test_device_disappearance_ends_the_session():
    gone: list[PortGone] = []
    transport = FakeTransport()
    session = await open_session(transport, on_gone=gone.append)

    transport.disappear("устройство отключено")
    for _ in range(100):
        if gone:
            break
        await asyncio.sleep(0)

    assert len(gone) == 1
    assert not session.alive
    with pytest.raises(PortGone):
        await session.execute("AT")
    await session.close()


async def test_pending_command_fails_when_device_disappears():
    transport = FakeTransport()
    transport.silent.add("AT+CSQ")
    session = await open_session(transport)

    async def pull_the_plug():
        await asyncio.sleep(0.01)
        transport.disappear()

    asyncio.create_task(pull_the_plug())
    with pytest.raises(PortGone):
        await session.execute("AT+CSQ", timeout=5.0)

    await session.close()


async def test_echo_is_disabled_on_initialise():
    transport = FakeTransport({"ATE0": "OK", "AT+CMEE=1": "OK"}, echo=True)
    session = await open_session(transport)

    await session.initialise()

    assert transport.commands == ["ATE0", "AT+CMEE=1"]
    await session.close()


async def test_initialise_tolerates_missing_error_codes():
    transport = FakeTransport({"ATE0": "OK", "AT+CMEE=1": "ERROR"})
    session = await open_session(transport)

    await session.initialise()  # не падает: старые прошивки не знают AT+CMEE

    assert transport.commands == ["ATE0", "AT+CMEE=1"]
    await session.close()


async def test_echoed_command_is_not_part_of_the_result():
    transport = FakeTransport({"AT+CSQ": "+CSQ: 14,99"}, echo=True)
    session = await open_session(transport)

    response = await session.execute("AT+CSQ")

    assert response.lines == ["+CSQ: 14,99"]
    await session.close()


# ----------------------------------------------------------------- вспомогательное

def test_expected_prefix_extraction():
    assert expected_prefix("AT+CREG?") == "+CREG:"
    assert expected_prefix("AT^CPIN?") == "^CPIN:"
    assert expected_prefix("AT+CMGR=1") == "+CMGR:"
    assert expected_prefix("ATE0") == ""
    assert expected_prefix("AT") == ""


def test_response_helpers():
    from modemmanager.at.session import Response

    response = Response(command="AT+CPMS?", lines=["+CPMS: 1,20", "+CPMS: ignored"])

    assert response.first("+CPMS:") == "1,20"
    assert response.first("+CSQ:") is None
    assert response.text.splitlines()[0] == "+CPMS: 1,20"
