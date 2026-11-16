"""Async client for Conso API's Enedis Mesure synchrone V2 endpoints."""

from __future__ import annotations

import re
from asyncio import sleep
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any

import jwt
from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout, ContentTypeError

from .const import API_REQUEST_DELAY, PARIS_TZ, USER_AGENT

_BASE_URL = "https://conso.boris.sh/api"
_TIMEOUT = ClientTimeout(total=20)
_ENEDIS_ERROR = "The Enedis API returned an error"
_PRM = re.compile(r"[0-9]{14}")
_JWT = re.compile(r"(?:[A-Za-z0-9_-]+\.){2}[A-Za-z0-9_-]+")
_MINUTES = re.compile(r"PT([1-9][0-9]?)M")
_MAX_CURVE_DAYS = 7


class APIError(Exception):
    """The provider request failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(APIError):
    """Conso API rejected the token or its access to the requested meter."""


class InvalidTokenError(AuthenticationError):
    """The token cannot be used to select a meter."""


class PRMAccessError(APIError):
    """The selected PRM is absent from the token's meter list."""


class NoDataError(APIError):
    """The selected meter has no readings for this resource and period."""


class HistoryLimitError(APIError):
    """The requested start predates activation or Enedis's retention limit."""


@dataclass(frozen=True)
class IntervalReading:
    """A reading on a French date, at Paris wall time, or at a known UTC instant."""

    date: date | datetime
    value: int | float
    duration: timedelta | None = None


@dataclass(frozen=True)
class MeteringData:
    """Measurements for a single meter, including the provider's unit."""

    usage_point_id: str
    start: date
    end: date
    interval_reading: list[IntervalReading]
    unit: str
    quality: str | None = None


async def fetch_recent(
    read: Callable[[date, date], Awaitable[MeteringData]], start: date, end: date
) -> MeteringData:
    """Read a polling window, shortening it only after an Enedis history-limit error."""
    result: MeteringData | APIError
    try:
        return await read(start, end)
    except HistoryLimitError as err:
        result = err

    # Search the last week for the longest valid suffix without guessing the activation date.
    low, high = 1, min(_MAX_CURVE_DAYS, (end - start).days - 1)
    while low <= high:
        days = (low + high) // 2
        await sleep(API_REQUEST_DELAY)
        try:
            result = await read(end - timedelta(days=days), end)
        except HistoryLimitError:
            high = days - 1
            continue
        except NoDataError as err:
            # Enedis accepted this range; an earlier start may still have readings.
            result = err
        low = days + 1

    if isinstance(result, APIError):
        raise result
    return result


def _request_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return (value.astimezone(PARIS_TZ) if value.tzinfo else value).date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _reading_time(value: str) -> datetime:
    if len(value) <= 10:
        raise ValueError
    dt = datetime.fromisoformat(value)
    # An offsetless reading cannot distinguish the two folds of an autumn hour.
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt


def _parse_value(value: Any) -> int | float:
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            value = float(value)
    if type(value) is float and isfinite(value):
        return value
    raise ValueError


def _parse_data(payload: Any, prm: str, business: str, physical: str, unit: str) -> MeteringData:
    try:
        if payload["idPrm"] != prm:
            raise ValueError
        period = payload["periode"]
        start = date.fromisoformat(period["dateDebut"])
        end = date.fromisoformat(period["dateFin"])
        quality = payload.get("etapeMetier")
        if start >= end or (quality is not None and not isinstance(quality, str)):
            raise ValueError

        # Select only the requested quantity, even if Enedis returns other series.
        series = [
            item
            for item in payload["grandeur"]
            if (item["grandeurMetier"], item["grandeurPhysique"]) == (business, physical)
        ]
        if len(series) != 1 or series[0]["unite"] != unit:
            raise ValueError
        points = series[0]["points"]
        if not isinstance(points, list):
            raise ValueError

        readings = []
        for point in points:
            if unit == "Wh":
                timestamp = date.fromisoformat(point["d"])
            else:
                timestamp = _reading_time(point["d"])

            value = point["v"]
            if value is None or value == "null":
                continue

            duration = None
            if unit == "W" and (step := point.get("p", payload.get("pas"))) is not None:
                if not (match := _MINUTES.fullmatch(step)):
                    raise ValueError
                duration = timedelta(minutes=int(match[1]))
            readings.append(IntervalReading(timestamp, _parse_value(value), duration))
    except KeyError, TypeError, ValueError, OverflowError:
        raise APIError("Conso API returned invalid measurement data", 200) from None

    return MeteringData(prm, start, end, readings, unit, quality)


async def _check_status(response: ClientResponse) -> None:
    status = response.status
    if status == 200:
        return
    if status in (400, 401, 404):
        try:
            body = await response.json()
        except ContentTypeError, ValueError:
            body = None
        upstream = isinstance(body, dict) and body.get("message") == _ENEDIS_ERROR
        if status == 401 and not upstream:
            raise AuthenticationError("Conso API denied access to this meter", status)
        if upstream and isinstance(detail := body.get("error"), dict):
            code = detail.get("error")
            if status == 400 and code in ("ADAM-ERR0123", "ADAM-ERR0125"):
                raise HistoryLimitError("Requested data predates available meter history", status)
            if status == 404 and (
                code in ("Not Found", "no_data_found")
                or detail.get("message") == "Pas de mesure trouvée pour ce point"
            ):
                raise NoDataError("Enedis returned no measurements", status)
    raise APIError(f"Conso API request failed (HTTP {status})", status)


class LinkyClient:
    """Read meter data over Home Assistant's shared HTTP session."""

    def __init__(self, session: ClientSession, token: str, *, prm: str | None = None) -> None:
        if not isinstance(token, str) or not _JWT.fullmatch(token):
            raise InvalidTokenError("Invalid Conso API token format")
        try:
            # Decode only to discover meter identifiers. Conso verifies the JWT.
            subjects = jwt.decode(token, options={"verify_signature": False}).get("sub")
        except jwt.PyJWTError, TypeError:
            raise InvalidTokenError("Invalid Conso API token format") from None
        if (
            not isinstance(subjects, list)
            or not subjects
            or not all(isinstance(value, str) and _PRM.fullmatch(value) for value in subjects)
        ):
            raise InvalidTokenError("Invalid Conso API meter list")
        self.prms: list[str] = list(dict.fromkeys(subjects))
        if prm is not None and prm not in self.prms:
            raise PRMAccessError("The token does not list the selected meter")
        self.prm: str = self.prms[0] if prm is None else prm
        self._session = session
        self._token = token

    async def get_daily_consumption(self, start: date | str, end: date | str) -> MeteringData:
        """Fetch daily consumption in Wh."""
        return await self._get("consommation_quotidienne", start, end, "CONS", "EA", "Wh")

    async def get_daily_production(self, start: date | str, end: date | str) -> MeteringData:
        """Fetch daily production in Wh."""
        return await self._get("production_quotidienne", start, end, "PROD", "EA", "Wh")

    async def get_max_power(self, start: date | str, end: date | str) -> MeteringData:
        """Fetch daily maximum apparent power (PMA) in VA."""
        return await self._get("puissance_conso_max_quotidienne", start, end, "CONS", "PMA", "VA")

    async def get_consumption_load_curve(self, start: date | str, end: date | str) -> MeteringData:
        """Fetch average consumption power in W per measurement interval."""
        return await self._get("courbe_de_charge_consommation", start, end, "CONS", "PA", "W")

    async def get_production_load_curve(self, start: date | str, end: date | str) -> MeteringData:
        """Fetch average production power in W per measurement interval."""
        return await self._get("courbe_de_charge_production", start, end, "PROD", "PA", "W")

    async def _get(
        self,
        endpoint: str,
        start: date | str,
        end: date | str,
        business: str,
        physical: str,
        unit: str,
    ) -> MeteringData:
        try:
            start_date, end_date = _request_date(start), _request_date(end)
        except TypeError, ValueError, OverflowError:
            raise APIError("Invalid Conso API date range", 400) from None
        if start_date >= end_date:
            raise APIError("The start date must precede the exclusive end date", 400)
        if unit == "W" and (end_date - start_date).days > _MAX_CURVE_DAYS:
            raise APIError("A load curve request can span at most seven days", 400)

        try:
            async with self._session.get(
                f"{_BASE_URL}/{endpoint}",
                params={
                    "pointId": self.prm,
                    "dateDebut": start_date.isoformat(),
                    "dateFin": end_date.isoformat(),
                },
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                timeout=_TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                await _check_status(response)
                try:
                    payload = await response.json()
                except ValueError, ContentTypeError:
                    raise APIError("Conso API returned invalid JSON", 200) from None
        except TimeoutError:
            raise APIError("Conso API request timed out") from None
        except ClientError, OSError:
            raise APIError("Could not connect to Conso API") from None

        return _parse_data(payload, self.prm, business, physical, unit)
