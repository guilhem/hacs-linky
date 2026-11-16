# Linky for Home Assistant

See your Linky meter's electricity use in Home Assistant's Energy dashboard.
Linky collects consumption through [Conso API](https://conso.boris.sh/) and can
also collect electricity you send back to the grid. To fill older gaps, you
can import a Conso Downloader CSV without contacting Conso.

## Install Linky and connect a meter

You need Home Assistant **2026.11.1 or later** with Recorder enabled, a
[Conso API token](https://conso.boris.sh/) for your meter, and Enedis data
collection enabled. Hourly charts require Enedis load curve access. If Enedis
only publishes daily totals for your meter, Linky can use those instead.

1. [![Open Linky in your Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=guilhem&repository=hacs-linky&category=integration)

   Download **Linky** and restart Home Assistant. If the link is unavailable,
   add `https://github.com/guilhem/hacs-linky` as an **Integration** in the
   Marketplace or HACS custom repositories menu. To install manually, copy
   `custom_components/linky` into Home Assistant's `config/custom_components`
   directory and restart.
2. [Add Linky to Home Assistant](https://my.home-assistant.io/redirect/config_flow_start/?domain=linky).
   Enter the Conso token and select the meter if prompted. Repeat for each
   additional meter. If Home Assistant reports an initial 24-hour pause, leave
   it running and retry the form when the pause ends.
3. After the first eligible refresh, open the meter from the
   [Linky integration page](https://my.home-assistant.io/redirect/integration/?domain=linky).
   **Daily consumption** shows the latest published day. Its `last_date`
   attribute is the day the reading was measured. Enedis usually publishes
   readings the following morning; the average power sensor is a published
   interval, not a live reading.

Find downloads and changes under [GitHub Releases](https://github.com/guilhem/hacs-linky/releases).

## Show the meter in Energy

1. [Open Energy configuration](https://my.home-assistant.io/redirect/config_energy/).
   Under **Grid consumption**, select `Linky {PRM} hourly consumption (kWh)`.
   If you enabled export collection, use `Linky {PRM} hourly production (kWh)`
   under **Return to grid**.
2. If your meter only provides daily totals, select its **daily** statistic
   instead. Use only one resolution per meter and direction to avoid double
   counting. Set the Home Assistant time zone to `Europe/Paris` to align days
   with Linky.
3. [Open the Energy dashboard](https://my.home-assistant.io/redirect/energy/)
   and select a day with published readings.

The device page shows the latest reading. The named statistics hold the
history used by Energy. Linky supplies energy quantities; any cost for an
external statistic needs a separate cost statistic.

## Fill gaps or check a refresh

Open [Tools → Actions](https://my.home-assistant.io/redirect/tools_actions/)
as a Home Assistant administrator, or use one of the links below to select
the action. Then choose the meter.

| Action | What it does |
| --- | --- |
| [Refresh now](https://my.home-assistant.io/redirect/tools_perform_action/?service=linky.refresh) | Uses the same daily attempt and request limit as the automatic refresh. The response says whether it ran or why it was skipped. |
| [Get collection status](https://my.home-assistant.io/redirect/tools_perform_action/?service=linky.get_status) | Shows remaining requests, the last daily attempt, and why collection may have to wait. It does not contact Conso. |
| [Import historical data](https://my.home-assistant.io/redirect/tools_perform_action/?service=linky.import_historical_data) | Fetches up to seven days from Conso. The start date is included and the end date is excluded. The end defaults to today in Paris. |
| [Import a Conso Downloader CSV](https://my.home-assistant.io/redirect/tools_perform_action/?service=linky.import_conso_csv) | Adds missing **hourly consumption** points from a file. It preserves existing measurements and does not contact Conso. |

To import a CSV, upload the `historique_conso_<PRM>_<date>.csv` file from
[Conso Downloader](https://github.com/bokub/conso-downloader). Linky checks that
the meter number in the filename matches the selected meter. The response
lists added, identical, and conflicting hours, plus timestamps the CSV
could not reconstruct, such as ambiguous hours around clock changes.
A conflict keeps the value already in Energy. Uploading and importing the
same file again adds zero points. Check the result using the hourly
consumption series in Energy.

## Manage the meter and its request limit

On the [Linky integration page](https://my.home-assistant.io/redirect/integration/?domain=linky),
use **Reconfigure** to replace the token without losing the meter's history.
Use **Configure** to turn on grid export if your meter sends electricity
back, or select **Daily totals only** if the load curve is unavailable.
Daily-only collection does not update the average power sensor. To monitor
export on the device page, enable the export sensors in the entity list.

Linky attempts one automatic refresh per day. The refresh starts in the
morning or later if it must wait for the request budget, and collects
available readings for the previous seven days. Export uses a second
request. Startup does not trigger a refresh. The limit is **three requests
per meter in any rolling 24 hours**, including failures, token checks, and
Conso imports. The budget survives a Home Assistant restart. Initialization
and a full host reboot start a 24-hour pause to account for earlier
requests. See [Conso's usage rules](https://conso.boris.sh/blocage-ip).

If values are missing, open **Get collection status** first. If Conso rejects
your token, use **Reconfigure** or follow the repair in Home Assistant. If
daily totals are visible but hourly history is missing, check Enedis load
curve consent or import a CSV. The maximum apparent power sensor requires
a separate request and is no longer collected.

## Develop

Use [uv](https://docs.astral.sh/uv/) with Python 3.14:

```sh
uv run --python 3.14 --extra dev ruff check
uv run --python 3.14 --extra dev ruff format --check
uv run --python 3.14 --extra dev pytest
```

The tests use Home Assistant, a local Recorder, and simulated Conso responses.
No meter or token is required. When [reporting an issue](https://github.com/guilhem/hacs-linky/issues),
include the Home Assistant version and remove tokens and meter identifiers.

[MIT license](LICENSE)
