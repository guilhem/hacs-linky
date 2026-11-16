"""Constants for the Linky integration."""

from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "linky"
PARIS_TZ: Final = ZoneInfo("Europe/Paris")
USER_AGENT: Final = "github.com/guilhem/hacs-linky"

# Configuration keys
CONF_COLLECT_EXPORT: Final = "collect_export"
CONF_DAILY_ONLY: Final = "daily_only"
CONF_PRM: Final = "prm"

# Default values
DEFAULT_COLLECT_EXPORT: Final = True

# API rate limiting: 5 req/sec max, we use 200ms delay between requests to be safe

# Attributes
ATTR_USAGE_POINT_ID: Final = "usage_point_id"
ATTR_QUALITY: Final = "quality"
ATTR_LAST_VALUE: Final = "last_value"
ATTR_LAST_DATE: Final = "last_date"
