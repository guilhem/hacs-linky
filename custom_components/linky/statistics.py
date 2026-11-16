"""Convert Linky measurements to Home Assistant Energy statistics."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isclose, isfinite
from typing import TYPE_CHECKING

from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    get_metadata_with_session,
    import_statistics,
    statistic_during_period,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.recorder import get_instance, session_scope
from homeassistant.util.unit_conversion import EnergyConverter
from sqlalchemy import select

from .const import DOMAIN, PARIS_TZ

if TYPE_CHECKING:
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant

    from .api import MeteringData

type Buckets = dict[datetime, float]

_HOUR = timedelta(hours=1)


def paris_midnight(day: date) -> datetime:
    """Return the UTC start of a Paris calendar day."""
    return datetime.combine(day, time.min, PARIS_TZ).astimezone(UTC)


def _utc_candidates(value: datetime) -> set[datetime]:
    if value.utcoffset() is not None:
        return {value.astimezone(UTC)}

    # A wall time can identify two instants in autumn and none in spring.
    candidates = {value.replace(tzinfo=PARIS_TZ, fold=fold).astimezone(UTC) for fold in (0, 1)}
    return {
        instant
        for instant in candidates
        if instant.astimezone(PARIS_TZ).replace(tzinfo=None) == value
    }


def hourly_energy(data: MeteringData, start: date, end: date) -> Buckets:
    """Convert exact, nonoverlapping UTC hour coverage from mean power to kWh."""
    if data.unit != "W":
        raise ValueError("Expected a load curve in W")

    lower, upper = paris_midnight(start), paris_midnight(end)
    intervals: dict[datetime, list[tuple[datetime, datetime, float]]] = defaultdict(list)
    invalid_hours: set[datetime] = set()
    for reading in data.interval_reading:
        duration = reading.duration
        if (
            not isinstance(reading.date, datetime)
            or duration is None
            or not timedelta(0) < duration <= _HOUR
        ):
            continue

        ends = _utc_candidates(reading.date)
        for interval_end in ends:
            interval_start = interval_end - duration
            if interval_end <= lower or interval_start >= upper:
                continue
            hour = interval_start.replace(minute=0, second=0, microsecond=0)
            if interval_end > hour + _HOUR:
                # Mean power over the entire interval cannot tell us how
                # much energy belongs on either side of the hour boundary.
                invalid_hours.update((hour, hour + _HOUR))
                continue
            if len(ends) != 1 or not isfinite(reading.value) or reading.value < 0:
                invalid_hours.add(hour)
                continue
            energy = reading.value / 1000 * (duration / _HOUR)
            intervals[hour].append((interval_start, interval_end, energy))

    buckets: Buckets = {}
    for hour, readings in intervals.items():
        if hour in invalid_hours:
            continue
        cursor = hour
        energy = 0.0
        for interval_start, interval_end, measured in sorted(readings):
            if interval_start != cursor:
                break  # A gap, duplicate or overlap prevents exact coverage.
            cursor = interval_end
            energy += measured
        else:
            if cursor == hour + _HOUR:
                buckets[hour] = energy
    return buckets


def daily_from_hourly(hourly: Buckets, start: date, end: date) -> Buckets:
    """Sum complete Paris days of hourly kWh, indexed by midnight in UTC."""
    daily: Buckets = {}
    for offset in range((end - start).days):
        day = start + timedelta(days=offset)
        lower = paris_midnight(day)
        hours = (paris_midnight(day + timedelta(days=1)) - lower) // _HOUR
        try:
            daily[lower] = sum(hourly[lower + hour * _HOUR] for hour in range(hours))
        except KeyError:
            continue
    return daily


def daily_energy(data: MeteringData, start: date, end: date) -> Buckets:
    """Store measured daily energy at the start of the Paris calendar day."""
    if data.unit != "Wh":
        raise ValueError("Expected daily energy in Wh")

    days: Buckets = {}
    invalid_days: set[datetime] = set()
    for reading in data.interval_reading:
        day = reading.date
        if not isinstance(day, date) or isinstance(day, datetime) or not start <= day < end:
            continue
        midnight = paris_midnight(day)
        if midnight in days or not isfinite(reading.value) or reading.value < 0:
            invalid_days.add(midnight)
        else:
            days[midnight] = reading.value / 1000

    return {midnight: value for midnight, value in days.items() if midnight not in invalid_days}


def _read_rows(
    hass: HomeAssistant, start: datetime, statistic_id: str
) -> dict[datetime, StatisticsRow]:
    rows = statistics_during_period(
        hass, start, None, {statistic_id}, "hour", None, {"state", "sum"}
    )
    return {datetime.fromtimestamp(row["start"], UTC): row for row in rows.get(statistic_id, ())}


def _build_suffix(hass: HomeAssistant, statistic_id: str, measured: Buckets) -> list[StatisticData]:
    first = min(measured)
    existing = _read_rows(hass, first, statistic_id)
    changed = [
        start for start, value in measured.items() if existing.get(start, {}).get("state") != value
    ]
    if not changed:
        return []

    first_changed = min(changed)
    preceding = max((start for start in existing if start < first_changed), default=None)
    if preceding is None:
        # With an open start, Recorder's change is the absolute sum of its final
        # row before the exclusive end, even if there is a gap before that end.
        predecessor = statistic_during_period(
            hass, None, first_changed, statistic_id, {"change"}, None
        )
        total = predecessor.get("change")
        if total is None:
            total = 0.0
    else:
        total = existing[preceding].get("sum")
        if total is None:
            raise HomeAssistantError("Linky statistics are missing a preceding cumulative sum")

    result: list[StatisticData] = []
    for start in sorted(measured.keys() | existing.keys()):
        if start < first_changed:
            continue
        row = existing.get(start, {})
        state = measured[start] if start in measured else row.get("state")
        if state is None:
            raise HomeAssistantError("Linky statistics are missing a stored measurement")
        total += state
        if row.get("state") != state or row.get("sum") != total:
            result.append(StatisticData(start=start, state=state, sum=total))

    return result


def _build_fill(
    recorder: Recorder, statistic_id: str, measured: Buckets, counts: dict[str, int]
) -> list[StatisticData]:
    """Add missing hours and shift stored sums by only the added energy."""
    query = (
        select(
            Statistics.start_ts,
            Statistics.state,
            Statistics.sum,
            Statistics.mean,
            Statistics.min,
            Statistics.max,
            Statistics.last_reset_ts,
        )
        .join(StatisticsMeta, Statistics.metadata_id == StatisticsMeta.id)
        .where(StatisticsMeta.statistic_id == statistic_id)
    )
    with session_scope(session=recorder.get_session(), read_only=True) as session:
        stored = get_metadata_with_session(recorder, session, statistic_ids={statistic_id})
        if statistic_id in stored:
            unit = stored[statistic_id][1]["unit_of_measurement"]
            if unit != UnitOfEnergy.KILO_WATT_HOUR:
                raise HomeAssistantError(
                    f"Linky statistics use unit {unit!r}; expected kWh for CSV import"
                )
        since = min(measured).timestamp()
        existing = {
            datetime.fromtimestamp(row.start_ts, UTC): row
            for row in session.execute(query.where(Statistics.start_ts >= since))
        }
        missing: Buckets = {}
        for start, value in measured.items():
            if (row := existing.get(start)) is None:
                missing[start] = value
            elif row.state is not None and isclose(row.state, value, rel_tol=0, abs_tol=1e-9):
                counts["identical"] += 1
            else:
                counts["conflicts"] += 1
        if not missing:
            return []

        first = min(missing)
        preceding = max((start for start in existing if start < first), default=None)
        predecessor = (
            existing[preceding]
            if preceding is not None
            else session.execute(
                query.where(Statistics.start_ts < first.timestamp())
                .order_by(Statistics.start_ts.desc())
                .limit(1)
            ).first()
        )

    if predecessor is not None:
        base = predecessor.sum
        if base is None or not isfinite(base):
            raise HomeAssistantError("Linky statistics have an invalid preceding cumulative sum")
    else:
        # Recorder measures the first change from zero, including any adjustment.
        base = 0.0

    result: list[StatisticData] = []
    added_energy = 0.0
    for start in sorted(missing.keys() | existing.keys()):
        if start < first:
            continue
        if (row := existing.get(start)) is None:
            state = missing[start]
            added_energy += state
            total = base + added_energy
            stat = StatisticData(start=start, state=state, sum=total)
        else:
            if row.sum is None or not isfinite(row.sum):
                raise HomeAssistantError("Linky statistics have an invalid stored cumulative sum")
            base = row.sum
            total = base + added_energy
            if total == base:
                continue
            # Recorder's import replaces omitted fields on existing rows.
            stat = StatisticData(
                start=start,
                state=row.state,
                sum=total,
                mean=row.mean,
                min=row.min,
                max=row.max,
                last_reset=(
                    datetime.fromtimestamp(row.last_reset_ts, UTC)
                    if row.last_reset_ts is not None
                    else None
                ),
            )
        if not isfinite(total):
            raise HomeAssistantError("Linky statistics would have a non-finite cumulative sum")
        result.append(stat)

    counts["added"] = len(missing)
    return result


@dataclass(slots=True)
class _WriteStatisticsTask(RecorderTask):
    """Read, upsert and verify each series together in Recorder's queue."""

    prm: str
    series: Mapping[tuple[str, str], Buckets]
    completion: Future[dict[str, int]]
    fill_only: bool

    def run(self, recorder: Recorder) -> None:
        """Atomically claim the task unless its caller cancelled it while queued."""
        if not self.completion.set_running_or_notify_cancel():
            return

        try:
            counts = self._write(recorder)
        except Exception as err:
            self.completion.set_exception(err)
        else:
            self.completion.set_result(counts)

    def _write(self, recorder: Recorder) -> dict[str, int]:
        hass = recorder.hass
        counts = {"added": 0, "identical": 0, "conflicts": 0}
        for (direction, resolution), measured in self.series.items():
            if not measured:
                continue
            statistic_id = f"{DOMAIN}:{self.prm}_{direction}_{resolution}_energy"
            stats = (
                _build_fill(recorder, statistic_id, measured, counts)
                if self.fill_only
                else _build_suffix(hass, statistic_id, measured)
            )
            if not stats:
                continue

            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Linky {self.prm} {resolution} {direction} (kWh)",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            # Recorder's normal import task would requeue an obsolete absolute sum on retry.
            if not import_statistics(recorder, metadata, stats, Statistics):
                raise HomeAssistantError(
                    f"Temporary Recorder failure writing Linky {resolution} {direction} statistics"
                )

            rows = _read_rows(hass, stats[0]["start"], statistic_id)
            for stat in stats:
                row = rows.get(stat["start"], {})
                if row.get("state") != stat["state"] or row.get("sum") != stat["sum"]:
                    raise HomeAssistantError(
                        f"Recorder did not confirm Linky {resolution} {direction} statistics"
                    )
        return counts


async def async_write_statistics(
    hass: HomeAssistant, prm: str, series: Mapping[tuple[str, str], Buckets]
) -> None:
    """Upsert measured buckets and confirm all new cumulative sums in Recorder."""
    await _async_queue_statistics(hass, prm, series, fill_only=False)


async def async_fill_hourly_consumption(
    hass: HomeAssistant, prm: str, measured: Buckets
) -> dict[str, int]:
    """Fill missing hourly consumption and classify overlaps without replacing them."""
    return await _async_queue_statistics(
        hass, prm, {("consumption", "hourly"): measured}, fill_only=True
    )


async def _async_queue_statistics(
    hass: HomeAssistant,
    prm: str,
    series: Mapping[tuple[str, str], Buckets],
    *,
    fill_only: bool,
) -> dict[str, int]:
    if not any(series.values()):
        return {"added": 0, "identical": 0, "conflicts": 0}

    try:
        recorder = get_instance(hass)
    except KeyError as err:
        raise HomeAssistantError("Home Assistant Recorder is unavailable") from err

    completion: Future[dict[str, int]] = Future()
    recorder.queue_task(_WriteStatisticsTask(prm, series, completion, fill_only))
    try:
        async with asyncio.timeout(60):
            return await asyncio.wrap_future(completion)
    except TimeoutError as err:
        raise HomeAssistantError("Timed out waiting for Linky statistics in Recorder") from err
    finally:
        # A running task cannot be interrupted, but a later request still runs after it.
        completion.cancel()
