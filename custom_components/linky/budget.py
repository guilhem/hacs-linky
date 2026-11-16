"""Serialize Conso requests and persist admissions before any network work."""

from __future__ import annotations

from asyncio import Lock, Task, create_task, shield
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from json import loads
from math import isfinite
from pathlib import Path
from time import monotonic, time
from typing import Literal, TypedDict

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_KEY = f"{DOMAIN}.conso_budget"
_VERSION = 1
_WINDOW = 24 * 60 * 60
_LIMIT = 3
_STORAGE_ERROR = "Could not verify the Conso request budget on disk"
_COOLDOWN_ERROR = "Conso requests are paused for this meter"
_INITIAL_PAUSE_ERROR = "Conso requests are paused for 24 hours after budget initialization"


class ConsoBudgetError(HomeAssistantError):
    """The request budget could not safely authorize the operation."""


class ConsoBudgetExceeded(ConsoBudgetError):
    """The meter has no remaining capacity or is in cooldown."""


class _MeterBudget(TypedDict):
    day: str | None
    requests: list[float]
    cooldown_until: float | Literal["pending"] | None


class _BudgetData(TypedDict):
    initial_pause_until: float
    meters: dict[str, _MeterBudget]


_BUDGET: HassKey[ConsoBudget] = HassKey(_KEY)


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
        self._save_task: Task[bool] | None = None
        self._unconfirmed_cooldowns: dict[str, float] = {}
        self._clock_offset = time() - monotonic()

    def _now(self) -> float:
        # Use elapsed time within this process while keeping timestamps comparable on restart.
        return self._clock_offset + monotonic()

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
                type(until) is not float
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

    async def _async_read(self) -> _BudgetData:
        self._check_running()
        await self._async_wait_for_save()
        self._check_running()
        data = await self._hass.async_add_executor_job(self._read_disk)
        if data is None:
            # Older versions have no ledger; allow their untracked requests to age out.
            data = {"initial_pause_until": self._now() + _WINDOW, "meters": {}}
            await self._async_save(data)
        self._check_running()
        return data

    def _check_initial_pause(self, data: _BudgetData) -> None:
        if data["initial_pause_until"] > self._now():
            raise ConsoBudgetExceeded(_INITIAL_PAUSE_ERROR)

    async def _async_save(self, data: _BudgetData) -> None:
        self._check_running()

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

    async def async_claim_daily(self, prm: str, day: date) -> bool:
        """Claim the caller's Paris calendar day before starting scheduled work."""
        async with self._lock:
            data = await self._async_read()
            self._check_initial_pause(data)
            meter = self._meter(data["meters"], prm)
            day_key = day.isoformat()
            if meter["day"] is not None and meter["day"] >= day_key:
                return False
            meter["day"] = day_key
            await self._async_save(data)
            return True

    @asynccontextmanager
    async def request(self, prm: str) -> AsyncIterator[Callable[[float], None]]:
        """Admit one request and yield a callback to mark a response cooldown in seconds."""
        async with self._lock:
            data = await self._async_read()
            self._check_initial_pause(data)
            meter = self._meter(data["meters"], prm)
            now = self._now()
            cooldown = meter["cooldown_until"]
            if cooldown == "pending":
                # A crash or failed settlement left the previous response unknown.
                meter["cooldown_until"] = max(
                    now + _WINDOW, self._unconfirmed_cooldowns.get(prm, 0)
                )
                await self._async_save(data)
                self._unconfirmed_cooldowns.pop(prm, None)
                raise ConsoBudgetExceeded(_COOLDOWN_ERROR)
            if max(cooldown or 0, self._unconfirmed_cooldowns.get(prm, 0)) > now:
                raise ConsoBudgetExceeded(_COOLDOWN_ERROR)
            requests = [stamp for stamp in meter["requests"] if stamp > now - _WINDOW]
            if len(requests) >= _LIMIT:
                raise ConsoBudgetExceeded("Conso request limit reached for this meter")

            meter["requests"] = [*requests, now]
            meter["cooldown_until"] = "pending"
            await self._async_save(data)

            def mark_cooldown(delay: float) -> None:
                self._unconfirmed_cooldowns[prm] = self._now() + max(_WINDOW, delay)

            failed = True
            try:
                yield mark_cooldown
                failed = False
            finally:
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
