"""Serialize Conso requests and persist admissions before any network work."""

from __future__ import annotations

from asyncio import Lock, Task, create_task, get_running_loop, shield, sleep
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from json import loads
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Literal, TypedDict
from uuid import UUID, uuid4

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

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


class ConsoBudgetError(HomeAssistantError):
    """The request budget could not safely authorize the operation."""


class ConsoBudgetExceeded(ConsoBudgetError):
    """The meter has no remaining capacity or is in cooldown."""


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

    async def _async_read(self) -> _BudgetData:
        self._check_running()
        await self._async_wait_for_save()
        self._check_running()
        if self._boot_id is None:
            self._boot_id = await self._hass.async_add_executor_job(_read_boot_id)
            # The previous HA process may have just sent a request on this boot.
            self._send_after = get_running_loop().time() + _REQUEST_GAP
        data = await self._hass.async_add_executor_job(self._read_disk)
        now = self._now()
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

    def _check_initial_pause(self, data: _BudgetData) -> None:
        if data["initial_pause_until"] > self._now():
            raise ConsoBudgetExceeded(_INITIAL_PAUSE_ERROR)

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

    async def _async_check_capacity(
        self, data: _BudgetData, meter: _MeterBudget, prm: str, now: float, required: int
    ) -> list[float]:
        self._check_initial_pause(data)
        cooldown = meter["cooldown_until"]
        if cooldown == "pending":
            # A crash or failed settlement left the previous response unknown.
            meter["cooldown_until"] = max(now + _WINDOW, self._unconfirmed_cooldowns.get(prm, 0))
            await self._async_save(data)
            self._unconfirmed_cooldowns.pop(prm, None)
            raise ConsoBudgetExceeded(_COOLDOWN_ERROR)
        if max(cooldown or 0, self._unconfirmed_cooldowns.get(prm, 0)) > now:
            raise ConsoBudgetExceeded(_COOLDOWN_ERROR)
        requests = [stamp for stamp in meter["requests"] if stamp > now - _WINDOW]
        if len(requests) + required > _LIMIT:
            raise ConsoBudgetExceeded("Conso request limit reached for this meter")
        return requests

    async def async_claim_daily(self, prm: str, day: date, *, required: int = 1) -> bool:
        """Claim the caller's Paris calendar day before starting scheduled work."""
        async with self._lock:
            data = await self._async_read()
            meter = self._meter(data["meters"], prm)
            day_key = day.isoformat()
            if meter["day"] is not None and meter["day"] >= day_key:
                return False
            if type(required) is not int or required < 1:
                raise ValueError("A daily claim must require at least one request")
            await self._async_check_capacity(data, meter, prm, self._now(), required)
            meter["day"] = day_key
            await self._async_save(data)
            return True

    @asynccontextmanager
    async def request(self, prm: str) -> AsyncIterator[Callable[[float], None]]:
        """Admit one request and yield a callback to mark a response cooldown in seconds."""
        async with self._lock:
            data = await self._async_read()
            meter = self._meter(data["meters"], prm)
            requests = await self._async_check_capacity(data, meter, prm, self._now(), 1)

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
