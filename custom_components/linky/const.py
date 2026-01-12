"""Constants for the Linky integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "linky"

# Configuration keys
CONF_TOKEN: Final = "token"
CONF_PRM: Final = "prm"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_PRICE_ENTITY: Final = "price_entity"

# Default values
DEFAULT_SCAN_INTERVAL_HOURS: Final = 6
DEFAULT_SCAN_INTERVAL: Final = timedelta(hours=DEFAULT_SCAN_INTERVAL_HOURS)
MIN_SCAN_INTERVAL_HOURS: Final = 1
MAX_SCAN_INTERVAL_HOURS: Final = 24

# API rate limiting: 5 req/sec max, we use 200ms delay between requests to be safe
API_REQUEST_DELAY: Final = 0.2

# Attributes
ATTR_USAGE_POINT_ID: Final = "usage_point_id"
ATTR_QUALITY: Final = "quality"
ATTR_LAST_VALUE: Final = "last_value"
ATTR_LAST_DATE: Final = "last_date"
ATTR_DATA_AGE_HOURS: Final = "data_age_hours"
