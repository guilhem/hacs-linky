"""Verify measurement buckets and cumulative sums in Recorder."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from threading import Event
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.statistics import (
    import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.linky.api import IntervalReading, MeteringData
from custom_components.linky.const import PARIS_TZ
from custom_components.linky.statistics import (
    async_write_statistics,
    daily_energy,
    daily_from_hourly,
    hourly_energy,
    paris_midnight,
)

_PRM = "12345678901234"
_HALF_HOUR = timedelta(minutes=30)
_DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("day", "naive", "expected_hours", "expected_kwh"),
    [
        (date(2024, 1, 1), False, 24, 24.0),
        (date(2024, 3, 31), True, 23, 23.0),
        (date(2024, 10, 27), False, 25, 26.0),
        (date(2024, 10, 27), True, 22, None),
    ],
)
def test_daily_from_curve(
    day: date, naive: bool, expected_hours: int, expected_kwh: float | None
) -> None:
    """Only complete Paris days contribute to daily totals, including across DST."""
    end = day + _DAY
    lower, upper = paris_midnight(day), paris_midnight(end)
    readings = []
    for slot in range(1, 1 + (upper - lower) // _HALF_HOUR):
        instant = lower + slot * _HALF_HOUR
        local = instant.astimezone(PARIS_TZ)
        value = 2000 if local.fold else 1000
        stamp = local.replace(tzinfo=None) if naive else datetime.fromisoformat(local.isoformat())
        readings.append(IntervalReading(stamp, value, _HALF_HOUR))

    data = MeteringData(_PRM, day, end, list(reversed(readings)), "W")
    buckets = hourly_energy(data, day, end)
    assert len(buckets) == expected_hours
    assert upper - timedelta(hours=1) in buckets  # midnight is the end of the final interval
    if expected_hours == 25:
        assert buckets[datetime(2024, 10, 27, 0, tzinfo=UTC)] == 1.5
        assert buckets[datetime(2024, 10, 27, 1, tzinfo=UTC)] == 1.5

    assert daily_from_hourly(buckets, day, end) == (
        {} if expected_kwh is None else {lower: expected_kwh}
    )
    if expected_kwh is not None:
        for incomplete in (readings[1:], [*readings, readings[0]]):
            buckets = hourly_energy(MeteringData(_PRM, day, end, incomplete, "W"), day, end)
            assert daily_from_hourly(buckets, day, end) == {}


def test_only_complete_measured_hours() -> None:
    """Skip duplicates, bad duration, absent halves and hours outside the range."""
    day = date(2024, 1, 1)
    midnight = paris_midnight(day)

    def reading(slot: int, value: float = 500, duration: timedelta = _HALF_HOUR) -> IntervalReading:
        return IntervalReading(midnight + slot * _HALF_HOUR, value, duration)

    data = MeteringData(
        _PRM,
        day,
        day + _DAY,
        [
            reading(-1),
            reading(0),
            reading(1),
            reading(2),
            reading(3),
            reading(4),
            reading(4),
            reading(5),
            reading(6, duration=timedelta(minutes=15)),
            reading(7),
            reading(9, 0),
            reading(10, 0),
        ],
        "W",
    )
    assert hourly_energy(data, day, day + _DAY) == {
        midnight: 0.5,
        midnight + timedelta(hours=4): 0,
    }


def test_hourly_energy_uses_reported_interval_lengths() -> None:
    """Integrate V2 20-, 30- and 60-minute intervals without splitting an average."""
    day = date(2024, 1, 1)
    midnight = paris_midnight(day)
    readings = [
        IntervalReading(midnight + timedelta(minutes=end), watts, timedelta(minutes=minutes))
        for end, minutes, watts in (
            (20, 20, 300),
            (40, 20, 600),
            (60, 20, 900),
            (120, 60, 0),
            (150, 30, 1000),
            (180, 30, 3000),
            (240, 60, 1000),
            (260, 20, 2000),
            (300, 60, 1000),
            (360, 60, 1500),
        )
    ]
    data = MeteringData(_PRM, day, day + _DAY, list(reversed(readings)), "W")
    # The 04:00–04:20 reading overlaps the 04:00–05:00 reading.
    assert hourly_energy(data, day, day + _DAY) == pytest.approx(
        {
            midnight: 0.6,
            midnight + timedelta(hours=1): 0,
            midnight + timedelta(hours=2): 2,
            midnight + timedelta(hours=3): 1,
            midnight + timedelta(hours=5): 1.5,
        }
    )

    # A 03:40–04:40 average cannot be allocated accurately to either hour.
    readings.append(IntervalReading(midnight + timedelta(minutes=280), 600, timedelta(hours=1)))
    data = MeteringData(_PRM, day, day + _DAY, readings, "W")
    assert hourly_energy(data, day, day + _DAY) == pytest.approx(
        {
            midnight: 0.6,
            midnight + timedelta(hours=1): 0,
            midnight + timedelta(hours=2): 2,
            midnight + timedelta(hours=5): 1.5,
        }
    )


def test_daily_energy_is_measured_and_at_paris_midnight() -> None:
    """A daily value has its own bucket; ambiguous duplicate dates are omitted."""
    day = date(2024, 3, 31)
    end = day + 4 * _DAY
    readings = [
        IntervalReading(day - _DAY, 9999),
        IntervalReading(day, 0),
        IntervalReading(day + _DAY, 4321),
        IntervalReading(day + 2 * _DAY, 1000),
        IntervalReading(day + 2 * _DAY, 3000),
        IntervalReading(end, 9999),
    ]
    assert daily_energy(MeteringData(_PRM, day, end, readings, "Wh"), day, end) == {
        datetime(2024, 3, 30, 23, tzinfo=UTC): 0,
        datetime(2024, 3, 31, 22, tzinfo=UTC): 4.321,
    }
    with pytest.raises(ValueError, match="Wh"):
        daily_energy(MeteringData(_PRM, day, end, readings, "W"), day, end)


@pytest.mark.parametrize("resolution", ["hourly", "daily"])
async def test_recorder_insert_revise_and_replay(
    hass: HomeAssistant, recorder_mock: Recorder, resolution: str
) -> None:
    """Preserve missing rows, insert historic data, and amend subsequent sums exactly once."""
    base = datetime(2024, 1, 1, tzinfo=UTC)
    step = timedelta(hours=1) if resolution == "hourly" else _DAY
    t = [base + i * step for i in range(9)]
    key = ("consumption", resolution)
    statistic_id = f"linky:{_PRM}_consumption_{resolution}_energy"

    async def rows():
        result = await recorder_mock.async_add_executor_job(
            statistics_during_period,
            hass,
            base,
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum", "change"},
        )
        return [
            (datetime.fromtimestamp(row["start"], UTC), row["state"], row["sum"], row["change"])
            for row in result[statistic_id]
        ]

    initial = {t[2]: 2.0, t[4]: 4.0, t[8]: 8.0}
    await async_write_statistics(hass, _PRM, {key: initial})
    assert await rows() == [(t[2], 2, 2, 2), (t[4], 4, 6, 4), (t[8], 8, 14, 8)]

    await async_write_statistics(hass, _PRM, {key: {t[0]: 1.0, t[4]: 0.0}})
    assert await rows() == [
        (t[0], 1, 1, 1),
        (t[2], 2, 3, 2),
        (t[4], 0, 3, 0),
        (t[8], 8, 11, 8),
    ]

    # The closest previous bucket is two periods away. Missing observations
    # must not be interpreted as zero or cause the predecessor's sum to be lost.
    update = {t[6]: 3.0}
    await async_write_statistics(hass, _PRM, {key: update})
    expected = [
        (t[0], 1, 1, 1),
        (t[2], 2, 3, 2),
        (t[4], 0, 3, 0),
        (t[6], 3, 6, 3),
        (t[8], 8, 14, 8),
    ]
    assert await rows() == expected
    with patch("custom_components.linky.statistics.import_statistics") as write:
        await async_write_statistics(hass, _PRM, {key: update})
        write.assert_not_called()
    assert await rows() == expected


async def test_recorder_reports_missing_write(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    """A queue drain alone is not evidence that Recorder committed a write."""
    with (
        patch("custom_components.linky.statistics.import_statistics", return_value=True),
        pytest.raises(HomeAssistantError, match="Recorder did not confirm"),
    ):
        await async_write_statistics(
            hass, _PRM, {("production", "hourly"): {datetime(2024, 1, 1, tzinfo=UTC): 0}}
        )


async def test_recorder_failed_batch_cannot_replay_over_a_newer_write(
    hass: HomeAssistant, recorder_mock: Recorder
) -> None:
    """Fail an old batch as the recorder would on a retryable DB error."""
    old_started = asyncio.Event()
    resume_recorder = Event()
    attempts = []
    key = ("consumption", "hourly")
    base = datetime(2024, 1, 1, tzinfo=UTC)
    next_hour = base + timedelta(hours=1)

    def write(recorder, metadata, stats, table):
        states = tuple(stat["state"] for stat in stats)
        attempts.append(states)
        if len(attempts) == 1:
            hass.loop.call_soon_threadsafe(old_started.set)
            assert resume_recorder.wait(5)
            return False
        return import_statistics(recorder, metadata, stats, table)

    with patch("custom_components.linky.statistics.import_statistics", side_effect=write):
        old = asyncio.create_task(async_write_statistics(hass, _PRM, {key: {base: 4.0}}))
        try:
            async with asyncio.timeout(3):
                await old_started.wait()
            superseded = asyncio.create_task(async_write_statistics(hass, _PRM, {key: {base: 6.0}}))
            await asyncio.sleep(0)
            superseded.cancel()
            with pytest.raises(asyncio.CancelledError):
                await superseded

            newest = {key: {base: 8.0, next_hour: 2.0}}
            new = asyncio.create_task(async_write_statistics(hass, _PRM, newest))
            await asyncio.sleep(0)
        finally:
            resume_recorder.set()

        with pytest.raises(HomeAssistantError, match="Temporary Recorder failure"):
            await old
        await new
        await async_write_statistics(hass, _PRM, newest)  # Drain any stale replay before asserting.

    statistic_id = f"linky:{_PRM}_consumption_hourly_energy"
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period, hass, base, None, {statistic_id}, "hour", None, {"state", "sum"}
    )
    assert attempts == [(4.0,), (8.0, 2.0)]
    assert [(row["state"], row["sum"]) for row in rows[statistic_id]] == [(8.0, 8.0), (2.0, 10.0)]
