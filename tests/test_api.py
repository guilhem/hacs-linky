"""Exercise the Conso V2 adapter without contacting the service."""

from asyncio import Event, wait_for
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from json import dumps
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from aiohttp import (
    ClientConnectionError,
    ClientPayloadError,
    ClientSession,
    StreamReader,
    UnixConnector,
    web,
)
from homeassistant.core import HomeAssistant
from multidict import CIMultiDict

from custom_components.linky import api
from custom_components.linky import budget as budget_module
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
)
from custom_components.linky.budget import ConsoBudget, ConsoBudgetExceeded, async_get_conso_budget
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
    response.headers = CIMultiDict()
    response.content_type = "application/json"
    response.content.readexactly = AsyncMock()
    response.json = AsyncMock()
    budget = MagicMock(spec=ConsoBudget)
    budget.request.return_value.__aenter__.return_value = MagicMock()
    return session, response, budget


@pytest.fixture
async def ready_budget(tmp_path, monkeypatch):
    elapsed = 0.0
    monkeypatch.setattr(budget_module, "monotonic", lambda: elapsed)
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    await budget.async_initialize()
    elapsed += 24 * 60 * 60
    return budget


def _error_body(response, payload: bytes):
    response.content = StreamReader(MagicMock(), limit=65536)
    response.content.feed_data(payload)
    response.content.feed_eof()


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
    session, response, budget = http
    token = _token([_OTHER_PRM, _PRM])
    client = LinkyClient(session, token, budget=budget, prm=_PRM)
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
    budget.request.assert_called_once_with(_PRM)
    session.close.assert_not_awaited()


async def test_curve_offsets_durations_and_missing_values(http):
    """Keep ambiguous wall time, distinguish explicit folds, and preserve real zero."""
    session, response, budget = http
    client = LinkyClient(session, _token([_PRM]), budget=budget)
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
    session, _, budget = http
    client = LinkyClient(session, _token([_OTHER_PRM, _PRM, _PRM], exp=1), budget=budget)
    assert client.prms == [_OTHER_PRM, _PRM]
    assert client.prm == _OTHER_PRM
    with pytest.raises(PRMAccessError) as error:
        LinkyClient(session, _token([_OTHER_PRM]), budget=budget, prm=_PRM)
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
    session, _, budget = http
    with pytest.raises(InvalidTokenError) as error:
        LinkyClient(session, token, budget=budget)
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
        (
            404,
            {
                "status": 404,
                "message": "The Enedis API returned an error",
                "error": {"message": "Pas de mesure trouvée pour ce point"},
            },
            NoDataError,
        ),
        (403, {"message": "Cloudflare block"}, APIError),
        (429, {"message": "Rate limited"}, APIError),
        (500, {"message": "Service failure"}, APIError),
    ],
)
async def test_error_statuses_are_classified_without_exposing_response(
    http, status, body, error_type
):
    session, response, budget = http
    token = _token([_PRM])
    response.status = status
    _error_body(response, dumps({**body, "private": f"{_PRM} {token} private data"}).encode())

    with pytest.raises(error_type) as error:
        await LinkyClient(session, token, budget=budget).get_daily_consumption(_START, _END)

    assert type(error.value) is error_type
    assert error.value.status_code == status
    assert not any(s in str(error.value) for s in (_PRM, token, "private data"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ADAM-ERR0123", "; Enedis: ADAM-ERR0123"),
        ("ADAM-DC-0007", "; Enedis: ADAM-DC-0007"),
        ("no_data_found", "; Enedis: no_data_found"),
        ("ADAM-ERR0123\nprivate data", "; Enedis"),
        (_PRM, "; Enedis"),
        (["ADAM-ERR0123"], "; Enedis"),
    ],
)
async def test_wrapped_403_preserves_only_safe_enedis_diagnostics(http, code, expected):
    session, response, budget = http
    token = _token([_PRM])
    response.status = 403
    response.headers["X-Request-ID"] = token
    body = {
        "status": 403,
        "message": "The Enedis API returned an error",
        "error": {
            "error": code,
            "error_description": f"<p>private data https://example.test/{_PRM} {token}</p>",
            "correlationId": token,
        },
    }
    _error_body(response, dumps(body).encode())

    with pytest.raises(APIError) as error:
        await LinkyClient(session, token, budget=budget).get_daily_consumption(_START, _END)

    assert type(error.value) is APIError
    assert error.value.status_code == 403
    assert str(error.value) == f"Conso API request failed (HTTP 403{expected})"


async def test_enedis_wrapper_can_carry_raw_text_without_exposing_it(http):
    session, response, budget = http
    token = _token([_PRM])
    response.status = 502
    body = {
        "status": 502,
        "message": "The Enedis API returned an error",
        "error": f"<html>Cloudflare https://example.test/{_PRM} {token}</html>",
    }
    _error_body(response, dumps(body).encode())

    with pytest.raises(APIError) as error:
        await LinkyClient(session, token, budget=budget).get_daily_consumption(_START, _END)

    assert error.value.status_code == 502
    assert str(error.value) == "Conso API request failed (HTTP 502; Enedis)"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            {"CF-Mitigated": "challenge", "CF-Ray": "230b030023ae2822-SJC"},
            "; Cloudflare challenge; Ray ID: 230b030023ae2822-SJC",
        ),
        ({"CF-Mitigated": "challenge", "CF-Ray": f"{_PRM}ab-SJC"}, "; Cloudflare challenge"),
        (
            {"CF-Mitigated": "challenge", "CF-Ray": "https://example.test/"},
            "; Cloudflare challenge",
        ),
        ({"Server": "cloudflare", "CF-Ray": "230b030023ae2822-SJC"}, ""),
        ({"CF-Mitigated": "other"}, ""),
        ({}, ""),
    ],
)
async def test_non_json_403_requires_explicit_challenge_marker(http, headers, expected):
    session, response, budget = http
    response.status = 403
    response.headers.update(headers)
    response.content_type = "text/html"

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert type(error.value) is APIError
    assert error.value.status_code == 403
    assert str(error.value) == f"Conso API request failed (HTTP 403{expected})"
    response.json.assert_not_awaited()
    response.content.readexactly.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 401, 404])
async def test_explicit_challenge_takes_precedence_over_status_and_body(http, status):
    session, response, budget = http
    response.status = status
    response.headers["cf-mitigated"] = "challenge"

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert type(error.value) is APIError
    assert error.value.status_code == status
    assert str(error.value) == f"Conso API request failed (HTTP {status}; Cloudflare challenge)"
    response.json.assert_not_awaited()
    response.content.readexactly.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"message": "Cloudflare block"},
        {"message": "The Enedis API returned an error", "error": {"error": "ADAM-ERR0123"}},
        {"status": 401, "message": "The Enedis API returned an error", "error": {}},
        {"status": 403.0, "message": "The Enedis API returned an error", "error": {}},
        {"status": 403, "message": "The Enedis API returned an error"},
        ["The Enedis API returned an error", "ADAM-ERR0123"],
    ],
)
async def test_unrecognized_403_has_neutral_origin(http, body):
    session, response, budget = http
    response.status = 403
    _error_body(response, dumps(body).encode())

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert type(error.value) is APIError
    assert error.value.status_code == 403
    assert str(error.value) == "Conso API request failed (HTTP 403)"


async def test_html_429_does_not_read_the_body(http):
    session, response, budget = http
    response.status = 429
    response.content_type = "text/html"

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert error.value.status_code == 429
    assert str(error.value) == "Conso API request failed (HTTP 429)"
    response.json.assert_not_awaited()
    response.content.readexactly.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [(403, "172800", 0), (429, "172800", 172800), (429, "http date", 172800), (429, "invalid", 0)],
)
async def test_provider_block_marks_cooldown_without_hiding_the_error(
    http, monkeypatch, status, retry_after, expected
):
    session, response, budget = http
    response.status = status
    if retry_after == "http date":
        now = datetime(2026, 11, 16, 12, tzinfo=UTC)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(api, "datetime", FixedDatetime)
        retry_after = format_datetime(now + timedelta(days=2), usegmt=True)
    response.headers["Retry-After"] = retry_after
    response.content_type = "text/html"

    client = LinkyClient(session, _token([_PRM]), budget=budget)
    with pytest.raises(APIError, match=f"HTTP {status}") as error:
        await client.get_daily_consumption(_START, _END)

    assert error.value.status_code == status
    budget.request.return_value.__aenter__.return_value.assert_called_once_with(expected)
    session.get.assert_called_once()


async def test_403_blocks_other_clients_before_transport(http, tmp_path, ready_budget):
    session, response, _ = http
    response.status = 403
    response.content_type = "text/html"
    token = _token([_PRM])
    with pytest.raises(APIError, match="HTTP 403"):
        await LinkyClient(session, token, budget=ready_budget).get_daily_consumption(_START, _END)
    budget = async_get_conso_budget(HomeAssistant(str(tmp_path)))
    with pytest.raises(ConsoBudgetExceeded, match="paused"):
        await LinkyClient(session, token, budget=budget).get_daily_production(_START, _END)
    session.get.assert_called_once()


async def test_connection_replay_is_blocked_on_the_wire(tmp_path, monkeypatch, ready_budget):
    hits = 0

    async def drop_first(request):
        nonlocal hits
        hits += 1
        if hits == 1:
            request.transport.close()
        return web.Response(text="second attempt")

    server = web.Application()
    server.router.add_get("/api/consommation_quotidienne", drop_first)
    runner = web.AppRunner(server)
    await runner.setup()
    socket = tmp_path / "http.sock"
    try:
        await web.UnixSite(runner, str(socket)).start()
        base = "http://localhost/api"
        monkeypatch.setattr(api, "_BASE_URL", base)
        async with ClientSession(connector=UnixConnector(path=socket)) as session:
            async with session.get(f"{base}/consommation_quotidienne") as response:
                assert await response.text() == "second attempt"
            assert hits == 2

            hits = 0
            client = LinkyClient(session, _token([_PRM]), budget=ready_budget)
            with pytest.raises(APIError, match="Could not connect"):
                await client.get_daily_consumption(_START, _END)
            assert hits == 1
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("size", "diagnostic", "unread"),
    [(8192, "; Enedis: ADAM-DC-0007", b""), (8194, "", b" ")],
)
async def test_json_error_diagnostic_has_a_byte_limit(http, size, diagnostic, unread):
    session, response, budget = http
    response.status = 403
    response.content_type = "application/problem+json"
    body = dumps(
        {
            "status": 403,
            "message": "The Enedis API returned an error",
            "error": {"error": "ADAM-DC-0007"},
        }
    ).encode()
    _error_body(response, body.ljust(size))

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert error.value.status_code == 403
    assert str(error.value) == f"Conso API request failed (HTTP 403{diagnostic})"
    assert response.content.read_nowait() == unread
    response.json.assert_not_awaited()


@pytest.mark.parametrize("failure", [b"{private response detail", ClientPayloadError("private")])
async def test_error_diagnostic_failure_does_not_lose_http_status(http, failure):
    session, response, budget = http
    response.status = 403
    if isinstance(failure, bytes):
        _error_body(response, failure)
    else:
        response.content.readexactly.side_effect = failure

    with pytest.raises(APIError) as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )

    assert error.value.status_code == 403
    assert str(error.value) == "Conso API request failed (HTTP 403)"
    assert error.value.__context__ is None or error.value.__suppress_context__
    response.json.assert_not_awaited()


async def test_incomplete_json_is_abandoned_at_the_diagnostic_deadline(http):
    session, response, budget = http
    response.status = 500
    blocked = Event()

    async def slow_read(_: int) -> bytes:
        await blocked.wait()
        return b""

    response.content.readexactly.side_effect = slow_read
    client = LinkyClient(session, _token([_PRM]), budget=budget)
    with pytest.raises(APIError) as error:
        await wait_for(client.get_daily_consumption(_START, _END), 2)

    assert error.value.status_code == 500
    assert str(error.value) == "Conso API request failed (HTTP 500)"
    response.content.readexactly.assert_awaited_once_with(8193)
    response.json.assert_not_awaited()


@pytest.mark.parametrize("network_error", [ClientConnectionError, TimeoutError])
async def test_network_failure_is_sanitized(http, network_error):
    session, _, budget = http
    token = _token([_PRM])
    session.get.side_effect = network_error(f"{_PRM} {token} private error")

    with pytest.raises(APIError) as error:
        await LinkyClient(session, token, budget=budget).get_daily_consumption(_START, _END)

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
    session, response, budget = http
    response.json.return_value = {
        **_payload("CONS", "EA", "Wh", {"d": "2026-09-28", "v": "100"}),
        **bad_data,
    }
    with pytest.raises(APIError, match="invalid measurement data") as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )
    assert error.value.status_code == 200
    assert error.value.__suppress_context__


async def test_invalid_json_is_sanitized(http):
    session, response, budget = http
    response.json.side_effect = ValueError("private upstream body")
    with pytest.raises(APIError, match="invalid JSON") as error:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(
            _START, _END
        )
    assert error.value.status_code == 200
    assert error.value.__suppress_context__
    assert "private upstream body" not in str(error.value)


async def test_rejects_invalid_date_ranges_before_request(http):
    session, _, budget = http
    client = LinkyClient(session, _token([_PRM]), budget=budget)
    with pytest.raises(APIError, match="exclusive end date"):
        await client.get_daily_consumption(_END, _END)
    with pytest.raises(APIError, match="seven days"):
        await client.get_production_load_curve(_START, _START + timedelta(days=8))
    session.get.assert_not_called()
    budget.request.assert_not_called()


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
    session, response, budget = http
    response.status = status
    body = {
        "status": status,
        "message": "The Enedis API returned an error" if upstream else "Invalid parameters",
        "error": {
            "error": code,
            "error_description": "Private provider detail",
        },
    }
    _error_body(response, dumps(body).encode())
    start = _END - timedelta(days=7)

    # A manual request must fail with the original bounds rather than truncate history.
    with pytest.raises(error_type) as raised:
        await LinkyClient(session, _token([_PRM]), budget=budget).get_daily_consumption(start, _END)

    assert type(raised.value) is error_type
    assert raised.value.status_code == status
    assert "Private provider detail" not in str(raised.value)
    session.get.assert_called_once()
    assert session.get.call_args.kwargs["params"]["dateDebut"] == start.isoformat()
