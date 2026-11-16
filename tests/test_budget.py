"""Exercise Conso admission and cooldown with Home Assistant's on-disk store."""

from asyncio import CancelledError, Event, create_task, gather, get_running_loop, sleep, timeout
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import storage

from custom_components.linky import budget as budget_module
from custom_components.linky.budget import (
    DAILY_ATTEMPT_USED,
    ConsoBudgetError,
    ConsoBudgetExceeded,
    async_get_conso_budget,
)
from custom_components.linky.const import PARIS_TZ

_PRM = "99999999999999"
_OTHER_PRM = "00000000000001"
_THIRD_PRM = "00000000000002"
_START = datetime(2026, 10, 24, 21, 59, tzinfo=UTC)
_DAY = _START.astimezone(PARIS_TZ).date()
_WINDOW = 24 * 60 * 60


@pytest.fixture
def clock(monkeypatch):
    instant = SimpleNamespace(wall=_START.timestamp(), elapsed=0.0)
    monkeypatch.setattr(budget_module, "monotonic", lambda: instant.elapsed)
    return instant


async def _admit(budget, prm=_PRM):
    async with budget.request(prm):
        pass


async def _claim(budget, prm, day, *, required=1):
    async with budget.daily_batch(prm, day, required=required) as claimed:
        return claimed


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
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization") as error:
        await _claim(budget, _PRM, _DAY)
    assert error.value.retry_in == _WINDOW
    clock.wall += _WINDOW - 1
    clock.elapsed += _WINDOW - 1
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await budget.async_initialize()
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization") as error:
        async with budget.request(_OTHER_PRM):
            pytest.fail("Admitted a request during initial grace")
    assert error.value.retry_in == 1
    clock.wall += 1
    clock.elapsed += 1
    assert await _claim(budget, _PRM, _DAY)
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
    claims = await gather(*(_claim(budget, _PRM, _DAY) for _ in range(2)))
    assert sorted(claims) == [False, True]
    assert await _claim(budget, _OTHER_PRM, _DAY)

    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    assert not await _claim(reopened, _PRM, _DAY)
    assert await _claim(reopened, _PRM, _DAY + timedelta(days=1))
    assert not await _claim(reopened, _PRM, _DAY)


async def test_request_is_serialized_and_spaced_across_meters(ready):
    _, budget = ready
    entering, entered, finish = Event(), Event(), Event()
    loop = get_running_loop()
    times = []

    async def other_meter():
        entering.set()
        async with budget.request(_OTHER_PRM):
            times.append(loop.time())
            entered.set()
            await finish.wait()

    async with budget.request(_PRM):
        task = create_task(other_meter())
        await entering.wait()
        assert not entered.is_set()
        times.append(loop.time())
    await entered.wait()
    finish.set()
    await task
    assert times[1] - times[0] >= 0.2


@pytest.mark.parametrize("competitor", [_PRM, _OTHER_PRM], ids=["token-check", "import"])
async def test_daily_batch_keeps_queued_callers_out_until_both_requests_finish(ready, competitor):
    _, budget = ready
    await _admit(budget)
    queued = Event()

    async def compete():
        queued.set()
        await _admit(budget, competitor)

    async with timeout(6):
        async with budget.daily_batch(_PRM, _DAY, required=2) as claimed:
            assert claimed
            task = create_task(compete())
            await queued.wait()
            for _ in range(2):
                await _admit(budget)
                assert not task.done()

        if competitor == _PRM:
            with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
                await task
        else:
            await task

        async with budget.daily_batch(_PRM, _DAY, required=2) as claimed:
            assert not claimed


async def test_two_slot_batch_reports_when_second_oldest_attempt_expires(ready, clock):
    _, budget = ready
    await _admit(budget)
    clock.elapsed += 10
    await _admit(budget)
    clock.elapsed += 20
    await _admit(budget)

    with pytest.raises(ConsoBudgetExceeded, match="limit reached") as error:
        async with budget.daily_batch(_PRM, _DAY, required=2):
            pytest.fail("Claimed the day before two slots were available")
    assert error.value.retry_in == _WINDOW - 20

    clock.elapsed += error.value.retry_in
    async with budget.daily_batch(_PRM, _DAY, required=2) as claimed:
        assert claimed


async def test_status_reports_rolling_capacity_and_the_next_paris_day(ready):
    _, budget = ready
    midnight = datetime(2026, 10, 25, tzinfo=PARIS_TZ)
    with freeze_time(midnight, real_asyncio=True):
        day = midnight.date()
        assert await budget.async_get_status(_PRM, required=2) == {
            "requests_remaining": 3,
            "last_daily_attempt": None,
            "can_refresh": True,
            "blocking_reason": None,
            "retry_in_seconds": None,
        }
        assert await _claim(budget, _PRM, day, required=2)
        status = await budget.async_get_status(_PRM, required=2)
        assert status["last_daily_attempt"] == day.isoformat()
        assert not status["can_refresh"]
        assert status["blocking_reason"] == DAILY_ATTEMPT_USED
        assert status["retry_in_seconds"] == 25 * 3600

        await _admit(budget)
        await _admit(budget)
        status = await budget.async_get_status(_PRM, required=2)
        assert status["requests_remaining"] == 1
        assert "limit reached" in status["blocking_reason"]
        assert status["retry_in_seconds"] == 25 * 3600

        async with budget.request(_PRM) as mark_cooldown:
            mark_cooldown(2 * _WINDOW)
        status = await budget.async_get_status(_PRM, required=2)
        assert status["requests_remaining"] == 0
        assert "paused" in status["blocking_reason"]
        assert status["retry_in_seconds"] == 2 * _WINDOW


async def test_limit_rolls_across_paris_midnight_dst_and_restart(tmp_path, clock, ready):
    _, budget = ready
    async with budget.request(_PRM):
        clock.elapsed += 1
        clock.wall += 1
    for _ in range(2):
        await _admit(budget)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)
    await _admit(budget, _OTHER_PRM)

    clock.wall += 120
    clock.elapsed += 120
    tomorrow = datetime.fromtimestamp(clock.wall, PARIS_TZ).date()
    assert tomorrow != _DAY
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _claim(budget, _PRM, tomorrow)

    clock.elapsed = 2 * _WINDOW
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)
    clock.elapsed += 1
    assert await _claim(budget, _PRM, tomorrow)
    await _admit(budget)


async def test_wall_clock_jump_and_restart_on_same_boot_do_not_refill(tmp_path, clock, ready):
    _, budget = ready
    for _ in range(3):
        await _admit(budget)
    clock.wall += 2 * _WINDOW
    with freeze_time(datetime.fromtimestamp(clock.wall, UTC), real_asyncio=True):
        reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
        clock.elapsed += 60
        with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
            await _admit(reopened)


@pytest.mark.parametrize("reset", ["boot", "fallback", "regression"])
async def test_uptime_reset_pauses_all_requests_and_carries_cooldown(
    tmp_path, clock, monkeypatch, reset
):
    boot_id = tmp_path / "boot_id"
    monkeypatch.setattr(budget_module, "_BOOT_ID_PATH", boot_id)
    if reset != "fallback":
        boot_id.write_text("0d7348f8-7574-4a71-9c57-f72d3fccc269", encoding="ascii")
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await budget.async_initialize()
    clock.elapsed += _WINDOW
    assert await _claim(budget, _PRM, _DAY)
    for _ in range(3):
        await _admit(budget)
    async with budget.request(_OTHER_PRM) as mark_cooldown:
        mark_cooldown(2 * _WINDOW)
    clock.elapsed += _WINDOW / 2
    assert await _claim(budget, _THIRD_PRM, _DAY)

    if reset == "boot":
        boot_id.write_text("ef65b0e4-3825-4a83-b718-1d31ff366ff1", encoding="ascii")
    elif reset == "fallback":
        monkeypatch.setattr(budget_module, "_PROCESS_BOOT_ID", "process:new")
    # For changed IDs, even a larger uptime belongs to a different monotonic timeline.
    clock.elapsed = 1.0 if reset == "regression" else 10.0 * _WINDOW
    reopened = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    with pytest.raises(ConsoBudgetError, match="not initialized for this boot"):
        await reopened.async_get_status(_PRM, required=1)
    await reopened.async_initialize()
    assert not await _claim(reopened, _PRM, _DAY)
    with pytest.raises(ConsoBudgetExceeded, match="budget initialization"):
        await _admit(reopened)

    clock.elapsed += _WINDOW
    for _ in range(3):
        await _admit(reopened)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(reopened)
    clock.elapsed += _WINDOW / 2 - 1
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await _claim(reopened, _OTHER_PRM, _DAY)
    clock.elapsed += 1
    assert await _claim(reopened, _OTHER_PRM, _DAY)
    await _admit(reopened, _OTHER_PRM)


async def test_insufficient_slots_do_not_claim_the_day_or_wait(ready, clock, monkeypatch):
    _, budget = ready
    for _ in range(2):
        await _admit(budget)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _claim(budget, _PRM, _DAY, required=2)
    await _admit(budget)

    async def unexpected_sleep(_):
        pytest.fail("A request without capacity waited for spacing")

    monkeypatch.setattr(budget_module, "sleep", unexpected_sleep)
    with pytest.raises(ConsoBudgetExceeded, match="limit reached"):
        await _admit(budget)
    clock.elapsed += _WINDOW
    assert await _claim(budget, _PRM, _DAY, required=2)
    assert not await _claim(budget, _PRM, _DAY, required=3)


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
    with pytest.raises(ConsoBudgetExceeded, match="paused") as error:
        await _admit(reopened)
    assert error.value.retry_in == 1
    clock.elapsed += 1
    await _admit(reopened)


async def test_failed_atomic_save_is_detected_before_admitting_or_claiming(ready, monkeypatch):
    _, budget = ready

    def fail_write(*args, **kwargs):
        raise storage.WriteError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "write_utf8_file_atomic", fail_write)
        with pytest.raises(ConsoBudgetError, match="on disk"):
            await _claim(budget, _PRM, _DAY)
        with pytest.raises(ConsoBudgetError, match="on disk"):
            async with budget.request(_PRM):
                pytest.fail("Admitted a request without a durable reservation")
    assert await _claim(budget, _PRM, _DAY)
    for _ in range(3):
        await _admit(budget)


async def test_failed_cooldown_write_preserves_the_error_and_blocks_on_restart(
    tmp_path, ready, clock, monkeypatch
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
    status = await reopened.async_get_status(_PRM, required=1)
    assert status["last_daily_attempt"] is None
    assert not status["can_refresh"]
    assert status["requests_remaining"] == 2
    assert status["retry_in_seconds"] is None
    clock.elapsed += 17  # The read did not start the pending request's cooldown.
    with pytest.raises(ConsoBudgetExceeded, match="paused") as error:
        await _admit(reopened)
    assert error.value.retry_in == _WINDOW
    clock.elapsed += 17
    with pytest.raises(ConsoBudgetExceeded, match="paused") as error:
        await _admit(reopened)
    assert error.value.retry_in == _WINDOW - 17


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
    claim = create_task(_claim(budget, _PRM, _DAY))
    await saving.wait()
    claim.cancel()
    with pytest.raises(CancelledError):
        await claim
    duplicate = create_task(_claim(budget, _PRM, _DAY))
    await sleep(0)
    assert not duplicate.done()
    finish.set()
    assert not await duplicate
