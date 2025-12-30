"""Constants for the Linky integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "linky"

# Configuration keys
CONF_TOKEN: Final = "token"
CONF_PRM: Final = "prm"

# Default values
DEFAULT_SCAN_INTERVAL: Final = timedelta(hours=6)

# Attributes
ATTR_USAGE_POINT_ID: Final = "usage_point_id"
ATTR_QUALITY: Final = "quality"
ATTR_LAST_VALUE: Final = "last_value"
ATTR_LAST_DATE: Final = "last_date"
