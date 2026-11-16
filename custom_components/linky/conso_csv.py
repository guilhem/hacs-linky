"""Read offline Linky consumption exports."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from math import isfinite
from pathlib import Path

from homeassistant.exceptions import ServiceValidationError

from .api import IntervalReading, MeteringData
from .const import PARIS_TZ
from .statistics import Buckets, _utc_candidates, hourly_energy

_FILENAME = re.compile(
    r"historique_conso_([0-9]{14})_([0-9]{4}-[0-9]{2}-[0-9]{2})(?: \([0-9]+\))?\.csv"
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:[.,][0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)?"
)
_POWER = re.compile(r"[0-9]+(?:[.,][0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MAX_BYTES = 8 * 1024 * 1024
_MAX_SPAN = timedelta(days=3 * 366)
_HOUR = timedelta(hours=1)


def _hour(instant: datetime) -> datetime:
    return instant.replace(minute=0, second=0, microsecond=0)


def _parse_rows(rows: Iterator[list[str]], prm: str) -> tuple[Buckets, list[str]]:
    if next(rows, None) != ["debut", "fin", "kW"]:
        raise ValueError

    first, last = datetime.max.replace(tzinfo=UTC), datetime.min.replace(tzinfo=UTC)
    intervals: dict[tuple[datetime, datetime], tuple[Decimal, bool]] = {}
    invalid_hours: set[datetime] = set()
    for debut, fin, raw_power in rows:
        if not (_TIMESTAMP.fullmatch(debut) and _TIMESTAMP.fullmatch(fin)):
            raise ValueError
        power = None
        if raw_power != "null":
            if len(raw_power) > 64 or not _POWER.fullmatch(raw_power):
                raise ValueError
            power = Decimal(raw_power.replace(",", "."))
            if not isfinite(float(power) * 1000):
                raise ValueError

        begin, finish = datetime.fromisoformat(debut), datetime.fromisoformat(fin)
        starts, ends = _utc_candidates(begin), _utc_candidates(finish)
        if not starts or not ends:
            # Keep the real hours next to an impossible spring timestamp reportable.
            for instant in starts | {end - datetime.resolution for end in ends}:
                hour = _hour(instant)
                first, last = min(first, hour), max(last, hour + _HOUR)
            continue
        aware = begin.tzinfo is not None and finish.tzinfo is not None
        candidates = [
            (start, end) for start in starts for end in ends if timedelta(0) < end - start <= _HOUR
        ]
        if not candidates:
            raise ValueError

        for start, end in candidates:
            first, last = min(first, start), max(last, end)
            if last - first > _MAX_SPAN:
                raise ServiceValidationError("Linky CSV covers more than 1098 days")
            if power is not None and len(candidates) == 1:
                key = (start, end)
                previous = intervals.get(key)
                if previous is None:
                    intervals[key] = (power, aware)
                    continue
                if aware and previous == (power, True):
                    continue
            # A missing, conflicting or ambiguous reading cannot complete an hour.
            invalid_hours.update((_hour(start), _hour(end - datetime.resolution)))

    if first >= last:
        raise ServiceValidationError("Linky CSV contains no readings")
    if last - first > _MAX_SPAN:
        raise ServiceValidationError("Linky CSV covers more than 1098 days")

    final_instant = last - datetime.resolution
    first_day = first.astimezone(PARIS_TZ).date()
    end_day = final_instant.astimezone(PARIS_TZ).date() + date.resolution
    readings = [
        IntervalReading(end, float(power) * 1000, end - start)
        for (start, end), (power, _) in intervals.items()
    ]
    buckets = hourly_energy(
        MeteringData(prm, first_day, end_day, readings, "W"), first_day, end_day
    )
    for hour in invalid_hours:
        buckets.pop(hour, None)
    first_hour = _hour(first)
    incomplete = [
        hour.astimezone(PARIS_TZ).isoformat()
        for index in range((final_instant - first_hour) // _HOUR + 1)
        if (hour := first_hour + index * _HOUR) not in buckets
    ]
    return buckets, incomplete


def read_conso_csv(path: Path, prm: str) -> tuple[Buckets, list[str]]:
    """Return complete UTC hourly kWh and incomplete hours in Paris time."""
    match = _FILENAME.fullmatch(path.name)
    if match is None:
        raise ServiceValidationError("Expected a Linky consumption CSV filename")
    if match[1] != prm:
        raise ServiceValidationError("Linky CSV does not match the selected meter")

    try:
        date.fromisoformat(match[2])
        if not path.is_file():
            raise OSError
        with path.open("rb") as source:
            content = source.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise ServiceValidationError("Linky CSV exceeds 8 MiB")
        with StringIO(content.decode("utf-8-sig"), newline="") as stream:
            return _parse_rows(csv.reader(stream, delimiter=";", strict=True), prm)
    except OSError:
        raise ServiceValidationError("Could not read the Linky CSV file") from None
    except ValueError, OverflowError, InvalidOperation, csv.Error:
        raise ServiceValidationError("Invalid Linky CSV format or measurement") from None
