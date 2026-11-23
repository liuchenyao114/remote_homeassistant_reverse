"""remote_push: reverse sync between two Home Assistant instances.

On the public instance (A) set role: hub.
On the instance behind NAT (B) set role: agent; B connects out to A.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .agent import PushAgent
from .const import (
    CONF_AGENTS,
    CONF_AGENT_ID,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_HOST,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_MAX_MSG_SIZE,
    CONF_PORT,
    CONF_PREFIX,
    CONF_ROLE,
    CONF_SECRET,
    CONF_SECURE,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DEFAULT_MAX_MSG_SIZE,
    DEFAULT_PORT,
    DOMAIN,
    ROLE_AGENT,
    ROLE_HUB,
)
from .hub import PushHub

_LOGGER = logging.getLogger(__name__)

AGENT_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AGENT_ID): cv.string,
        vol.Required(CONF_SECRET): cv.string,
        vol.Optional(CONF_PREFIX, default=""): cv.string,
        vol.Optional(CONF_INCLUDE_DOMAINS, default=[]): cv.ensure_list,
        vol.Optional(CONF_EXCLUDE_DOMAINS, default=[]): cv.ensure_list,
        vol.Optional(CONF_INCLUDE_ENTITIES, default=[]): cv.entity_ids,
        vol.Optional(CONF_EXCLUDE_ENTITIES, default=[]): cv.entity_ids,
    },
    extra=vol.ALLOW_EXTRA,
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ROLE, default=ROLE_AGENT): vol.In(
                    [ROLE_HUB, ROLE_AGENT]
                ),
                vol.Optional(CONF_AGENTS, default=[]): vol.All(
                    cv.ensure_list, [AGENT_ENTRY_SCHEMA]
                ),
                vol.Optional(CONF_AGENT_ID): cv.string,
                vol.Optional(CONF_SECRET): cv.string,
                vol.Optional(CONF_PREFIX, default=""): cv.string,
                vol.Optional(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Optional(CONF_SECURE, default=False): cv.boolean,
                vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
                vol.Optional(CONF_TOKEN): cv.string,
                vol.Optional(CONF_MAX_MSG_SIZE, default=DEFAULT_MAX_MSG_SIZE): vol.Coerce(
                    int
                ),
                vol.Optional(CONF_INCLUDE_DOMAINS, default=[]): cv.ensure_list,
                vol.Optional(CONF_EXCLUDE_DOMAINS, default=[]): cv.ensure_list,
                vol.Optional(CONF_INCLUDE_ENTITIES, default=[]): cv.entity_ids,
                vol.Optional(CONF_EXCLUDE_ENTITIES, default=[]): cv.entity_ids,
            },
            extra=vol.ALLOW_EXTRA,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN)
    if not conf:
        return True

    hass.data.setdefault(DOMAIN, {})
    role = conf.get(CONF_ROLE, ROLE_AGENT)

    if role == ROLE_HUB:
        agents_conf = conf.get(CONF_AGENTS, [])
        if not agents_conf:
            _LOGGER.error("remote_push hub requires at least one agent entry")
            return False
        hub = PushHub(hass, agents_conf)
        hass.data[DOMAIN]["hub"] = hub
        await hub.async_start()
        return True

    missing = [
        key for key in (CONF_AGENT_ID, CONF_SECRET, CONF_HOST, CONF_TOKEN) if not conf.get(key)
    ]
    if missing:
        _LOGGER.error("remote_push agent missing required option(s): %s", missing)
        return False

    agent = PushAgent(hass, dict(conf))
    await agent.async_start()
    hass.data[DOMAIN]["agent"] = agent
    return True
