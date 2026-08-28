"""Значения: нормализация уровня сигнала и производные признаки."""

from __future__ import annotations

import pytest

from modemmanager.values import (
    Operator,
    PinAttempts,
    Registration,
    RegistrationState,
    Signal,
    Storage,
    registration_state,
)


@pytest.mark.parametrize(
    ("rssi", "dbm"),
    [(0, -113), (1, -111), (10, -93), (17, -79), (31, -51)],
)
def test_csq_scale_boundaries(rssi, dbm):
    assert Signal.from_csq(rssi).dbm == dbm


def test_csq_unknown_is_not_a_number():
    signal = Signal.from_csq(99)

    assert signal.dbm is None
    assert signal.known is False
    assert signal.raw == 99
    assert signal.bars is None


def test_csq_out_of_scale_is_unknown():
    """Значение вне 0..31 -- не «нулевой сигнал», а отсутствие данных."""
    assert Signal.from_csq(42).known is False
    assert Signal.from_csq(-1).known is False


def test_bars_scale_is_converted_to_dbm():
    assert Signal.from_bars(0, 5).dbm == -113
    assert Signal.from_bars(5, 5).dbm == -51
    assert Signal.from_bars(3, 5).known is True
    assert Signal.from_bars(6, 5).known is False


def test_bars_derived_from_dbm():
    assert Signal(dbm=-51).bars == 5
    assert Signal(dbm=-60).bars == 4
    assert Signal(dbm=-80).bars == 2
    assert Signal(dbm=-100).bars == 1
    assert Signal(dbm=-113).bars == 0


@pytest.mark.parametrize("value", [0, 1, 2, 3, 4, 5])
def test_bars_survive_the_round_trip(value):
    """Деление, сообщённое модемом, не должно меняться при показе."""
    assert Signal.from_bars(value).bars == value


def test_registration_codes():
    assert registration_state(1) is RegistrationState.REGISTERED
    assert registration_state(5) is RegistrationState.ROAMING
    assert registration_state(4) is RegistrationState.UNKNOWN
    assert registration_state(77) is RegistrationState.UNKNOWN


def test_roaming_and_sms_usability():
    roaming = Registration(voice=RegistrationState.ROAMING, data=RegistrationState.SEARCHING)
    assert roaming.roaming is True
    assert roaming.usable_for_sms is True

    partial = Registration(voice=RegistrationState.SEARCHING, data=RegistrationState.ROAMING)
    assert partial.roaming is True
    assert partial.usable_for_sms is False


def test_unknown_pin_attempts_are_not_zero():
    unknown = PinAttempts(reason="модем не понял команду")

    assert unknown.known is False
    assert unknown.pin is None
    assert PinAttempts(pin=0).known is True  # исчерпан -- это известное значение


def test_storage_fullness():
    assert Storage(used=20, total=20).full is True
    assert Storage(used=5, total=20).free == 15
    assert Storage().known is False
    assert Storage().full is False
    assert Storage().free is None


def test_operator_label():
    assert Operator(plmn="25002", name="MegaFon").label == "MegaFon (25002)"
    assert Operator(plmn="25002").label == "25002"
    assert Operator().label == "неизвестен"
    assert Operator().known is False
