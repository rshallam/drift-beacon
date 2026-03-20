from typing import Final

DOMAIN: Final = "drift_beacon"

# Configuration keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_PROTOCOL: Final = "protocol"
CONF_API_TOKEN: Final = "api_token"
CONF_HUB_ID: Final = "hub_id"
CONF_HUB_NAME: Final = "hub_name"

# Default values
DEFAULT_HOST: Final = "local-drift-beacon"
DEFAULT_PORT: Final = 9000

# API endpoints (HTTP — used by config_flow only)
API_SYSTEM_STATUS: Final = "/api/device/status"

# WebSocket
WS_PATH: Final = "/api/ws"
WS_RECONNECT_MIN_DELAY: Final = 1  # seconds
WS_RECONNECT_MAX_DELAY: Final = 4  # seconds

# Timeouts
API_TIMEOUT: Final = 5  # seconds

# Hub detection
DETECTION_CANDIDATES: Final = [
    ("local-drift-beacon", 9000),
    ("homeassistant.local", 9000),
    ("localhost", 9000),
]
DETECTION_TIMEOUT: Final = 2  # seconds
PROTOCOL_DETECTION_TIMEOUT: Final = 1.5  # seconds

# Platforms
PLATFORMS: Final = ["switch", "sensor", "button"]

# Events
EVENT_SESSION_STARTED: Final = "drift_beacon_session_started"
EVENT_SESSION_STOPPED: Final = "drift_beacon_session_stopped"
EVENT_SESSION_CHANGED: Final = "drift_beacon_session_changed"
EVENT_ACTIVITY_ARMED: Final = "drift_beacon_activity_armed"
EVENT_ACTIVITY_DISARMED: Final = "drift_beacon_activity_disarmed"

# Attributes
ATTR_ACTIVITY_ID: Final = "activity_id"
ATTR_ACTIVITY_NAME: Final = "activity_name"
ATTR_DESCRIPTION: Final = "description"
ATTR_CATEGORY_ID: Final = "category_id"
ATTR_CATEGORY_NAME: Final = "category_name"
ATTR_CATEGORY_ICON: Final = "category_icon"
ATTR_CATEGORY_COLOR: Final = "category_color"
ATTR_COLOR: Final = "color"
ATTR_ICON: Final = "icon"
ATTR_SORT_ORDER: Final = "sort_order"
ATTR_UNIT: Final = "unit"
ATTR_PROGRESS: Final = "progress"
ATTR_TARGET: Final = "target"
ATTR_WORKSPACE_ID: Final = "workspace_id"
ATTR_WORKSPACE_NAME: Final = "workspace_name"
ATTR_SESSION_START_TIME: Final = "session_start_time"
ATTR_SESSION_DURATION: Final = "session_duration"
ATTR_SESSION_DURATION_FORMATTED: Final = "session_duration_formatted"
ATTR_ARMED_AT: Final = "armed_at"
