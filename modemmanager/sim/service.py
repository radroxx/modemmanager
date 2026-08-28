"""Обслуживание SIM-карты одного модема.

Часть обслуживания модема: подключается к ``Modem`` через контракт ``Component``.
Отвечает за опознание карты, её имя, состояние и защиту от блокировки по PUK.
"""

from __future__ import annotations

import logging
import time

from ..at.errors import AtError, ForbiddenCommand, PortGone
from ..behaviors.base import Kind, ModemBehavior, Unsolicited
from ..config import SettingsStore
from ..events import EventType
from ..modem import Modem, ModemStatus
from ..values import PinAttempts, SimState
from .imsi import SimIdentity, from_imsi, identify
from .pin import MIN_ATTEMPTS, PinAction, PinPlan, PinRejected, enter_pin, plan

log = logging.getLogger(__name__)


#: Соответствие состояния SIM и состояния обслуживания модема.
_STATUS_BY_SIM: dict[SimState, ModemStatus] = {
    SimState.READY: ModemStatus.ONLINE,
    SimState.ABSENT: ModemStatus.NO_SIM,
    SimState.PIN_REQUIRED: ModemStatus.PIN_REQUIRED,
    SimState.PUK_REQUIRED: ModemStatus.PUK_LOCKED,
}

#: События, которые нужно поднять при входе в состояние SIM.
_ENTRY_EVENT: dict[SimState, str] = {
    SimState.ABSENT: EventType.SIM_ABSENT,
    SimState.PUK_REQUIRED: EventType.PUK_LOCKED,
}


class SimService:
    """Часть обслуживания модема, отвечающая за SIM."""

    def __init__(
        self,
        store: SettingsStore,
        *,
        poll_interval: float = 60.0,
    ):
        self.store = store
        self.poll_interval = poll_interval
        self.modem: Modem | None = None
        self.identity: SimIdentity | None = None
        #: Последнее известное состояние SIM: чтобы отличать вход в состояние от
        #: продолжения. Событие ``sim_state`` публикуется только при изменении.
        self._sim_state: SimState = SimState.UNKNOWN
        #: PIN, отправленный в этой сессии обслуживания. Персистентности нет:
        #: счётчик попыток хранится на самой SIM и после перезапуска сам скажет,
        #: пробовать ли снова (см. design.md, D11).
        self._pin_attempt: str = ""
        #: Пометки «уведомление уже поднято» -- чтобы не спамить.
        self._unknown_reported: bool = False
        self._next_poll: float = 0.0

    # ------------------------------------------------------------- жизненный цикл

    async def start(self, modem: Modem) -> None:
        self.modem = modem
        await self._identify()
        # Начальная сверка -- часть запуска. Следующая сверка через регулярный
        # опрос; задвинуть срок сюда, чтобы ``poll`` сразу после ``start`` не
        # повторил ту же работу.
        self._next_poll = time.monotonic() + self.poll_interval
        await self._reconcile()

    async def stop(self) -> None:
        self.modem = None

    async def handle(self, unsolicited: Unsolicited) -> None:
        """Модем сам сообщил о смене состояния SIM -- проверим при ближайшем опросе."""
        if unsolicited.kind == Kind.SIM_STATE:
            self._next_poll = 0.0

    async def poll(self) -> None:
        """Регулярно перечитывает состояние SIM.

        Реже, чем регистрацию и сигнал: состояние SIM меняется только руками --
        карту вынули, PIN исправили в UI. Незапрошенные ``^SIMST`` сдвигают
        ближайший опрос на «сейчас».
        """
        now = time.monotonic()
        if now < self._next_poll:
            return
        self._next_poll = now + self.poll_interval
        try:
            await self._reconcile()
        except PortGone:
            raise
        except AtError as exc:
            log.debug(
                "%s: опрос SIM не удался (%s)",
                self._modem_path(),
                exc,
            )

    # ------------------------------------------------------------------- опознание

    async def _identify(self) -> None:
        """Читает IMSI и связывает его с модемом.

        Связь модем↔SIM держится только в памяти: следующий запуск обслуживания
        опросит железо заново, и совпадение восстановится, даже если файлы
        состояния (которых нет) не пережили перезапуск.
        """
        modem = self._require_modem()
        session = modem.session
        try:
            identity = await identify(session, modem.behavior)
        except AtError as exc:
            log.warning("%s: не удалось прочитать IMSI (%s)", self._modem_path(), exc)
            identity = SimIdentity(imsi="")
        self.identity = identity
        modem.state.imsi = identity.imsi
        modem.state.sim_label = self._label_for(identity)
        if not identity.imsi:
            # Модем не отдал IMSI (см. design.md, D7): без ключа настройки
            # не найти, обслуживание требует ручного вмешательства.
            log.warning(
                "%s: модем не отдал IMSI -- SIM неопознана", self._modem_path()
            )
            await self._report_unknown_sim(identity)
        elif not self._is_configured(identity.imsi):
            await self._report_unknown_sim(identity)

    def _label_for(self, identity: SimIdentity) -> str:
        """Пользовательское имя главнее автоматического.

        Автоимя даёт понятную привязку сразу, до всякой настройки; заданное
        пользователем имя пусть встаёт вместо него как только настройка появится.
        """
        if identity.imsi:
            settings = self.store.settings.sim(identity.imsi)
            if settings.label:
                return settings.label
        return identity.auto_label

    def _is_configured(self, imsi: str) -> bool:
        return self.store.settings.is_configured(imsi)

    async def _report_unknown_sim(self, identity: SimIdentity) -> None:
        if self._unknown_reported:
            return
        self._unknown_reported = True
        modem = self._require_modem()
        await modem.bus.publish(
            modem.event(
                EventType.SIM_UNKNOWN,
                {
                    "imsi": identity.imsi,
                    "auto_label": identity.auto_label,
                },
            )
        )

    # ---------------------------------------------------------------- сверка

    async def _reconcile(self) -> None:
        """Определяет состояние SIM и, если нужно, вводит PIN-код."""
        modem = self._require_modem()
        behavior: ModemBehavior = modem.behavior
        state, raw = await behavior.read_sim_state(modem.session)
        attempts = await self._read_attempts(behavior)
        modem.state.pin_attempts = attempts

        pin_configured = self._pin_configured(modem.state.imsi)
        decision = plan(state, attempts, pin_configured=pin_configured)

        if decision.action is PinAction.ENTER:
            state, attempts = await self._try_pin(behavior, attempts)
            modem.state.pin_attempts = attempts
            # После попытки заново оценим ситуацию: если PIN подошёл, SIM READY,
            # если нет -- остаток стал меньше и plan сам скажет «больше не пробуем».
            decision = plan(state, attempts, pin_configured=pin_configured)

        await self._apply_state(state, decision, raw=raw)

    async def _read_attempts(self, behavior: ModemBehavior) -> PinAttempts:
        modem = self._require_modem()
        try:
            return await behavior.read_pin_attempts(modem.session)
        except AtError as exc:
            return PinAttempts(reason=f"чтение остатка попыток не удалось: {exc}")

    async def _try_pin(
        self, behavior: ModemBehavior, attempts: PinAttempts
    ) -> tuple[SimState, PinAttempts]:
        """Вводит PIN один раз и возвращает свежее состояние SIM."""
        modem = self._require_modem()
        pin = self._pin_value(modem.state.imsi)
        if not pin:
            return (SimState.PIN_REQUIRED, attempts)
        if self._pin_attempt == pin:
            # Уже пробовали ровно этот PIN в этой сессии обслуживания. Повторной
            # попытки в одном пробеге не бывает -- решение принимает plan() по
            # свежему остатку попыток.
            return (SimState.PIN_REQUIRED, attempts)
        self._pin_attempt = pin
        try:
            await enter_pin(modem.session, pin)
        except PinRejected as exc:
            log.warning("%s: PIN-код отклонён модемом (%s)", self._modem_path(), exc.detail)
            new_attempts = await self._read_attempts(behavior)
            await modem.bus.publish(
                modem.event(
                    EventType.PIN_REJECTED,
                    {
                        "attempts": new_attempts.pin,
                        "known": new_attempts.known,
                        "detail": exc.detail,
                    },
                )
            )
            state, _ = await behavior.read_sim_state(modem.session)
            return (state, new_attempts)
        except ForbiddenCommand:
            # Если бы AT+CPIN попал в запрет -- это программная ошибка. Пропускаем
            # исключение наверх без перехвата: молчаливо игнорировать нельзя.
            raise
        # Модем принял PIN: свежее состояние скажет, стала ли SIM готова.
        state, _ = await behavior.read_sim_state(modem.session)
        new_attempts = await self._read_attempts(behavior)
        return (state, new_attempts)

    async def _apply_state(
        self,
        state: SimState,
        decision: PinPlan,
        *,
        raw: str,
    ) -> None:
        """Обновляет состояние модема и, при переходе, публикует события."""
        modem = self._require_modem()
        previous = self._sim_state
        modem.state.sim_state = state

        # Состояние обслуживания модема выводится из состояния SIM и решения.
        status = _STATUS_BY_SIM.get(state, ModemStatus.STARTING)
        if state is SimState.PIN_REQUIRED:
            status = ModemStatus.PIN_REQUIRED
        modem.state.status = status

        if state != previous:
            await modem.bus.publish(
                modem.event(
                    EventType.SIM_STATE,
                    {"state": state.value, "raw": raw, "previous": previous.value},
                )
            )
            entry = _ENTRY_EVENT.get(state)
            if entry:
                await modem.bus.publish(
                    modem.event(
                        entry,
                        self._entry_payload(state, decision),
                    )
                )
        self._sim_state = state

        # Отдельные события по PIN нужны и при повторном опросе: они
        # рассказывают администратору, чего именно система ждёт от него.
        if state is SimState.PIN_REQUIRED and decision.action is not PinAction.ENTER:
            await self._publish_pin_reason(decision)

    def _entry_payload(self, state: SimState, decision: PinPlan) -> dict:
        payload: dict = {"imsi": self._require_modem().state.imsi}
        if state is SimState.PUK_REQUIRED and decision.attempts is not None:
            payload["puk_attempts"] = decision.attempts
        return payload

    async def _publish_pin_reason(self, decision: PinPlan) -> None:
        """Уведомляет о PIN: только при первом входе в состояние.

        Дедупликация повторов -- забота уровня уведомлений, здесь фиксируется
        лишь смена причины ожидания.
        """
        modem = self._require_modem()
        previous = getattr(self, "_pin_reason", "")
        if previous == decision.action.value:
            return
        self._pin_reason = decision.action.value  # type: ignore[attr-defined]
        event_type = {
            PinAction.REFUSE: EventType.PIN_GUARD,
            PinAction.WAIT: EventType.PIN_REQUIRED,
        }.get(decision.action)
        if event_type is None:
            return
        await modem.bus.publish(
            modem.event(
                event_type,
                {
                    "attempts": decision.attempts,
                    "known": decision.attempts is not None,
                    "reason": decision.reason,
                    "min_attempts": MIN_ATTEMPTS,
                },
            )
        )

    # ------------------------------------------------------------- вспомогательные

    def _require_modem(self) -> Modem:
        if self.modem is None:
            raise RuntimeError("SimService не подключён к модему")
        return self.modem

    def _modem_path(self) -> str:
        return self.modem.usb_path if self.modem is not None else "?"

    def _pin_value(self, imsi: str) -> str:
        if not imsi:
            return ""
        return self.store.settings.sim(imsi).pin

    def _pin_configured(self, imsi: str) -> bool:
        return bool(self._pin_value(imsi))


__all__ = ["SimService"]
