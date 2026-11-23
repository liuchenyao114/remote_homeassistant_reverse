"""Constants for the remote_push integration."""

DOMAIN = "remote_push"

CONF_ROLE = "role"
ROLE_HUB = "hub"
ROLE_AGENT = "agent"

CONF_AGENTS = "agents"
CONF_AGENT_ID = "agent_id"
CONF_SECRET = "secret"
CONF_PREFIX = "prefix"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_SECURE = "secure"
CONF_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_MAX_MSG_SIZE = "max_message_size"

CONF_INCLUDE_DOMAINS = "include_domains"
CONF_EXCLUDE_DOMAINS = "exclude_domains"
CONF_INCLUDE_ENTITIES = "include_entities"
CONF_EXCLUDE_ENTITIES = "exclude_entities"

WS_REGISTER = "remote_push/register"
WS_STATES = "remote_push/states"
WS_STATE_CHANGED = "remote_push/state_changed"
WS_REMOVE = "remote_push/remove"
WS_SERVICE_CALL = "remote_push/service_call"
WS_SERVICE_RESULT = "remote_push/service_result"

SERVICE_CALL_SERVICE = "call_service"
EVENT_SERVICE_RESULT = "remote_push_service_result"

DEFAULT_PORT = 8123
DEFAULT_PREFIX = ""
DEFAULT_MAX_MSG_SIZE = 16 * 1024 * 1024
RECONNECT_DELAY = 10
