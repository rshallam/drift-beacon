from typing import Final

DOMAIN: Final = "drift_beacon"

# Configuration keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_PROTOCOL: Final = "protocol"
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_USER_ID: Final = "user_id"
CONF_SESSION_TOKEN: Final = "session_token"
CONF_SESSION_EXPIRES: Final = "session_expires"
CONF_HUB_ID: Final = "hub_id"
CONF_HUB_NAME: Final = "hub_name"

# Default values
DEFAULT_HOST: Final = "local-drift-beacon"
DEFAULT_PORT: Final = 9000
DEFAULT_SCAN_INTERVAL: Final = 3  # seconds

# API endpoints
API_SYSTEM_STATUS: Final = "/api/device/status"
API_AUTH_SIGN_IN: Final = "/api/auth/sign-in/email"
API_AUTH_CREATE_SERVER_SESSION: Final = "/api/auth/create-server-session"
API_ACTIVITIES: Final = "/api/activities"
API_LIVE_SESSION: Final = "/api/live-session"
API_START_SESSION: Final = "/api/start-session"
API_STOP_SESSION: Final = "/api/stop-session"

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
PLATFORMS: Final = ["switch", "sensor"]

# Events
EVENT_SESSION_STARTED: Final = "drift_beacon_session_started"
EVENT_SESSION_STOPPED: Final = "drift_beacon_session_stopped"
EVENT_SESSION_CHANGED: Final = "drift_beacon_session_changed"

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
ATTR_WORKSPACE_ID: Final = "workspace_id"
ATTR_WORKSPACE_NAME: Final = "workspace_name"
ATTR_SESSION_START_TIME: Final = "session_start_time"
ATTR_SESSION_DURATION: Final = "session_duration"
ATTR_SESSION_DURATION_FORMATTED: Final = "session_duration_formatted"
