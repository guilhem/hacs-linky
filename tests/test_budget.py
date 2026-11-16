"""Exercise Conso admission and cooldown with Home Assistant's on-disk store."""

from asyncio import CancelledError, Event, create_task, gather, sleep
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import storage

from custom_components.linky import budget as budget_module
from custom_components.linky.budget import (
    ConsoBudgetError,
    ConsoBudgetExceeded,
    async_get_conso_budget,
)
from custom_components.linky.const import PARIS_TZ

_PRM = "99999999999999"
_OTHER_PRM = "00000000000001"
_START = datetime(2026, 10, 24, 21, 59, tzinfo=UTC)
_DAY = _START.astimezone(PARIS_TZ).date()
_WINDOW = 24 * 60 * 60


@pytest.fixture
def clock(monkeypatch):
    instant = SimpleNamespace(wall=_START.timestamp(), elapsed=0.0)
    monkeypatch.setattr(budget_module, "time", lambda: instant.wall)
    monkeypatch.setattr(budget_module, "monotonic", lambda: instant.elapsed)
    return instant


async def _admit(budget, prm=_PRM):
    async with budget.request(prm):
        pass


@pytest.fixture
async def ready(tmp_path, clock):
    clock.wall -= _WINDOW
    hass = HomeAssistant(str(tmp_path))
    budget = async_get_conso_budget(hass)
    await budget.async_initialize()
    clock.wall += _WINDOW
    clock.elapsed += _WINDOW
    return hass, budget


async def test_initial_pause_survives_restart_and_does_not_claim(tmp_path, clock):
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await budget.async_initialize()
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization"):
        await budget.async_claim_daily(_PRM, _DAY)
    clock.wall += _WINDOW - 1
    clock.elapsed += _WINDOW - 1
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await budget.async_initialize()
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization"):
        async with budget.request(_OTHER_PRM):
            pytest.fail("Admitted a request during initial grace")
    clock.wall += 1
    clock.elapsed += 1
    assert await budget.async_claim_daily(_PRM, _DAY)
    for _ in range(3):
        await _admit(budget)


async def test_missing_store_is_initialized_lazily(tmp_path, clock):
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization"):
        await _admit(budget)
    clock.wall += _WINDOW
    clock.elapsed += _WINDOW
    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await _admit(reopened)


async def test_claim_is_shared_durable_and_once_per_paris_day(tmp_path, ready):
    hass, budget = ready
    assert async_get_conso_budget(hass) is budget
    claims = await gather(*(budget.async_claim_daily(_PRM, _DAY) for _ in range(2)))
    assert sorted(claims) == [False, True]
    assert await budget.async_claim_daily(_OTHER_PRM, _DAY)

    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    assert not await reopened.async_claim_daily(_PRM, _DAY)
    assert await reopened.async_claim_daily(_PRM, _DAY + timedelta(days=1))
    assert not await reopened.async_claim_daily(_PRM, _DAY)


async def test_request_is_serialized_across_meters(ready):
    _, budget = ready
    entering, entered, finish = Event(), Event(), Event()

    async def other_meter():
        entering.set()
        async with budget.request(_OTHER_PRM):
            entered.set()
            await finish.wait()

    async with budget.request(_PRM):
        task = create_task(other_meter())
        await entering.wait()
        assert not entered.is_set()
    await entered.wait()
    finish.set()
    await task


async def test_limit_rolls_across_paris_midnight_dst_and_restart(tmp_path, clock, ready):
    _, budget = ready
    for _ in range(3):
        await _admit(budget)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)
    await _admit(budget, _OTHER_PRM)

    clock.wall += 120
    clock.elapsed += 120
    tomorrow = datetime.fromtimestamp(clock.wall, PARIS_TZ).date()
    assert tomorrow != _DAY
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    assert await budget.async_claim_daily(_PRM, tomorrow)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)

    clock.elapsed = 2 * _WINDOW - 1
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)
    clock.elapsed = 2 * _WINDOW
    await _admit(budget)


async def test_clock_changes_cannot_refill_the_budget_early(tmp_path, clock, ready):
    _, budget = ready
    for _ in range(3):
        await _admit(budget)
    clock.wall += 2 * _WINDOW
    with pytest.raises(ConsoBudgetExceeded):
        await _admit(budget)

    clock.wall = _START.timestamp() - 60
    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    clock.elapsed += 60
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(reopened)


async def test_failed_and_cancelled_attempts_are_not_refunded(tmp_path, ready):
    _, budget = ready
    with pytest.raises(TimeoutError):
        async with budget.request(_PRM):
            raise TimeoutError
    entered = Event()

    async def attempt():
        async with budget.request(_PRM):
            entered.set()
            await Event().wait()

    task = create_task(attempt())
    await entered.wait()
    task.cancel()
    with pytest.raises(CancelledError):
        await task

    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await _admit(reopened)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(reopened)


@pytest.mark.parametrize("delay", [0, 2 * _WINDOW])
async def test_cooldown_is_per_meter_and_survives_restart(tmp_path, clock, ready, delay):
    _, budget = ready
    async with budget.request(_PRM) as mark_cooldown:
        clock.elapsed += 1
        mark_cooldown(delay)
    clock.wall += 1
    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await _admit(reopened, _OTHER_PRM)
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await _admit(reopened)

    clock.elapsed = _WINDOW + max(_WINDOW, delay)  # One second before the cooldown ends.
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await _admit(reopened)
    clock.elapsed += 1
    await _admit(reopened)


async def test_failed_atomic_save_is_detected_before_admitting_or_claiming(ready, monkeypatch):
    _, budget = ready

    def fail_write(*args, **kwargs):
        raise storage.WriteError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write_utf8_file_atomic", fail_write)
        with pytest.raises(ConsoBudgetError, match="on disk"):
            await budget.async_claim_daily(_PRM, _DAY)
        with pytest.raises(ConsoBudgetError, match="on disk"):
            async with budget.request(_PRM):
                pytest.fail("Admitted a request without a durable reservation")
    assert await budget.async_claim_daily(_PRM, _DAY)
    for _ in range(3):
        await _admit(budget)


async def test_failed_cooldown_write_preserves_the_error_and_blocks_on_restart(
    tmp_path, ready, monkeypatch
):
    _, budget = ready

    def fail_write(*args, **kwargs):
        raise storage.WriteError("simulated disk failure")

    with monkeypatch.context() as patch, pytest.raises(ValueError, match="original provider error"):
        async with budget.request(_PRM) as mark_cooldown:
            mark_cooldown(0)
            patch.setattr(storage, "write_utf8_file_atomic", fail_write)
            raise ValueError("original provider error")

    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await _admit(reopened)
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await _admit(reopened)


async def test_shutdown_between_reservation_and_confirmation_blocks_send(ready, monkeypatch):
    hass, budget = ready
    save = storage.Store.async_save

    async def stop_then_defer_write(store, data):
        hass.set_state(CoreState.stopping)
        await save(store, data)

    monkeypatch.setattr(storage.Store, "async_save", stop_then_defer_write)
    with pytest.raises(ConsoBudgetError):
        async with budget.request(_PRM):
            pytest.fail("Admitted a request while shutting down")
    with pytest.raises(ConsoBudgetError, match="shutdown"):
        await _admit(budget)


async def test_cancelled_write_is_waited_for_before_reading_again(ready, monkeypatch):
    _, budget = ready
    saving, finish = Event(), Event()
    save = storage.Store.async_save

    async def slow_save(store, data):
        saving.set()
        await finish.wait()
        await save(store, data)

    monkeypatch.setattr(storage.Store, "async_save", slow_save)
    claim = create_task(budget.async_claim_daily(_PRM, _DAY))
    await saving.wait()
    claim.cancel()
    with pytest.raises(CancelledError):
        await claim
    duplicate = create_task(budget.async_claim_daily(_PRM, _DAY))
    await sleep(0)
    assert not duplicate.done()
    finish.set()
    assert not await duplicate
