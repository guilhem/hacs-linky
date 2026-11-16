"""Verify offline parsing and coverage of Linky consumption exports."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.linky.conso_csv import read_conso_csv

_PRM = "12345678901234"
_FILENAME = f"historique_conso_{_PRM}_2026-11-16.csv"
_HEADER = "debut;fin;kW"


def _write_csv(directory: Path, *rows: str, name: str = _FILENAME) -> Path:
    path = directory / name
    path.write_bytes("\r\n".join((_HEADER, *rows, "")).encode("utf-8-sig"))
    return path


def test_explicit_offsets_distinguish_both_autumn_hours(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-10-25T02:00+01:00;2026-10-25T03:00+01:00;"2,5"',
        '2026-10-25T02:00+02:00;2026-10-25T02:00+01:00;"1,25"',
        '2026-10-25T00:00Z;2026-10-25T01:00Z;"1,250"',
        name=_FILENAME.replace(".csv", " (1).csv"),
    )
    assert read_conso_csv(path, _PRM) == (
        {
            datetime(2026, 10, 25, 0, tzinfo=UTC): 1.25,
            datetime(2026, 10, 25, 1, tzinfo=UTC): 2.5,
        },
        [],
    )


@pytest.mark.parametrize(("day", "duplicate_power"), [("2025-10-26", "1,0"), ("2026-10-25", "2,0")])
def test_naive_autumn_export_with_duplicate_half_hours(
    tmp_path: Path, day: str, duplicate_power: str
) -> None:
    path = _write_csv(
        tmp_path,
        f'{day}T00:00;{day}T01:00;"1"',
        f'{day}T01:00;{day}T01:30;"1"',
        f'{day}T01:30;{day}T02:00;"1,0"',
        f'{day}T01:30;{day}T02:00;"{duplicate_power}"',
        f'{day}T02:00;{day}T02:30;"1,0"',
        f'{day}T02:00;{day}T02:30;"{duplicate_power}"',
        f'{day}T02:30;{day}T03:00;"1"',
        f'{day}T03:00;{day}T04:00;"1"',
    )
    buckets, incomplete = read_conso_csv(path, _PRM)
    assert sorted(buckets) == [
        datetime.fromisoformat(f"{day}T00:00+02:00").astimezone(UTC),
        datetime.fromisoformat(f"{day}T03:00+01:00").astimezone(UTC),
    ]
    assert incomplete == [f"{day}T01:00:00+02:00", f"{day}T02:00:00+02:00", f"{day}T02:00:00+01:00"]


def test_ambiguity_invalidates_both_folds_even_with_explicit_coverage(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-10-25T02:00+02:00;2026-10-25T02:00+01:00;"1"',
        '2026-10-25T02:00+01:00;2026-10-25T03:00+01:00;"1"',
        '2026-10-25T02:00;2026-10-25T02:30;"1"',
    )
    assert read_conso_csv(path, _PRM) == (
        {},
        ["2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00"],
    )


@pytest.mark.parametrize("day", ["2025-03-30", "2026-03-29"])
def test_naive_spring_export_skips_nonexistent_times(tmp_path: Path, day: str) -> None:
    path = _write_csv(
        tmp_path,
        f'{day}T01:00;{day}T01:30;"1"',
        f'{day}T01:30;{day}T02:00;"1"',
        f'{day}T02:00;{day}T02:30;"1"',
        f'{day}T02:30;{day}T03:00;"1"',
        f'{day}T03:00;{day}T03:30;"1,5"',
        f'{day}T03:30;{day}T04:00;"2,5"',
    )
    assert read_conso_csv(path, _PRM) == (
        {datetime.fromisoformat(f"{day}T01:00Z"): 2.0},
        [f"{day}T01:00:00+01:00"],
    )


@pytest.mark.parametrize(
    "skipped",
    [
        '2026-03-29T01:30;2026-03-29T02:00;"null"',
        '2026-03-29T02:30;2026-03-29T03:00;"null"',
    ],
)
def test_spring_gap_is_reported_at_export_boundary(tmp_path: Path, skipped: str) -> None:
    path = _write_csv(tmp_path, skipped, '2026-03-29T03:00;2026-03-29T04:00;"2"')
    assert read_conso_csv(path, _PRM) == (
        {datetime(2026, 3, 29, 1, tzinfo=UTC): 2.0},
        ["2026-03-29T01:00:00+01:00"],
    )


def test_spring_gap_does_not_discard_an_hour_with_explicit_coverage(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-03-29T01:00+01:00;2026-03-29T03:00+02:00;"1,25"',
        '2026-03-29T02:30;2026-03-29T03:00;"null"',
    )
    assert read_conso_csv(path, _PRM) == (
        {datetime(2026, 3, 29, 0, tzinfo=UTC): 1.25},
        [],
    )


def test_midnight_interval_partial_edges_and_internal_gap(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-01-02T01:00;2026-01-02T01:30;"1"',
        '2026-01-01T22:30;2026-01-01T23:00;"1"',
        '2026-01-01T23:00;2026-01-02T00:00;"1,5"',
    )
    assert read_conso_csv(path, _PRM) == (
        {datetime(2026, 1, 1, 22, tzinfo=UTC): 1.5},
        [
            "2026-01-01T22:00:00+01:00",
            "2026-01-02T00:00:00+01:00",
            "2026-01-02T01:00:00+01:00",
        ],
    )


def test_exported_nulls_leave_gaps_and_zero_is_a_measurement(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-01-02T00:00;2026-01-02T01:00;"null"',
        '2026-01-02T01:00;2026-01-02T01:30;"0"',
        '2026-01-02T01:30;2026-01-02T02:00;"0"',
        '2026-01-02T02:00;2026-01-02T02:30;"1,2"',
        '2026-01-02T02:30;2026-01-02T03:00;"null"',
    )
    assert read_conso_csv(path, _PRM) == (
        {datetime(2026, 1, 2, 0, tzinfo=UTC): 0.0},
        ["2026-01-02T00:00:00+01:00", "2026-01-02T02:00:00+01:00"],
    )


def test_conflicting_aware_duplicates_leave_hour_incomplete(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-01-01T00:00Z;2026-01-01T01:00Z;"1"',
        '2026-01-01T00:00Z;2026-01-01T01:00Z;"2"',
    )
    assert read_conso_csv(path, _PRM) == ({}, ["2026-01-01T01:00:00+01:00"])


@pytest.mark.parametrize(
    ("name", "prm", "message"),
    [
        (_FILENAME, "98765432109876", "does not match"),
        (_FILENAME.replace("_conso_", "_prod_"), _PRM, "consumption CSV filename"),
        (_FILENAME.replace("11-16", "11-31"), _PRM, "Invalid Linky CSV"),
        (_FILENAME, _PRM, "Could not read"),
    ],
)
def test_invalid_filename_meter_or_missing_file(
    tmp_path: Path, name: str, prm: str, message: str
) -> None:
    with pytest.raises(ServiceValidationError, match=message) as error:
        read_conso_csv(tmp_path / name, prm)
    assert name not in str(error.value)
    assert _PRM not in str(error.value)
    assert prm not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b"debut;fin;W\n",
        b"debut;fin;kW\n\xff",
        b'debut;fin;kW\n2026-01-01T00:00;2026-01-01T01:00;"1',
        b"debut;fin;kW\n2026-01-01;2026-01-01T01:00;1\n",
        b"debut;fin;kW\n2026-01-01T00:00;2026-01-01T02:00;1\n",
        b"debut;fin;kW\n2026-01-01T00:00;2026-01-01T01:00;1 kW\n",
        b"debut;fin;kW\n2026-01-01T00:00;2026-01-01T01:00;1e999\n",
    ],
)
def test_reject_malformed_csv(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / _FILENAME
    path.write_bytes(content)
    with pytest.raises(ServiceValidationError, match="Invalid Linky CSV"):
        read_conso_csv(path, _PRM)


def test_reject_empty_csv(tmp_path: Path) -> None:
    with pytest.raises(ServiceValidationError, match="no readings"):
        read_conso_csv(_write_csv(tmp_path), _PRM)


def test_reject_excessive_coverage(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        '2026-01-01T00:00;2026-01-01T01:00;"1"',
        '2022-01-01T00:00;2022-01-01T01:00;"1"',
    )
    with pytest.raises(ServiceValidationError, match="covers more than"):
        read_conso_csv(path, _PRM)


def test_reject_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / _FILENAME
    with path.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ServiceValidationError, match="exceeds 8 MiB"):
        read_conso_csv(path, _PRM)
