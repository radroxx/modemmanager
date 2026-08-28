"""Цикл сверки: фактические устройства против обслуживаемых.

Опрос sysfs вместо подписки на события ядра выбран сознательно: сверка полного
списка самовосстанавливается. Пропущенное событие подключения ничего не ломает --
следующий проход всё равно увидит устройство, а лишние проходы стоят одного
чтения каталога.

Исчезновение объявляется не сразу: короткий провал по питанию или переинициализация
USB даёт паузу в пару секунд, и уведомлять о пропадании модема из-за неё не нужно.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from ..config import SettingsStore
from ..events import Event, EventBus, EventType
from ..modem import Component, Modem, ModemStatus
from ..modem_registry import ModemRegistry
from .probe import Prober, ProbeResult, TransportFactory, default_transport_factory
from .sysfs import UsbDevice, enumerate_devices

log = logging.getLogger(__name__)

Enumerator = Callable[..., list[UsbDevice]]
ComponentFactory = Callable[[Modem], Sequence[Component]]


class Reconciler:
    """Приводит набор обслуживаемых модемов в соответствие с фактическим."""

    def __init__(
        self,
        *,
        store: SettingsStore,
        bus: EventBus,
        registry: ModemRegistry,
        prober: Prober | None = None,
        transport_factory: TransportFactory = default_transport_factory,
        enumerator: Enumerator = enumerate_devices,
        component_factory: ComponentFactory | None = None,
    ):
        self.store = store
        self.bus = bus
        self.registry = registry
        self.transport_factory = transport_factory
        self.enumerator = enumerator
        self.component_factory = component_factory
        self.prober = prober or self._make_prober()

        #: Устройства, которых не видно, и когда их не стало.
        self._missing: dict[str, float] = {}
        #: Сколько раз модем на этом пути пропадал и возвращался в пределах дебаунса.
        self._reconnects: dict[str, int] = {}
        #: Неисправные устройства и время следующей попытки.
        self._retry_at: dict[str, float] = {}
        #: Устройства, о неисправности которых уже сообщено: путь -> причина.
        self._faulted: dict[str, str] = {}

        self._task: asyncio.Task | None = None
        self._losses: set[asyncio.Task] = set()
        self._stopping = False
        #: Сколько проходов сверки выполнено -- для диагностики и метрик.
        self.sweeps = 0

    def _make_prober(self) -> Prober:
        discovery = self.store.settings.discovery
        return Prober(
            transport_factory=self.transport_factory,
            baudrates=tuple(discovery.probe_baudrates),
            port_baudrate=discovery.port_baudrate,
            timeout=discovery.probe_timeout,
            max_attempts=discovery.probe_max_attempts,
            retry_backoff=discovery.probe_retry_backoff,
            trace=discovery.at_trace,
        )

    # ------------------------------------------------------------ жизненный цикл

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="discovery-sweep")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for pending in list(self._losses):
            pending.cancel()
        for modem in self.registry:
            self.registry.remove(modem.usb_path)
            try:
                await modem.stop(reason="остановка приложения")
            except Exception:
                log.exception("%s: сбой остановки модема", modem.usb_path)

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сбой цикла сверки")
            await asyncio.sleep(self.store.settings.discovery.scan_interval)

    # -------------------------------------------------------------------- сверка

    async def sweep(self) -> None:
        """Один проход сверки. Сбой одного устройства не мешает остальным."""
        self.sweeps += 1
        discovery = self.store.settings.discovery
        devices = self.enumerator(
            discovery.drivers,
            sysfs_root=discovery.sysfs_root,
            dev_root=discovery.dev_root,
        )
        present = {device.usb_path: device for device in devices}
        # Вердикты «не модем» относятся к присутствующим устройствам: исчезнувшее
        # устройство должно быть проверено заново, когда вернётся.
        self.prober.forget_missing(devices)

        await self._collect_missing(present)
        for device in devices:
            try:
                await self._reconcile_device(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: сбой обслуживания устройства", device.usb_path)

    async def _collect_missing(self, present: dict[str, UsbDevice]) -> None:
        for modem in self.registry:
            if modem.usb_path not in present:
                await self._note_missing(modem.usb_path, "устройства нет в системе")
        now = time.monotonic()
        debounce = self.store.settings.discovery.gone_debounce
        for usb_path, since in list(self._missing.items()):
            if usb_path in present:
                continue
            if now - since >= debounce:
                await self._declare_gone(usb_path)

    async def _reconcile_device(self, device: UsbDevice) -> None:
        usb_path = device.usb_path
        modem = self.registry.get(usb_path)

        if modem is not None and usb_path in self._missing:
            # Устройство вернулось в пределах дебаунса: то же самое обслуживание,
            # но сессию нужно поднять заново -- прежний дескриптор мёртв.
            del self._missing[usb_path]
            self._reconnects[usb_path] = self._reconnects.get(usb_path, 0) + 1
            log.info(
                "%s: устройство вернулось, переподключение %d",
                usb_path,
                self._reconnects[usb_path],
            )
            await self._restart(device, modem, reconnected=True)
            return

        if modem is not None and modem.state.status is ModemStatus.FAULT:
            if self._retry_due(usb_path):
                await self._restart(device, modem, reconnected=False)
            return

        if modem is not None:
            return  # обслуживается и не подаёт признаков беды

        if not self._retry_due(usb_path):
            return
        await self._bring_up(device, reconnected=False)

    def _retry_due(self, usb_path: str) -> bool:
        """Пора ли повторить попытку для устройства, которое не удалось поднять."""
        deadline = self._retry_at.get(usb_path)
        if deadline is None:
            return True
        if time.monotonic() < deadline:
            return False
        del self._retry_at[usb_path]
        return True

    # ------------------------------------------------------------------- запуск

    async def _bring_up(self, device: UsbDevice, *, reconnected: bool) -> None:
        """Пробует устройство и, если это модем, начинает обслуживание."""
        result = await self.prober.probe(device)
        if not result.ok:
            if result.fault:
                await self._mark_fault(device, result.reason)
            else:
                # Не модем: вердикт уже в кэше пробы, повторять незачем.
                self._retry_at.pop(device.usb_path, None)
            return

        try:
            modem = self._create(device, result)
            await modem.start()
        except Exception as exc:
            log.exception("%s: не удалось начать обслуживание", device.usb_path)
            await self._mark_fault(device, f"запуск не удался: {exc}")
            return

        self.registry.add(modem)
        self._retry_at.pop(device.usb_path, None)
        was_faulted = self._faulted.pop(device.usb_path, None) is not None
        if reconnected or was_faulted:
            await modem.mark_recovered()
        else:
            await self.bus.publish(
                modem.event(
                    EventType.MODEM_UP,
                    {
                        "family": modem.behavior.family,
                        "model": modem.state.identity.description,
                        "control_port": modem.state.control_port,
                        "baudrate": modem.state.baudrate,
                    },
                )
            )

    def _create(self, device: UsbDevice, result: ProbeResult) -> Modem:
        settings = self.store.settings
        transport = self.transport_factory(result.control_port, result.baudrate)
        modem = Modem(
            device=device,
            behavior=result.behavior,  # type: ignore[arg-type]
            transport=transport,
            bus=self.bus,
            identity=result.identity,
            baudrate=result.baudrate,
            intervals=settings.intervals,
            on_lost=self._device_lost,
            trace=settings.discovery.at_trace,
        )
        modem.state.reconnects = self._reconnects.get(device.usb_path, 0)
        if self.component_factory is not None:
            modem.components.extend(self.component_factory(modem))
        return modem

    async def _restart(self, device: UsbDevice, modem: Modem, *, reconnected: bool) -> None:
        """Поднимает обслуживание заново, сохранив накопленные счётчики."""
        self.registry.remove(modem.usb_path)
        try:
            await modem.stop(reason="переподключение" if reconnected else "восстановление")
        except Exception:
            log.exception("%s: сбой остановки перед перезапуском", modem.usb_path)
        await self._bring_up(device, reconnected=reconnected)

    # ---------------------------------------------------------------- потеря

    def _device_lost(self, modem: Modem) -> None:
        """Порт сообщил об исчезновении: начинаем дебаунс не дожидаясь сверки."""
        task = asyncio.create_task(
            self._note_missing(modem.usb_path, "ошибка чтения порта"),
            name=f"discovery-lost:{modem.usb_path}",
        )
        self._losses.add(task)
        task.add_done_callback(self._losses.discard)

    async def _note_missing(self, usb_path: str, reason: str) -> None:
        """Отмечает исчезновение и останавливает сессию, не объявляя пропадание."""
        if usb_path in self._missing:
            return
        self._missing[usb_path] = time.monotonic()
        modem = self.registry.get(usb_path)
        log.info("%s: устройство пропало (%s), жду возвращения", usb_path, reason)
        if modem is None:
            return
        modem.state.status = ModemStatus.GONE
        try:
            await modem.stop(reason=reason)
        except Exception:
            log.exception("%s: сбой остановки исчезнувшего модема", usb_path)

    async def _declare_gone(self, usb_path: str) -> None:
        """Дебаунс истёк: модем считается отсутствующим, администратор уведомлён."""
        self._missing.pop(usb_path, None)
        self._reconnects.pop(usb_path, None)
        self._retry_at.pop(usb_path, None)
        self._faulted.pop(usb_path, None)
        modem = self.registry.remove(usb_path)
        if modem is None:
            return
        modem.state.status = ModemStatus.GONE
        try:
            await modem.stop(reason="устройство не вернулось")
        except Exception:
            log.exception("%s: сбой остановки пропавшего модема", usb_path)
        await self.bus.publish(
            modem.event(
                EventType.MODEM_GONE,
                {"debounce": self.store.settings.discovery.gone_debounce},
            )
        )

    # ------------------------------------------------------------ неисправность

    async def _mark_fault(self, device: UsbDevice, reason: str) -> None:
        """Переводит устройство в состояние неисправности с повторными попытками."""
        interval = self.store.settings.discovery.fault_retry_interval
        self._retry_at[device.usb_path] = time.monotonic() + interval
        if device.usb_path in self._faulted:
            return  # об этом уже сообщено, второй раз незачем
        self._faulted[device.usb_path] = reason
        modem = self.registry.get(device.usb_path)
        if modem is not None:
            await modem.mark_fault(reason)
            return
        await self.bus.publish(
            _device_event(device, EventType.MODEM_FAULT, {"reason": reason})
        )

    @property
    def faults(self) -> dict[str, str]:
        """Устройства, которые не удалось поднять: путь на шине -> причина.

        Такое устройство не попадает в список модемов -- управлять им не удалось,
        и представлять его нечем, -- но на странице статуса о нём нужно сказать.
        """
        return dict(self._faulted)

    # ------------------------------------------------------------- по требованию

    async def rescan(self) -> None:
        """Внеочередная сверка -- для кнопки в интерфейсе."""
        await self.sweep()

    def forget_verdict(self, usb_path: str) -> None:
        """Снимает вердикт и отсрочку, чтобы устройство проверили в следующий проход."""
        self._retry_at.pop(usb_path, None)
        self._faulted.pop(usb_path, None)
        for device in self.enumerator(
            self.store.settings.discovery.drivers,
            sysfs_root=self.store.settings.discovery.sysfs_root,
            dev_root=self.store.settings.discovery.dev_root,
        ):
            if device.usb_path == usb_path:
                self.prober.forget(device)


def _device_event(device: UsbDevice, event_type: str, data: dict) -> Event:
    """Событие об устройстве, которое ещё не стало обслуживаемым модемом."""
    return Event(
        type=event_type,
        usb_path=device.usb_path,
        data={"device": device.description, **data},
    )
