"""Поведение семейств модемов: опрос, разбор, остаток попыток, сброс, реестр."""

from __future__ import annotations

import pytest
from fake_modem import FakeTransport

from modemmanager import behaviors
from modemmanager.at.session import AtSession
from modemmanager.behaviors import (
    GenericAtBehavior,
    HuaweiBehavior,
    Kind,
    Sim800Behavior,
)
from modemmanager.behaviors.huawei import parse_huawei_cpin
from modemmanager.behaviors.sim800 import parse_spic
from modemmanager.values import Identity, RegistrationState, SimState

# Ответы, снятые с реальных модемов (сокращённые до значимых строк).
HUAWEI_ATI = (
    "Manufacturer: huawei",
    "Model: E3372",
    "Revision: 22.323.01.00.00",
    "IMEI: 861234567890123",
    "+GCAP: +CGSM,+DS,+ES",
)
SIM800_ATI = ("SIM800 R14.18",)


async def session_for(responses: dict[str, object], **kwargs) -> AtSession:
    transport = FakeTransport(responses, **kwargs)
    session = AtSession(transport)
    await session.open()
    return session


# ----------------------------------------------------------------- опознание

def test_huawei_is_recognised_by_its_answer():
    identity = Identity(manufacturer="huawei", model="E3372", raw="\n".join(HUAWEI_ATI))

    assert HuaweiBehavior.matches(identity) is True
    assert Sim800Behavior.matches(identity) is False


def test_sim800_is_recognised_behind_a_generic_bridge():
    """Идентификаторы USB принадлежат мосту, опознание идёт по ответу модуля."""
    identity = Identity(manufacturer="SIM800 R14.18", raw="SIM800 R14.18")
    # Пара идентификаторов моста CH341 -- заведомо не SIMCom.
    bridge_hint = "1a86:7523"

    behaviour = behaviors.select(identity, hint=bridge_hint)

    assert behaviour.family == "sim800"


def test_unrecognised_modem_gets_generic_behaviour():
    behaviour = behaviors.select(Identity(manufacturer="Quectel", model="EC25"))

    assert behaviour.family == "generic"
    assert isinstance(behaviour, GenericAtBehavior)


def test_registry_lists_all_families():
    assert behaviors.families() == ("huawei", "sim800", "generic")
    assert behaviors.by_family("huawei") is HuaweiBehavior
    assert behaviors.by_family("нет такого") is None


def test_registering_a_family_does_not_touch_other_parts():
    class FictionalBehavior(GenericAtBehavior):
        family = "fictional"

        @classmethod
        def matches(cls, identity, *, hint=""):
            return "FICTION" in identity.raw.upper()

    behaviors.register(FictionalBehavior)
    try:
        chosen = behaviors.select(Identity(raw="FICTION MODEM 1"))
        assert chosen.family == "fictional"
    finally:
        behaviors.registered()  # реестр остаётся списком типов
        behaviors._REGISTRY.remove(FictionalBehavior)


def test_broken_family_does_not_break_selection():
    class BrokenBehavior(GenericAtBehavior):
        family = "broken"

        @classmethod
        def matches(cls, identity, *, hint=""):
            raise RuntimeError("опознание сломалось")

    behaviors.register(BrokenBehavior)
    try:
        assert behaviors.select(Identity(manufacturer="huawei")).family == "huawei"
    finally:
        behaviors._REGISTRY.remove(BrokenBehavior)


# -------------------------------------------------------------- общий опрос

async def test_generic_reads_signal_registration_operator_storage():
    session = await session_for(
        {
            "AT+CSQ": "+CSQ: 17,99",
            "AT+CREG?": "+CREG: 1,5,\"2B1A\",\"1F2C3D\"",
            "AT+CGREG?": "+CGREG: 1,5",
            "AT+COPS=3,2": "OK",
            "AT+COPS=3,0": "OK",
            "AT+COPS?": "+COPS: 0,2,\"25002\",7",
            "AT+CPMS?": '+CPMS: "SM",7,20,"SM",7,20,"SM",7,20',
        }
    )
    behaviour = GenericAtBehavior()

    signal = await behaviour.read_signal(session)
    registration = await behaviour.read_registration(session)
    storage = await behaviour.read_storage(session)

    assert signal.dbm == -79
    assert registration.voice is RegistrationState.ROAMING
    assert registration.data is RegistrationState.ROAMING
    assert registration.roaming is True
    assert registration.area == "2B1A"
    assert storage.used == 7 and storage.total == 20 and storage.name == "SM"
    await session.close()


async def test_generic_reads_operator_in_both_formats():
    answers = {"format": 2}

    def cops_query(command: str):
        return (
            '+COPS: 1,2,"25002",7'
            if answers["format"] == 2
            else '+COPS: 1,0,"MegaFon",7'
        )

    def set_format(command: str):
        answers["format"] = int(command.rsplit(",", 1)[1])
        return "OK"

    transport = FakeTransport({"AT+COPS?": cops_query})
    transport.set_response("AT+COPS=3,2", set_format)
    transport.set_response("AT+COPS=3,0", set_format)
    session = AtSession(transport)
    await session.open()

    operator = await GenericAtBehavior().read_operator(session)

    assert operator.plmn == "25002"
    assert operator.name == "MegaFon"
    assert operator.manual is True
    assert operator.label == "MegaFon (25002)"
    await session.close()


async def test_unknown_signal_when_modem_refuses():
    session = await session_for({"AT+CSQ": "+CME ERROR: 4"})

    signal = await GenericAtBehavior().read_signal(session)

    assert signal.known is False
    await session.close()


async def test_unknown_signal_when_modem_says_unknown():
    session = await session_for({"AT+CSQ": "+CSQ: 99,99"})

    signal = await GenericAtBehavior().read_signal(session)

    assert signal.known is False
    assert signal.raw == 99
    await session.close()


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        ("+CPIN: READY", SimState.READY),
        ("+CPIN: SIM PIN", SimState.PIN_REQUIRED),
        ("+CPIN: SIM PUK", SimState.PUK_REQUIRED),
        ("+CPIN: SIM PIN2", SimState.PIN_REQUIRED),
        ("+CME ERROR: 10", SimState.ABSENT),
        ("+CME ERROR: 13", SimState.ABSENT),
        ("+CME ERROR: 11", SimState.PIN_REQUIRED),
        ("+CME ERROR: 12", SimState.PUK_REQUIRED),
        ("+CME ERROR: 4", SimState.UNKNOWN),
    ],
)
async def test_sim_states_are_recognised(answer, state):
    session = await session_for({"AT+CPIN?": answer})

    detected, raw = await GenericAtBehavior().read_sim_state(session)

    assert detected is state
    assert raw
    await session.close()


async def test_generic_pin_attempts_unknown_when_command_refused():
    session = await session_for({'AT+CPINR="SIM PIN"': "ERROR"})

    attempts = await GenericAtBehavior().read_pin_attempts(session)

    assert attempts.known is False
    assert "AT+CPINR" in attempts.reason
    await session.close()


async def test_generic_pin_attempts_from_cpinr():
    session = await session_for({'AT+CPINR="SIM PIN"': '+CPINR: "SIM PIN",3,3'})

    attempts = await GenericAtBehavior().read_pin_attempts(session)

    assert attempts.pin == 3
    assert attempts.source == "AT+CPINR"
    await session.close()


# ------------------------------------------------------------------- Huawei

def test_huawei_prefers_the_second_port():
    ports = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]

    order = HuaweiBehavior().rank_ports(ports)

    assert order == ["/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyUSB0"]


def test_huawei_port_order_keeps_every_port():
    ports = ["/dev/ttyUSB3", "/dev/ttyUSB4"]

    order = HuaweiBehavior().rank_ports(ports)

    assert sorted(order) == sorted(ports)


@pytest.mark.parametrize(
    ("answer", "pin", "puk"),
    [
        ("^CPIN: SIM PIN,3,10,3,10,3", 3, 10),
        ("^CPIN: READY,-1,10,3,10,3", 3, 10),
        ("^CPIN: SIM PIN,2", 2, None),
    ],
)
def test_huawei_pin_attempts_parsing(answer, pin, puk):
    attempts = parse_huawei_cpin(answer)

    assert attempts.pin == pin
    assert attempts.puk == puk
    assert attempts.source == "AT^CPIN?"


def test_huawei_pin_attempts_unparseable_is_unknown():
    attempts = parse_huawei_cpin("^CPIN: SIM PIN")

    assert attempts.known is False
    assert attempts.reason


async def test_huawei_falls_back_to_standard_command():
    session = await session_for(
        {
            "AT^CPIN?": "^CPIN: SIM PIN",
            'AT+CPINR="SIM PIN"': '+CPINR: "SIM PIN",3,3',
        }
    )

    attempts = await HuaweiBehavior().read_pin_attempts(session)

    assert attempts.pin == 3
    assert attempts.source == "AT+CPINR"
    await session.close()


async def test_huawei_reports_unknown_when_nothing_parses():
    session = await session_for({"AT^CPIN?": "^CPIN: SIM PIN", 'AT+CPINR="SIM PIN"': "ERROR"})

    attempts = await HuaweiBehavior().read_pin_attempts(session)

    assert attempts.known is False
    assert "AT^CPIN?" in attempts.reason
    await session.close()


def test_huawei_classifies_its_own_messages():
    behaviour = HuaweiBehavior()

    assert behaviour.classify("^RSSI:19").kind == Kind.SIGNAL
    assert behaviour.classify("^RSSI:19").data["rssi"] == 19
    assert behaviour.classify("^MODE:5,4").kind == Kind.NETWORK_MODE
    assert behaviour.classify("^BOOT:12345,0,0,0,68").kind == Kind.BOOT
    assert behaviour.classify("^SIMST:255").data["state"] == SimState.ABSENT.value
    assert behaviour.classify("^SIMST:1").data["state"] == SimState.READY.value
    assert behaviour.classify("^SMMEMFULL:\"SM\"").kind == Kind.STORAGE_FULL
    assert behaviour.classify("^CEND:,10,0").kind == Kind.CALL_ENDED
    # Общие сообщения по-прежнему распознаются.
    assert behaviour.classify("RING").kind == Kind.RING
    assert behaviour.classify('+CMTI: "SM",3').data["index"] == 3


async def test_huawei_reset_uses_its_own_command():
    session = await session_for({"AT^RESET": "OK"})

    await HuaweiBehavior().reset(session)

    assert session.transport.commands == ["AT^RESET"]
    assert HuaweiBehavior.supports_reset is True
    await session.close()


def test_huawei_pushes_changes_itself():
    assert HuaweiBehavior.pushes_signal is True
    assert HuaweiBehavior.pushes_registration is True


# ------------------------------------------------------------------- SIM800

def test_sim800_tries_several_baudrates():
    assert Sim800Behavior.baudrates[0] == 115200
    assert 9600 in Sim800Behavior.baudrates


@pytest.mark.parametrize(
    ("answer", "pin", "puk"),
    [
        ("+SPIC: 3,3,10,10", 3, 10),
        ("+SPIC: 2,3,10,10", 2, 10),
        ("+SPIC: 3", 3, None),
    ],
)
def test_sim800_pin_attempts_parsing(answer, pin, puk):
    attempts = parse_spic(answer)

    assert attempts.pin == pin
    assert attempts.puk == puk
    assert attempts.source == "AT+SPIC"


def test_sim800_pin_attempts_unparseable_is_unknown():
    assert parse_spic("+SPIC: не число").known is False


async def test_sim800_reads_pin_attempts_with_its_command():
    session = await session_for({"AT+SPIC": "+SPIC: 3,3,10,10"})

    attempts = await Sim800Behavior().read_pin_attempts(session)

    assert attempts.pin == 3
    assert attempts.source == "AT+SPIC"
    await session.close()


def test_sim800_classifies_its_own_messages():
    behaviour = Sim800Behavior()

    assert behaviour.classify("+CSQN: 14").kind == Kind.SIGNAL
    assert behaviour.classify("+CSQN: 14").data["rssi"] == 14
    signal = behaviour.classify("+CIEV: 2,3")
    assert signal.kind == Kind.SIGNAL
    assert signal.data == {"bars": 3, "scale": 5}
    assert behaviour.classify("RDY").kind == Kind.BOOT
    assert behaviour.classify("NORMAL POWER DOWN").kind == Kind.POWER
    assert behaviour.classify("+CPIN: NOT READY").kind == Kind.SIM_STATE


async def test_sim800_reset_uses_cfun():
    session = await session_for({"AT+CFUN=1,1": "OK"})

    await Sim800Behavior().reset(session)

    assert session.transport.commands == ["AT+CFUN=1,1"]
    await session.close()


async def test_sim800_detects_self_reporting_of_signal():
    session = await session_for(
        {
            "ATE0": "OK",
            "AT+CMEE=1": "OK",
            "AT+CLIP=1": "OK",
            "AT+CMGF=0": "OK",
            "AT+CNMI=2,1,0,0,0": "OK",
            "AT+CREG=1": "OK",
            "AT+CGREG=1": "OK",
            "AT+CIURC=1": "OK",
            "AT+AUTOCSQ=1,1": "OK",
        }
    )
    behaviour = Sim800Behavior()

    await behaviour.enable_notifications(session)

    assert behaviour.pushes_signal is True
    await session.close()


async def test_sim800_without_self_reporting_keeps_polling():
    session = await session_for(
        {
            "ATE0": "OK",
            "AT+CLIP=1": "OK",
            "AT+CMGF=0": "OK",
            "AT+AUTOCSQ=1,1": "ERROR",
        }
    )
    behaviour = Sim800Behavior()

    await behaviour.enable_notifications(session)

    assert behaviour.pushes_signal is False
    await session.close()


# ------------------------------------------------------- инициализация вообще

async def test_pdu_mode_is_mandatory_in_initialisation():
    """Текстовый режим не отдаёт служебный заголовок -- без него части не собрать."""
    session = await session_for({"AT+CMGF=0": "ERROR"})
    behaviour = GenericAtBehavior()

    with pytest.raises(Exception):
        await behaviour.enable_notifications(session)

    await session.close()


async def test_caller_id_is_enabled_for_both_families():
    for behaviour in (HuaweiBehavior(), Sim800Behavior()):
        session = await session_for(
            {
                "ATE0": "OK",
                "AT+CMEE=1": "OK",
                "AT+CLIP=1": "OK",
                "AT+CMGF=0": "OK",
                "AT+CNMI=2,1,0,0,0": "OK",
                "AT+CREG=1": "OK",
                "AT+CGREG=1": "OK",
                "AT^CURC=1": "OK",
                "AT+CIURC=1": "OK",
                "AT+AUTOCSQ=1,1": "OK",
            }
        )
        await behaviour.enable_notifications(session)

        assert "AT+CLIP=1" in session.transport.commands
        assert "AT+CMGF=0" in session.transport.commands
        assert "AT+CNMI=2,1,0,0,0" in session.transport.commands
        await session.close()


async def test_generic_family_has_no_software_reset():
    session = await session_for({})

    assert GenericAtBehavior.supports_reset is False
    with pytest.raises(Exception):
        await GenericAtBehavior().reset(session)

    await session.close()
