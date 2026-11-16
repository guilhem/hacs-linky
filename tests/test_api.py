"""Exercise the Conso V2 adapter without contacting the service."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import jwt
import pytest
from aiohttp import ClientConnectionError, ClientSession

from custom_components.linky.api import (
    APIError,
    AuthenticationError,
    HistoryLimitError,
    IntervalReading,
    InvalidTokenError,
    LinkyClient,
    MeteringData,
    NoDataError,
    PRMAccessError,
    fetch_recent,
)
from custom_components.linky.const import USER_AGENT

_PRM = "99999999999999"
_OTHER_PRM = "00000000000001"
_START = date(2026, 9, 28)
_END = date(2026, 9, 29)
_SIGNING_KEY = "a synthetic key for local adapter tests only"


def _token(subject: object, **claims: object) -> str:
    return jwt.encode({"sub": subject, **claims}, _SIGNING_KEY, algorithm="HS256")


@pytest.fixture
def http():
    """Use a shared session double with an aiohttp-style request context."""
    session = MagicMock(spec=ClientSession)
    response = session.get.return_value.__aenter__.return_value
    response.status = 200
    response.json = AsyncMock()
    return session, response


def _payload(business, physical, unit, *points, **extras):
    # V2 schema: https://github.com/bokub/linky#utilisation-1
    return {
        "idPrm": _PRM,
        "etapeMetier": "BRUT",
        "periode": {"dateDebut": str(_START), "dateFin": str(_END)},
        "grandeur": [
            {
                "grandeurMetier": business,
                "grandeurPhysique": physical,
                "unite": unit,
                "points": list(points),
                "calendrier": [],
            }
        ],
        "contexte": [],
        **extras,
    }


@pytest.mark.parametrize(
    ("method", "endpoint", "business", "physical", "unit", "point", "reading"),
    [
        (
            "get_daily_consumption",
            "consommation_quotidienne",
            "CONS",
            "EA",
            "Wh",
            {"v": "9500", "d": "2026-09-28"},
            IntervalReading(_START, 9500),
        ),
        (
            "get_daily_production",
            "production_quotidienne",
            "PROD",
            "EA",
            "Wh",
            {"v": "3900", "d": "2026-09-28"},
            IntervalReading(_START, 3900),
        ),
        (
            "get_max_power",
            "puissance_conso_max_quotidienne",
            "CONS",
            "PMA",
            "VA",
            {"v": "2800", "d": "2026-09-28 07:30:41"},
            IntervalReading(datetime(2026, 9, 28, 7, 30, 41), 2800),
        ),
        (
            "get_consumption_load_curve",
            "courbe_de_charge_consommation",
            "CONS",
            "PA",
            "W",
            {"v": "170", "d": "2026-09-29 00:00:00", "p": "PT30M", "n": "B", "iv": "0", "ec": "0"},
            IntervalReading(datetime(2026, 9, 29), 170, timedelta(minutes=30)),
        ),
        (
            "get_production_load_curve",
            "courbe_de_charge_production",
            "PROD",
            "PA",
            "W",
            {"v": "60", "d": "2026-09-28 00:30:00", "p": "PT30M", "n": "B", "iv": "0", "ec": "0"},
            IntervalReading(datetime(2026, 9, 28, 0, 30), 60, timedelta(minutes=30)),
        ),
    ],
)
async def test_v2_requests_and_readings(
    http, method, endpoint, business, physical, unit, point, reading
):
    """Parse representative points from the public esthesis-edge V2 fixtures."""
    # esthesis-edge-backend/src/test/resources/wiremock/enedis/__files/mesure-v2-*.json
    session, response = http
    token = _token([_OTHER_PRM, _PRM])
    client = LinkyClient(session, token, prm=_PRM)
    extra = {} if unit == "W" else {"pas": "P1D"}
    response.json.return_value = _payload(business, physical, unit, point, **extra)

    start = datetime(2026, 9, 27, 22, 10, tzinfo=UTC)
    assert await getattr(client, method)(start, str(_END)) == MeteringData(
        _PRM, _START, _END, [reading], unit, "BRUT"
    )
    (url,), kwargs = session.get.call_args
    assert url == f"https://conso.boris.sh/api/{endpoint}"
    assert kwargs["params"] == {
        "pointId": _PRM,
        "dateDebut": "2026-09-28",
        "dateFin": "2026-09-29",
    }
    assert kwargs["headers"] == {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    assert 0 < kwargs["timeout"].total < 30
    assert not kwargs["allow_redirects"]
    session.close.assert_not_awaited()


async def test_curve_offsets_durations_and_missing_values(http):
    """Keep ambiguous wall time, distinguish explicit folds, and preserve real zero."""
    session, response = http
    client = LinkyClient(session, _token([_PRM]))
    response.json.return_value = _payload(
        "CONS",
        "PA",
        "W",
        {"v": None, "d": "2026-10-25 01:30:00"},
        {"v": "null", "d": "2026-10-25 02:00:00"},
        {"v": "25.5", "d": "2026-10-25 02:30:00+02:00", "p": "PT10M"},
        {"v": "0", "d": "2026-10-25T02:30:00+01:00", "p": "PT30M"},
        {"v": 17, "d": "2026-10-25 02:30:00"},
        periode={"dateDebut": "2026-10-25", "dateFin": "2026-10-26"},
    )
    data = await client.get_consumption_load_curve("2026-10-25", "2026-10-26")
    assert data.interval_reading == [
        IntervalReading(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), 25.5, timedelta(minutes=10)),
        IntervalReading(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), 0, timedelta(minutes=30)),
        IntervalReading(datetime(2026, 10, 25, 2, 30), 17),
    ]


def test_token_claim_only_selects_a_meter(http):
    """Leave expiration and signature checks to Conso."""
    session, _ = http
    client = LinkyClient(session, _token([_OTHER_PRM, _PRM, _PRM], exp=1))
    assert client.prms == [_OTHER_PRM, _PRM]
    assert client.prm == _OTHER_PRM
    with pytest.raises(PRMAccessError) as error:
        LinkyClient(session, _token([_OTHER_PRM]), prm=_PRM)
    assert _PRM not in str(error.value)
    session.get.assert_not_called()


@pytest.mark.parametrize(
    "token",
    [
        "garbled-token",
        _token(_PRM),
        _token([]),
        _token([_PRM, 7]),
        _token(["９" * 14]),
    ],
)
def test_rejects_malformed_token_or_subject(http, token):
    session, _ = http
    with pytest.raises(InvalidTokenError) as error:
        LinkyClient(session, token)
    assert token not in str(error.value)
    session.get.assert_not_called()


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, {"message": "Votre token est invalide"}, AuthenticationError),
        (401, {"message": "The Enedis API returned an error", "error": "upstream"}, APIError),
        (
            404,
            {
                "message": "The Enedis API returned an error",
                "error": {"error": "Not Found", "error_description": "La sous-ressource demandée"},
            },
            NoDataError,
        ),
        (
            404,
            {
                "message": "The Enedis API returned an error",
                "error": {"error": "no_data_found", "error_description": "no measure found"},
            },
            NoDataError,
        ),
        (404, {"message": "Unknown route"}, APIError),
        (403, {"message": "Cloudflare block"}, APIError),
        (429, {"message": "Rate limited"}, APIError),
        (500, {"message": "Service failure"}, APIError),
    ],
)
async def test_error_statuses_are_classified_without_exposing_response(
    http, status, body, error_type
):
    session, response = http
    token = _token([_PRM])
    response.status = status
    response.json.return_value = {**body, "private": f"{_PRM} {token} private data"}

    with pytest.raises(error_type) as error:
        await LinkyClient(session, token).get_daily_consumption(_START, _END)

    assert type(error.value) is error_type
    assert error.value.status_code == status
    assert not any(s in str(error.value) for s in (_PRM, token, "private data"))


@pytest.mark.parametrize("network_error", [ClientConnectionError, TimeoutError])
async def test_network_failure_is_sanitized(http, network_error):
    session, _ = http
    token = _token([_PRM])
    session.get.side_effect = network_error(f"{_PRM} {token} private error")

    with pytest.raises(APIError) as error:
        await LinkyClient(session, token).get_daily_consumption(_START, _END)

    assert error.value.status_code is None
    assert error.value.__suppress_context__
    assert not any(s in str(error.value) for s in (_PRM, token, "private error"))


@pytest.mark.parametrize(
    "bad_data",
    [
        {"idPrm": _OTHER_PRM},
        {"grandeur": [{"grandeurMetier": "CONS", "grandeurPhysique": "EA", "unite": "kWh"}]},
        _payload("CONS", "EA", "Wh", {"d": "2026-09-28", "v": "NaN"}),
        {"periode": None},
    ],
)
async def test_rejects_wrong_meter_or_invalid_schema(http, bad_data):
    session, response = http
    response.json.return_value = {
        **_payload("CONS", "EA", "Wh", {"d": "2026-09-28", "v": "100"}),
        **bad_data,
    }
    with pytest.raises(APIError, match="invalid measurement data") as error:
        await LinkyClient(session, _token([_PRM])).get_daily_consumption(_START, _END)
    assert error.value.status_code == 200
    assert error.value.__suppress_context__


async def test_invalid_json_is_sanitized(http):
    session, response = http
    response.json.side_effect = ValueError("private upstream body")
    with pytest.raises(APIError, match="invalid JSON") as error:
        await LinkyClient(session, _token([_PRM])).get_daily_consumption(_START, _END)
    assert error.value.status_code == 200
    assert error.value.__suppress_context__
    assert "private upstream body" not in str(error.value)


async def test_rejects_invalid_date_ranges_before_request(http):
    session, _ = http
    client = LinkyClient(session, _token([_PRM]))
    with pytest.raises(APIError, match="exclusive end date"):
        await client.get_daily_consumption(_END, _END)
    with pytest.raises(APIError, match="seven days"):
        await client.get_production_load_curve(_START, _START + timedelta(days=8))
    session.get.assert_not_called()


@pytest.mark.parametrize(
    ("status", "upstream", "code", "error_type"),
    [
        (400, True, "ADAM-ERR0123", HistoryLimitError),
        (400, True, "ADAM-ERR0125", HistoryLimitError),
        (400, True, "ADAM-ERR0002", APIError),
        (400, False, "ADAM-ERR0123", APIError),
        (404, True, "ADAM-ERR0123", APIError),
    ],
)
async def test_history_limit_requires_enedis_400_and_known_code(
    http, status, upstream, code, error_type
):
    session, response = http
    response.status = status
    response.json.return_value = {
        "status": status,
        "message": "The Enedis API returned an error" if upstream else "Invalid parameters",
        "error": {
            "error": code,
            "error_description": "Private provider detail",
        },
    }
    start = _END - timedelta(days=7)

    # A manual request must fail with the original bounds rather than truncate history.
    with pytest.raises(error_type) as raised:
        await LinkyClient(session, _token([_PRM])).get_daily_consumption(start, _END)

    assert type(raised.value) is error_type
    assert raised.value.status_code == status
    assert "Private provider detail" not in str(raised.value)
    session.get.assert_called_once()
    assert session.get.call_args.kwargs["params"]["dateDebut"] == start.isoformat()


async def test_recent_poll_recovers_the_longest_valid_suffix():
    activated = _END - timedelta(days=4)
    data = MeteringData(_PRM, activated, _END, [IntervalReading(activated, 1200)], "Wh")
    blocked = HistoryLimitError("Too early", 400)
    pending = NoDataError("Not yet published", 404)
    read = AsyncMock(side_effect=[blocked, pending, blocked, data])

    assert await fetch_recent(read, _END - timedelta(days=7), _END) is data

    assert read.await_args_list == [
        call(_END - timedelta(days=days), _END) for days in (7, 3, 5, 4)
    ]


@pytest.mark.parametrize(
    "last_error",
    [HistoryLimitError("Too early", 400), NoDataError("Not yet published", 404)],
)
async def test_recent_validation_preserves_provider_outcome(last_error):
    blocked = HistoryLimitError("Too early", 400)
    read = AsyncMock(side_effect=[blocked, last_error, blocked])

    with pytest.raises(type(last_error)) as raised:
        await fetch_recent(read, _END - timedelta(days=3), _END)

    assert raised.value.status_code == last_error.status_code
    requested_days = (3, 1, 2) if isinstance(last_error, NoDataError) else (3, 1)
    assert read.await_args_list == [
        call(_END - timedelta(days=days), _END) for days in requested_days
    ]


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("Denied", 401),
        NoDataError("Not yet published", 404),
        APIError("Rate limited", 429),
    ],
)
async def test_recent_helper_does_not_retry_other_errors(error):
    read = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as raised:
        await fetch_recent(read, _START, _END)

    assert raised.value is error
    read.assert_awaited_once_with(_START, _END)
