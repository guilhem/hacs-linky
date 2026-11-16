"""Constants for the Linky integration."""

from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "linky"
PARIS_TZ: Final = ZoneInfo("Europe/Paris")
USER_AGENT: Final = "github.com/guilhem/hacs-linky"

# Configuration keys
CONF_PRM: Final = "prm"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Default values
DEFAULT_SCAN_INTERVAL_HOURS: Final = 12
MIN_SCAN_INTERVAL_HOURS: Final = 1
MAX_SCAN_INTERVAL_HOURS: Final = 24

# API rate limiting: 5 req/sec max, we use 200ms delay between requests to be safe
API_REQUEST_DELAY: Final = 0.2

# Attributes
ATTR_USAGE_POINT_ID: Final = "usage_point_id"
ATTR_QUALITY: Final = "quality"
ATTR_LAST_VALUE: Final = "last_value"
ATTR_LAST_DATE: Final = "last_date"
