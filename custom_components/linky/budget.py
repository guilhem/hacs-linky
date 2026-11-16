"""Serialize Conso requests and persist admissions before any network work."""

from __future__ import annotations

from asyncio import Lock, Task, create_task, current_task, get_running_loop, shield, sleep
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from datetime import date, datetime, time, timedelta
from json import loads
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Any, Literal, TypedDict
from uuid import UUID, uuid4

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, PARIS_TZ

_KEY = f"{DOMAIN}.conso_budget"
_VERSION = 1
_WINDOW = 24 * 60 * 60
_LIMIT = 3
_REQUEST_GAP = 0.2
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_PROCESS_BOOT_ID = f"process:{uuid4()}"
_STORAGE_ERROR = "Could not verify the Conso request budget on disk"
_COOLDOWN_ERROR = "Conso requests are paused for this meter"
_INITIAL_PAUSE_ERROR = "Conso requests are paused for 24 hours after budget initialization"
DAILY_ATTEMPT_USED = "The daily Linky refresh has already been attempted for this meter"


class ConsoBudgetError(HomeAssistantError):
    """The request budget could not safely authorize the operation."""


class ConsoBudgetExceeded(ConsoBudgetError):
    """The meter has no remaining capacity or is in cooldown."""

    def __init__(self, message: str, *, retry_in: float | None = None) -> None:
        super().__init__(message)
        self.retry_in = retry_in


class _MeterBudget(TypedDict):
    day: str | None
    requests: list[float]
    cooldown_until: float | Literal["pending"] | None


class _BudgetData(TypedDict):
    boot_id: str
    saved_at: float
    initial_pause_until: float
    meters: dict[str, _MeterBudget]


_BUDGET: HassKey[ConsoBudget] = HassKey(_KEY)


def _read_boot_id() -> str:
    try:
        return str(UUID(_BOOT_ID_PATH.read_text(encoding="ascii").strip()))
    except OSError, ValueError:
        return _PROCESS_BOOT_ID


@callback
def async_get_conso_budget(hass: HomeAssistant) -> ConsoBudget:
    """Return the shared budget for every Conso caller in this HA instance."""
    if _BUDGET not in hass.data:
        hass.data[_BUDGET] = ConsoBudget(hass)
    return hass.data[_BUDGET]


class ConsoBudget:
    """Keep scheduled claims and rolling transport limits in one private HA store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store = Store[_BudgetData](hass, _VERSION, _KEY, private=True, atomic_writes=True)
        self._lock = Lock()
        self._batch_owner: Task | None = None
        self._save_task: Task[bool] | None = None
        self._unconfirmed_cooldowns: dict[str, float] = {}
        self._boot_id: str | None = None
        self._send_after = 0.0

    def _now(self) -> float:
        return float(monotonic())

    def _check_running(self) -> None:
        if self._hass.is_stopping or self._hass.state is CoreState.stopped:
            raise ConsoBudgetError("Conso requests are unavailable during shutdown")

    def _read_disk(self) -> _BudgetData | None:
        # Store.async_load can read a pending shutdown write or the HA preload cache.
        try:
            envelope = loads(Path(self._store.path).read_bytes())
            if (
                envelope["key"] != _KEY
                or envelope["version"] != _VERSION
                or envelope["minor_version"] != self._store.minor_version
            ):
                raise ValueError
            data = envelope["data"]
            until = data["initial_pause_until"]
            if (
                not isinstance(data["boot_id"], str)
                or not data["boot_id"]
                or type(data["saved_at"]) is not float
                or not isfinite(data["saved_at"])
                or type(until) is not float
                or not isfinite(until)
                or not isinstance(data["meters"], dict)
            ):
                raise ValueError
            return data
        except FileNotFoundError:
            return None
        except OSError, ValueError, TypeError, KeyError, RecursionError:
            raise ConsoBudgetError(_STORAGE_ERROR) from None

    async def _async_wait_for_save(self) -> bool:
        if self._save_task is None:
            return True
        # The executor write can outlive a cancelled caller. The next lock holder must wait.
        saved = await shield(self._save_task)
        self._save_task = None
        return saved

    async def _async_read(self, *, initialize: bool = True) -> _BudgetData:
        self._check_running()
        await self._async_wait_for_save()
        self._check_running()
        if self._boot_id is None:
            self._boot_id = await self._hass.async_add_executor_job(_read_boot_id)
            # The previous HA process may have just sent a request on this boot.
            self._send_after = get_running_loop().time() + _REQUEST_GAP
        data = await self._hass.async_add_executor_job(self._read_disk)
        now = self._now()
        if not initialize and (
            data is None or data["boot_id"] != self._boot_id or now < data["saved_at"]
        ):
            raise ConsoBudgetError("The Conso request budget is not initialized for this boot")
        if data is None:
            # Older versions have no ledger; allow their untracked requests to age out.
            data = {
                "boot_id": self._boot_id,
                "saved_at": now,
                "initial_pause_until": now + _WINDOW,
                "meters": {},
            }
            await self._async_save(data)
        elif data["boot_id"] != self._boot_id or now < data["saved_at"]:
            # Uptime across boots is incomparable. Keep the last known remaining cooldown.
            previous = data["saved_at"]
            for prm in data["meters"]:
                meter = self._meter(data["meters"], prm)
                cooldown = meter["cooldown_until"]
                known = self._unconfirmed_cooldowns.get(prm, previous)
                if cooldown == "pending":
                    known = max(known, previous + _WINDOW)
                elif cooldown is not None:
                    known = max(known, cooldown)
                meter["cooldown_until"] = now + (known - previous) if known > previous else None
                meter["requests"] = []
            data["boot_id"] = self._boot_id
            data["saved_at"] = now
            data["initial_pause_until"] = now + _WINDOW
            await self._async_save(data)
            self._unconfirmed_cooldowns.clear()
        self._check_running()
        return data

    async def _async_save(self, data: _BudgetData) -> None:
        self._check_running()
        now = self._now()
        if now < data["saved_at"]:
            raise ConsoBudgetError("Conso request budget clock moved backwards")
        data["saved_at"] = now

        async def save() -> bool:
            try:
                await self._store.async_save(data)
            except HomeAssistantError, OSError:
                return False
            return True

        self._save_task = create_task(save())
        # HA Store can swallow a WriteError, so confirm the exact new state on disk.
        saved = await self._async_wait_for_save()
        self._check_running()
        if not saved or await self._hass.async_add_executor_job(self._read_disk) != data:
            raise ConsoBudgetError(_STORAGE_ERROR)
        self._check_running()

    @staticmethod
    def _meter(data: dict[str, _MeterBudget], prm: str) -> _MeterBudget:
        if prm not in data:
            data[prm] = {"day": None, "requests": [], "cooldown_until": None}
        meter = data[prm]
        try:
            day, requests, cooldown = meter["day"], meter["requests"], meter["cooldown_until"]
            if (
                (day is not None and date.fromisoformat(day).isoformat() != day)
                or not isinstance(requests, list)
                or not all(type(stamp) is float and isfinite(stamp) for stamp in requests)
                or (
                    cooldown is not None
                    and cooldown != "pending"
                    and not (type(cooldown) is float and isfinite(cooldown))
                )
            ):
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise ConsoBudgetError(_STORAGE_ERROR) from None
        return meter

    async def async_initialize(self) -> None:
        """Seed the ledger without sending or claiming anything."""
        async with self._lock:
            await self._async_read()

    async def _async_capacity(
        self,
        data: _BudgetData,
        meter: _MeterBudget,
        prm: str,
        now: float,
        required: int,
        *,
        settle_pending: bool = True,
    ) -> tuple[list[float], ConsoBudgetExceeded | None]:
        requests = [stamp for stamp in meter["requests"] if stamp > now - _WINDOW]
        limit_error = "Conso request limit reached for this meter"
        if required > _LIMIT:
            return requests, ConsoBudgetExceeded(limit_error)

        known = self._unconfirmed_cooldowns.get(prm, 0)
        cooldown = meter["cooldown_until"]
        if cooldown == "pending":
            if not settle_pending:
                return requests, ConsoBudgetExceeded(
                    "The previous Conso request could not be confirmed"
                )
            # A crash or failed settlement left the previous response unknown.
            cooldown = meter["cooldown_until"] = max(now + _WINDOW, known)
            await self._async_save(data)
            self._unconfirmed_cooldowns.pop(prm, None)

        cooldown_until = max(cooldown or 0, known)
        pause_until = data["initial_pause_until"]
        needed = len(requests) + required - _LIMIT
        if pause_until > now:
            message = _INITIAL_PAUSE_ERROR
        elif cooldown_until > now:
            message = _COOLDOWN_ERROR
        elif needed > 0:
            message = limit_error
        else:
            return requests, None

        available_at = sorted(requests)[needed - 1] + _WINDOW if needed > 0 else now
        retry_at = max(pause_until, cooldown_until, available_at)
        return requests, ConsoBudgetExceeded(message, retry_in=max(0.0, retry_at - self._now()))

    async def async_get_status(self, prm: str, *, required: int) -> dict[str, Any]:
        """Check capacity without claiming a day or reserving a request."""
        async with self._lock:
            data = await self._async_read(initialize=False)
            meter = self._meter(data["meters"], prm)
            requests, blocked = await self._async_capacity(
                data, meter, prm, self._now(), required, settle_pending=False
            )
            reason = str(blocked) if blocked else None
            retry_in = blocked.retry_in if blocked else None
            day = meter["day"]
            now = dt_util.now(PARIS_TZ)
            if day is not None and day >= now.date().isoformat():
                next_day = date.fromisoformat(day) + timedelta(days=1)
                next_slot = datetime.combine(next_day, time(), PARIS_TZ)
                reason = reason or DAILY_ATTEMPT_USED
                if blocked is None or retry_in is not None:
                    retry_in = max(retry_in or 0.0, next_slot.timestamp() - now.timestamp())
            return {
                "requests_remaining": max(0, _LIMIT - len(requests)),
                "last_daily_attempt": day,
                "can_refresh": reason is None,
                "blocking_reason": reason,
                "retry_in_seconds": retry_in,
            }

    @asynccontextmanager
    async def daily_batch(self, prm: str, day: date, *, required: int = 1) -> AsyncIterator[bool]:
        """Claim the day and hold transport for requests awaited by this task."""
        async with self._lock:
            data = await self._async_read()
            meter = self._meter(data["meters"], prm)
            day_key = day.isoformat()
            if meter["day"] is None or meter["day"] < day_key:
                if type(required) is not int or required < 1:
                    raise ValueError("A daily claim must require at least one request")
                _, blocked = await self._async_capacity(data, meter, prm, self._now(), required)
                if blocked is not None:
                    raise blocked
                meter["day"] = day_key
                await self._async_save(data)
                self._batch_owner = current_task()
                try:
                    yield True
                finally:
                    self._batch_owner = None
                return
        yield False

    @asynccontextmanager
    async def request(self, prm: str) -> AsyncIterator[Callable[[float], None]]:
        """Admit one request and yield a callback to mark a response cooldown in seconds."""
        in_batch = self._batch_owner is not None and self._batch_owner is current_task()
        async with nullcontext() if in_batch else self._lock:
            data = await self._async_read()
            meter = self._meter(data["meters"], prm)
            requests, blocked = await self._async_capacity(data, meter, prm, self._now(), 1)
            if blocked is not None:
                raise blocked

            loop = get_running_loop()
            delay = self._send_after - loop.time()
            if delay > 0:
                await sleep(delay)

            meter["requests"] = [*requests, self._now()]
            meter["cooldown_until"] = "pending"
            await self._async_save(data)

            def mark_cooldown(delay: float) -> None:
                self._unconfirmed_cooldowns[prm] = self._now() + max(_WINDOW, delay)

            failed = True
            try:
                yield mark_cooldown
                failed = False
            finally:
                self._send_after = loop.time() + _REQUEST_GAP
                # The send may have started after the reservation reached disk.
                meter["requests"][-1] = self._now()
                meter["cooldown_until"] = self._unconfirmed_cooldowns.get(prm)
                try:
                    await self._async_save(data)
                except ConsoBudgetError:
                    if not failed:
                        raise
                else:
                    self._unconfirmed_cooldowns.pop(prm, None)
