"""HTTP-интерфейс: страницы, JSON-API, авторизация, метрики."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport
from starlette.testclient import TestClient

from modemmanager.behaviors import HuaweiBehavior
from modemmanager.config import IntervalSettings, SettingsStore, SimSettings
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.eventlog import EventLog
from modemmanager.modem import Modem, ModemStatus
from modemmanager.modem_registry import ModemRegistry
from modemmanager.network import NetworkService
from modemmanager.values import Identity, PinAttempts, Signal
from modemmanager.web.server import WebServer


PASSWORD = "secret-1"
ADMIN_CHAT = "admin-1"
IMSI = "89701020123456789042"


def _store(tmp_path: Path) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.web.password = PASSWORD
    store.settings.telegram.token = "token"
    store.settings.telegram.admin_chat_id = ADMIN_CHAT
    return store


def _basic_auth(password: str) -> dict[str, str]:
    encoded = base64.b64encode(f":{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _make_modem(*, usb_path="3-1", imsi=IMSI, sim_label="Работа") -> Modem:
    """Собирает модем целиком в памяти, без запуска обмена по портy."""
    transport = FakeTransport(dict(HUAWEI))
    bus = EventBus()
    modem = Modem(
        device=UsbDevice(usb_path=usb_path, ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890" + usb_path.replace("-", "")),
        intervals=IntervalSettings(),
    )
    modem.state.imsi = imsi
    modem.state.sim_label = sim_label
    modem.state.signal = Signal(dbm=-79, raw=17)
    modem.state.pin_attempts = PinAttempts(pin=3, source="AT^CPIN?")
    modem.state.status = ModemStatus.ONLINE
    return modem


def _make_web(tmp_path: Path) -> tuple[WebServer, SettingsStore, ModemRegistry, EventLog]:
    store = _store(tmp_path)
    registry = ModemRegistry()
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.ensure_file()
    web = WebServer(store=store, registry=registry, event_log=event_log)
    return (web, store, registry, event_log)


# --------------------------------------------- 10.2 авторизация одним паролем

class TestAuthentication:
    def test_pages_require_authentication(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            for path in ("/", "/history", "/sims", "/api/state"):
                response = client.get(path)
                assert response.status_code == 401, path
                assert "Basic" in response.headers.get("WWW-Authenticate", "")

    def test_pages_open_with_correct_password(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            response = client.get("/", headers=_basic_auth(PASSWORD))
            assert response.status_code == 200

    def test_wrong_password_is_rejected(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            response = client.get("/", headers=_basic_auth("wrong"))
            assert response.status_code == 401

    def test_metrics_endpoint_is_open(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            response = client.get("/metrics")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")


# ---------------------------------- 10.3 API состояния модемов и SIM

class TestStateApi:
    def test_state_returns_working_modem(self, tmp_path):
        web, _store, registry, _ = _make_web(tmp_path)
        registry.add(_make_modem())
        with TestClient(web.app) as client:
            data = client.get("/api/state", headers=_basic_auth(PASSWORD)).json()
        assert data["counts"]["online"] == 1
        modem = data["modems"][0]
        assert modem["imsi"] == IMSI
        assert modem["signal_dbm"] == -79
        assert modem["status"] == "online"

    def test_state_returns_no_sim_and_faulted(self, tmp_path):
        web, _store, registry, _ = _make_web(tmp_path)
        online = _make_modem(usb_path="3-1")
        no_sim = _make_modem(usb_path="3-2", imsi="", sim_label="")
        no_sim.state.status = ModemStatus.NO_SIM
        fault = _make_modem(usb_path="3-3", imsi="", sim_label="")
        fault.state.status = ModemStatus.FAULT
        fault.state.fault_reason = "порт не отвечает"
        registry.add(online)
        registry.add(no_sim)
        registry.add(fault)
        with TestClient(web.app) as client:
            data = client.get("/api/state", headers=_basic_auth(PASSWORD)).json()
        statuses = {m["status"] for m in data["modems"]}
        assert {"online", "no_sim", "fault"} <= statuses
        faulty = next(m for m in data["modems"] if m["status"] == "fault")
        assert faulty["fault_reason"] == "порт не отвечает"

    def test_state_represents_unknown_signal_distinctly(self, tmp_path):
        web, _store, registry, _ = _make_web(tmp_path)
        modem = _make_modem()
        modem.state.signal = Signal.unknown()
        registry.add(modem)
        with TestClient(web.app) as client:
            data = client.get("/api/state", headers=_basic_auth(PASSWORD)).json()
        assert data["modems"][0]["signal_dbm"] is None
        assert data["modems"][0]["signal_bars"] is None


# --------------------------------- 10.4 API настроек SIM (write-only PIN)

class TestSimApi:
    def test_setting_pin_never_returns_value(self, tmp_path):
        """Задать PIN, значение не должно вернуться ни в POST, ни в GET."""
        web, store, _registry, _ = _make_web(tmp_path)
        secret_pin = "8765"  # не является подстрокой IMSI
        with TestClient(web.app) as client:
            response = client.post(
                f"/api/sims/{IMSI}",
                json={"pin": secret_pin, "label": "Работа"},
                headers=_basic_auth(PASSWORD),
            )
            assert response.status_code == 200
            body = response.json()
            assert body["pin_set"] is True
            assert "pin" not in body
            assert secret_pin not in json.dumps(body)
            got = client.get(f"/api/sims/{IMSI}", headers=_basic_auth(PASSWORD)).json()
            assert got["pin_set"] is True
            assert "pin" not in got
            assert secret_pin not in json.dumps(got)
        # Значение действительно попало в настройки.
        assert store.settings.sims[IMSI].pin == secret_pin

    def test_deleting_pin_via_null(self, tmp_path):
        web, store, _registry, _ = _make_web(tmp_path)
        store.settings.sims[IMSI] = SimSettings(pin="8765", label="Работа")
        with TestClient(web.app) as client:
            response = client.post(
                f"/api/sims/{IMSI}",
                json={"pin": None},
                headers=_basic_auth(PASSWORD),
            )
            assert response.status_code == 200
            assert response.json()["pin_set"] is False
        assert store.settings.sims[IMSI].pin == ""

    def test_omitting_pin_preserves_previous_value(self, tmp_path):
        web, store, _registry, _ = _make_web(tmp_path)
        store.settings.sims[IMSI] = SimSettings(pin="8765", label="Работа")
        with TestClient(web.app) as client:
            response = client.post(
                f"/api/sims/{IMSI}",
                json={"label": "Роуминг"},  # без ключа pin
                headers=_basic_auth(PASSWORD),
            )
            assert response.status_code == 200
        assert store.settings.sims[IMSI].pin == "8765"
        assert store.settings.sims[IMSI].label == "Роуминг"


# ---------------------------------------- 10.5 API истории (фильтр и лимит)

class TestHistoryApi:
    def _seed_events(self, event_log: EventLog, *records: dict) -> None:
        async def write():
            for record in records:
                event = Event(
                    type=record["type"],
                    imsi=record.get("imsi"),
                    data=record.get("data", {}),
                )
                await event_log.append(event)

        asyncio.get_event_loop().run_until_complete(write()) if False else asyncio.run(write())

    def test_general_history_returns_all_recent_records(self, tmp_path):
        web, _store, _registry, log = _make_web(tmp_path)
        self._seed_events(
            log,
            {"type": "sms", "imsi": IMSI, "data": {"text": "a"}},
            {"type": "call", "imsi": IMSI, "data": {"number": "+7999"}},
            {"type": "sms", "imsi": "OTHER", "data": {"text": "b"}},
        )
        with TestClient(web.app) as client:
            data = client.get("/api/history", headers=_basic_auth(PASSWORD)).json()
        assert isinstance(data, list)
        assert len(data) == 3

    def test_history_filtered_by_imsi(self, tmp_path):
        web, _store, _registry, log = _make_web(tmp_path)
        self._seed_events(
            log,
            {"type": "sms", "imsi": IMSI, "data": {"text": "a"}},
            {"type": "sms", "imsi": "OTHER", "data": {"text": "b"}},
        )
        with TestClient(web.app) as client:
            data = client.get(
                f"/api/history?imsi={IMSI}",
                headers=_basic_auth(PASSWORD),
            ).json()
        assert all(r.get("imsi") == IMSI for r in data)

    def test_history_limit_smaller_than_records_returned(self, tmp_path):
        """Если записей меньше запрошенного, отдаётся сколько есть."""
        web, _store, _registry, log = _make_web(tmp_path)
        self._seed_events(log, {"type": "sms", "imsi": IMSI})
        with TestClient(web.app) as client:
            data = client.get(
                "/api/history?limit=100", headers=_basic_auth(PASSWORD)
            ).json()
        assert len(data) == 1


# ----------------------------------- 10.6 запуск сканирования из интерфейса

class _FakeNetworkService:
    """Стойка вместо ``NetworkService`` для тестов веб-эндпоинта скана.

    Не касается AT-сессии, чтобы тест не зависел от event loop клиента
    ``TestClient``. Реальные интеграционные проверки скана -- в test_network.py.
    """

    def __init__(self, candidates):
        from modemmanager.network import ScanResult

        self._candidates = candidates
        self._scan_result = ScanResult(
            candidates=list(candidates), started_at=0.0, duration=0.0
        )

    async def scan(self, timeout: float = 120.0):
        return self._scan_result

    async def apply_operator(self) -> None:
        return None


class TestScanApi:
    def test_scan_endpoint_returns_candidates(self, tmp_path):
        from modemmanager.values import NetworkCandidate

        web, store, registry, _ = _make_web(tmp_path)
        modem = _make_modem()
        fake = _FakeNetworkService(
            [
                NetworkCandidate(plmn="25001", name="MTS", status=1, technology="7"),
                NetworkCandidate(plmn="25002", name="MegaFon", status=2, technology=""),
            ]
        )
        modem.components = [fake]
        # ``find_component(NetworkService)`` не найдёт стойку -- патчим напрямую.
        modem.find_component = lambda cls: fake if cls is NetworkService else None
        registry.add(modem)

        with TestClient(web.app) as client:
            response = client.post(
                f"/api/modems/{modem.usb_path}/scan",
                headers=_basic_auth(PASSWORD),
            )
            assert response.status_code == 200
            data = response.json()
        plmns = [c["plmn"] for c in data["candidates"]]
        assert "25001" in plmns
        assert "25002" in plmns


# ------------------------------ 10.7 страница статуса с автообновлением

class TestStatusPage:
    def test_status_page_has_all_required_fields_and_autorefresh(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            response = client.get("/", headers=_basic_auth(PASSWORD))
            assert response.status_code == 200
            html = response.text
        # Заголовки колонок отражают все требуемые сведения.
        for column in (
            "Состояние",
            "USB",
            "SIM",
            "IMSI",
            "Сигнал",
            "Оператор",
            "Регистрация",
            "Память SMS",
        ):
            assert column in html
        # Автообновление реализовано через setInterval + fetch.
        assert "setInterval" in html
        assert "/api/state" in html


# --------------------------- 10.8 история и настройки SIM без PIN

class TestPages:
    def test_history_page_marks_incomplete_message(self, tmp_path):
        web, *_ = _make_web(tmp_path)
        with TestClient(web.app) as client:
            html = client.get("/history", headers=_basic_auth(PASSWORD)).text
        # Клиентский рендер добавляет пометку -- проверяем, что она заложена.
        assert "не полностью" in html
        assert "/api/history" in html

    def test_sim_page_does_not_contain_pin_value(self, tmp_path):
        web, store, *_ = _make_web(tmp_path)
        secret_pin = "87654321"  # заведомо не подстрока IMSI
        store.settings.sims[IMSI] = SimSettings(pin=secret_pin, label="Работа", msisdn="+79999")
        with TestClient(web.app) as client:
            html = client.get(f"/sims/{IMSI}", headers=_basic_auth(PASSWORD)).text
        assert secret_pin not in html
        assert "задан" in html
        assert 'name="pin"' in html


# ---------------------------- 10.1 сервер не блокирует опрос модемов

class TestNonBlocking:
    def test_state_endpoint_responds_while_scan_is_slow(self, tmp_path):
        """Пока один запрос сканирования висит, /api/state отдаётся сразу."""
        web, store, registry, _ = _make_web(tmp_path)
        modem = _make_modem()

        # Заменяем реальный NetworkService.scan на медленный, чтобы имитировать
        # длительную операцию, конкурирующую с обслуживанием других запросов.
        class SlowNetwork:
            def __init__(self, store):
                self.store = store
                self.busy = False

            async def scan(self, timeout=120.0):
                self.busy = True
                await asyncio.sleep(0.5)
                self.busy = False
                from modemmanager.network import ScanResult

                return ScanResult(candidates=[], started_at=0, duration=0.5)

            async def apply_operator(self):
                pass

        slow = SlowNetwork(store)
        modem.components = [slow]
        # Патчим find_component: обычная проверка isinstance ожидает NetworkService.
        original_find = modem.find_component
        modem.find_component = lambda cls: slow if cls is NetworkService else original_find(cls)
        registry.add(modem)

        with TestClient(web.app) as client:
            # Запускаем скан асинхронно через отдельный HTTP-клиент; ждём, пока
            # он «застрянет», и проверяем, что /api/state отвечает быстро.
            import threading
            import time

            done: list[int] = []

            def run_scan():
                r = client.post(
                    f"/api/modems/{modem.usb_path}/scan",
                    headers=_basic_auth(PASSWORD),
                )
                done.append(r.status_code)

            thread = threading.Thread(target=run_scan, daemon=True)
            thread.start()
            time.sleep(0.05)  # дать скану захватить очередь

            start = time.monotonic()
            response = client.get("/api/state", headers=_basic_auth(PASSWORD))
            elapsed = time.monotonic() - start

            assert response.status_code == 200
            # /api/state должен вернуться до того, как «медленный» скан
            # завершится (0.5с). Двойная головная планка -- 0.3с.
            assert elapsed < 0.3, f"api/state ответил за {elapsed}с"

            thread.join(timeout=2.0)
            assert done == [200]
