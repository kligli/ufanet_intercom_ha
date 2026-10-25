"""Constants for the Ufanet Intercom integration."""

DOMAIN = "ufanet"

CONF_CONTRACT = "contract"
CONF_PASSWORD = "password"
CONF_INTERCOM_ID = "intercom_id"

# Auth method keys for config flow.
CONF_AUTH_METHOD = "auth_method"
CONF_PHONE = "phone"
CONF_CONTRACT_ID = "contract_id"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DOM_URL = "dom_url"
CONF_CAMERA_MAP = "camera_map"
AUTH_PASSWORD = "password"
AUTH_PHONE = "phone"

# Options value meaning «do not attach a camera to this intercom».
NONE_CAMERA = "__none__"

PHONE_APPLICATION_ID = "ru.ufanet.smarthome"
PHONE_COUNTRY_ID = 1

DEFAULT_DOM_URL = "https://dom.ufanet.ru/"

CONF_SIP_USERNAME = "sip_username"
CONF_SIP_PASSWORD = "sip_password"
CONF_SIP_HOST = "sip_host"
CONF_SIP_PORT = "sip_port"
DEFAULT_SIP_HOST = "81.30.201.150"
DEFAULT_SIP_PORT = 5401

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1
