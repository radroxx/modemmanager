"""Проба устройства: семейство, перебор скоростей, негативный кэш, управляющий порт."""

from __future__ import annotations

import logging

from fake_answers import HUAWEI, NO_NOTIFICATIONS, UNKNOWN_MODEM
from fake_modem import FakePortSet as FakeBus

from modemmanager.discovery.probe import Prober, probe_all
from modemmanager.discovery.sysfs import UsbDevice


def device(*ports: str, usb_path: str = "3-1", hint: tuple[str, str] = ("12d1", "1001")):
    return UsbDevice(
        usb_path=usb_path,
        vendor_id=hint[0],
        product_id=hint[1],
        manufacturer="HUAWEI Technology",
        product="HUAWEI Mobile",
        ports=list(ports),
        drivers={"option1"},
    )


def prober_for(bus: FakeBus, **kwargs) -> Prober:
    kwargs.setdefault("timeout", 0.05)
    # Тестам нужна старая семантика «первая молчаливая проба -- вердикт»,
    # backoff и повторы проверяются отдельно.
    kwargs.setdefault("max_attempts", 1)
    kwargs.setdefault("retry_backoff", 0.0)
    return Prober(transport_factory=bus.factory, **kwargs)


# ------------------------------------------------------------------ опознание

async def test_responding_device_is_recognised():
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI})
    prober = prober_for(bus)

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.ok is True
    assert result.behavior is not None and result.behavior.family == "huawei"
    assert result.identity.model == "E3372"
    assert result.imei == "861234567890123"
    assert result.control_port == "/dev/ttyUSB0"
    assert result.baudrate == 115200


async def test_responding_but_unknown_device_gets_generic_behaviour():
    """Устройство отвечает на AT, значит это модем -- пусть и незнакомый."""
    bus = FakeBus({"/dev/ttyUSB0": UNKNOWN_MODEM})
    prober = prober_for(bus)

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.ok is True
    assert result.behavior is not None and result.behavior.family == "generic"
    assert result.control_port == "/dev/ttyUSB0"


async def test_silent_device_is_not_a_modem():
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = prober_for(bus)

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.ok is False
    assert result.behavior is None
    assert result.reason
    assert result.responding_ports == []


async def test_verdict_is_remembered_and_the_port_is_not_reopened():
    """К устройству, признанному не модемом, система больше не обращается."""
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = prober_for(bus)
    target = device("/dev/ttyUSB0")

    await prober.probe(target)
    touched_after_first = list(bus.touched)
    second = await prober.probe(target)

    assert bus.touched == touched_after_first
    assert second.ok is False
    assert prober.rejected(target) == second.reason


async def test_reconnected_device_is_checked_again():
    """Переподключение меняет набор портов -- вердикт снимается сам."""
    bus = FakeBus({"/dev/ttyUSB0": None, "/dev/ttyUSB5": HUAWEI})
    prober = prober_for(bus)

    await prober.probe(device("/dev/ttyUSB0"))
    result = await prober.probe(device("/dev/ttyUSB5"))

    assert result.ok is True


async def test_forgetting_a_verdict_allows_a_new_probe():
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = prober_for(bus)
    target = device("/dev/ttyUSB0")

    await prober.probe(target)
    prober.forget(target)
    bus.tables["/dev/ttyUSB0"] = HUAWEI

    assert (await prober.probe(target)).ok is True


async def test_missing_devices_leave_the_cache():
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = prober_for(bus)
    target = device("/dev/ttyUSB0")

    await prober.probe(target)
    prober.forget_missing([])

    assert prober.rejected(target) is None


# ---------------------------------------------------------------- скорости

async def test_baudrate_sweep_stops_at_the_first_answer():
    """Перебор прекращается на первом ответе и не повторяется дальше."""
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI}, baudrates={9600})
    prober = prober_for(bus, baudrates=(115200, 9600))

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.ok is True
    assert result.baudrate == 9600
    # Первое открытие -- неудачная попытка на 115200, дальше только 9600.
    assert bus.touched[0] == ("/dev/ttyUSB0", 115200)
    assert bus.touched[1] == ("/dev/ttyUSB0", 9600)
    assert 115200 not in [baudrate for _port, baudrate in bus.touched[2:]]


async def test_fixed_baudrate_is_not_swept():
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI})
    prober = prober_for(
        bus, baudrates=(115200, 9600), port_baudrate={"/dev/ttyUSB0": 9600}
    )

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.baudrate == 9600
    assert {baudrate for _port, baudrate in bus.touched} == {9600}


# ------------------------------------------------------- управляющий порт

async def test_control_port_is_chosen_by_experiment():
    """Отвечает не первый порт, а уведомления включаются на третьем."""
    bus = FakeBus(
        {
            "/dev/ttyUSB0": None,
            "/dev/ttyUSB1": NO_NOTIFICATIONS,
            "/dev/ttyUSB2": HUAWEI,
        }
    )
    prober = prober_for(bus)

    result = await prober.probe(device("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"))

    assert result.ok is True
    assert result.control_port == "/dev/ttyUSB2"
    # Порядковый номер порта ни при чём: годность подтверждена инициализацией.
    assert "/dev/ttyUSB1" in bus.ports_touched


async def test_device_without_a_usable_port_becomes_faulty():
    """Модем есть, управлять им нельзя: это неисправность, а не «не модем»."""
    bus = FakeBus({"/dev/ttyUSB0": NO_NOTIFICATIONS, "/dev/ttyUSB1": NO_NOTIFICATIONS})
    prober = prober_for(bus)
    target = device("/dev/ttyUSB0", "/dev/ttyUSB1")

    result = await prober.probe(target)

    assert result.ok is False
    assert result.fault is True
    assert result.behavior is not None
    assert result.reason
    # Вердикт «не модем» не выносится, иначе восстановление невозможно.
    assert prober.rejected(target) is None


async def test_control_port_keeps_the_confirmed_baudrate():
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI}, baudrates={9600})
    prober = prober_for(bus, baudrates=(115200, 9600))

    result = await prober.probe(device("/dev/ttyUSB0"))

    assert (result.control_port, result.baudrate) == ("/dev/ttyUSB0", 9600)


async def test_every_probed_port_is_closed():
    """Порт не должен остаться открытым: иначе обслуживание его не получит."""
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI})
    prober = prober_for(bus)

    await prober.probe(device("/dev/ttyUSB0"))

    assert all(not transport.is_open for transport in bus.transports)


# ----------------------------------------------------------- проба набора

# ---------------------------------------------------------------- трассировка

async def test_probe_writes_at_traffic_when_trace_is_on(caplog):
    """При включённой трассировке проба пишет обмен по каждому порту."""
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI})
    prober = prober_for(bus, trace=True)

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        result = await prober.probe(device("/dev/ttyUSB0"))

    assert result.ok is True
    traffic = [
        record.getMessage()
        for record in caplog.records
        if " > AT" in record.getMessage() or " < " in record.getMessage()
    ]
    assert traffic, "нет трассировочных строк"
    assert any("/dev/ttyUSB0" in message for message in traffic)


async def test_probe_stays_silent_when_trace_is_off(caplog):
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI})
    prober = prober_for(bus)  # trace по умолчанию False

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        await prober.probe(device("/dev/ttyUSB0"))

    assert not any(
        " > AT" in record.getMessage() or " < " in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------- backoff

async def test_transient_silence_is_retried_before_verdict():
    """Молчаливая проба не запоминается с первой попытки: даётся пять шансов."""
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = Prober(
        transport_factory=bus.factory,
        timeout=0.01,
        max_attempts=5,
        retry_backoff=0.0,
    )
    target = device("/dev/ttyUSB0")

    for _ in range(4):
        result = await prober.probe(target)
        assert result.ok is False
        assert prober.rejected(target) is None

    final = await prober.probe(target)
    assert final.ok is False
    assert prober.rejected(target) == final.reason


async def test_success_before_the_limit_forgets_prior_failures():
    """Ответ модема на N-ой попытке отменяет накопленные неудачи."""
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = Prober(
        transport_factory=bus.factory,
        timeout=0.01,
        max_attempts=5,
        retry_backoff=0.0,
    )
    target = device("/dev/ttyUSB0")

    for _ in range(2):
        await prober.probe(target)
    assert prober.rejected(target) is None

    bus.tables["/dev/ttyUSB0"] = HUAWEI
    result = await prober.probe(target)

    assert result.ok is True
    assert prober.rejected(target) is None

    # После успеха счётчик сбрасывается: молчание с нуля пойдёт снова.
    bus.tables["/dev/ttyUSB0"] = None
    silent = await prober.probe(target)
    assert silent.ok is False
    assert prober.rejected(target) is None


async def test_backoff_skips_probe_between_attempts():
    """В паузе между попытками порт не трогается; истечение таймера пускает пробу."""
    bus = FakeBus({"/dev/ttyUSB0": None})
    prober = Prober(
        transport_factory=bus.factory,
        timeout=0.01,
        max_attempts=5,
        retry_backoff=60.0,
    )
    target = device("/dev/ttyUSB0")

    await prober.probe(target)
    touched = list(bus.touched)

    # Backoff держит: следующий вызов даже не открывает порт.
    result = await prober.probe(target)
    assert bus.touched == touched
    assert "пауза" in result.reason

    # Имитация истечения таймера -- следующая проба выполняется.
    prober._next_try.clear()
    await prober.probe(target)
    assert bus.touched != touched


async def test_probe_all_isolates_a_failing_device():
    bus = FakeBus({"/dev/ttyUSB0": HUAWEI, "/dev/ttyUSB1": HUAWEI})
    prober = prober_for(bus)
    devices = [
        device("/dev/ttyUSB0", usb_path="3-1"),
        device("/dev/ttyUSB1", usb_path="3-2"),
    ]

    broken = devices[0]

    async def explode(target):
        if target is broken:
            raise RuntimeError("проба сломалась")
        return await Prober.probe(prober, target)

    prober.probe = explode  # type: ignore[method-assign]

    results = await probe_all(prober, devices)

    assert len(results) == 2
    assert results[0].ok is False and "сломалась" in results[0].reason
    assert results[1].ok is True
