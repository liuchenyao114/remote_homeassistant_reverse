"""remote_push: reverse sync between two Home Assistant instances.

On the public instance (A) set role: hub.
On the instance behind NAT (B) set role: agent; B connects out to A.

Both roles can be configured from the UI (Settings -> Devices & services ->
Add integration -> Remote Push) or, for backward compatibility, from YAML.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

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

AGENT_REQUIRED = (CONF_AGENT_ID, CONF_SECRET, CONF_HOST, CONF_TOKEN)


def get_hub(hass: HomeAssistant) -> PushHub | None:
    """Return the shared hub instance, if this instance runs as a hub."""
    return hass.data.get(DOMAIN, {}).get("hub")


def _ensure_hub(hass: HomeAssistant) -> PushHub:
    data = hass.data.setdefault(DOMAIN, {})
    hub: PushHub | None = data.get("hub")
    if hub is None:
        hub = PushHub(hass)
        data["hub"] = hub
    return hub


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Handle YAML configuration (kept for backward compatibility)."""
    hass.data.setdefault(DOMAIN, {})
    conf = config.get(DOMAIN)
    if not conf:
        return True

    role = conf.get(CONF_ROLE, ROLE_AGENT)

    if role == ROLE_HUB:
        agents_conf = conf.get(CONF_AGENTS) or []
        if not agents_conf:
            _LOGGER.error("remote_push hub requires at least one agent entry")
            return False
        hub = _ensure_hub(hass)
        for agent_conf in agents_conf:
            hub.add_agent(agent_conf)
        await hub.async_start()
        _LOGGER.info(
            "remote_push hub configured from YAML, agents: %s", list(hub.agents)
        )
        return True

    missing = [key for key in AGENT_REQUIRED if not conf.get(key)]
    if missing:
        _LOGGER.error("remote_push agent missing required option(s): %s", missing)
        return False

    agent = PushAgent(hass, dict(conf))
    await agent.async_start()
    hass.data[DOMAIN]["yaml_agent"] = agent
    _LOGGER.info("remote_push agent configured from YAML")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one config entry: either a hub agent registration or an agent."""
    data = {**entry.data, **entry.options}
    role = data.get(CONF_ROLE, ROLE_AGENT)
    hass.data.setdefault(DOMAIN, {})

    if role == ROLE_HUB:
        hub = _ensure_hub(hass)
        hub.add_agent(data, config_entry_id=entry.entry_id)
        await hub.async_start()
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        _LOGGER.info(
            "remote_push hub agent %s added from config entry", data[CONF_AGENT_ID]
        )
        return True

    missing = [key for key in AGENT_REQUIRED if not data.get(key)]
    if missing:
        _LOGGER.error("remote_push agent entry missing option(s): %s", missing)
        return False

    agent = PushAgent(hass, dict(data))
    await agent.async_start()
    hass.data[DOMAIN][entry.entry_id] = agent
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the agent registration or stop the outbound agent."""
    data = {**entry.data, **entry.options}
    if data.get(CONF_ROLE, ROLE_AGENT) == ROLE_HUB:
        hub = get_hub(hass)
        agent_id = data.get(CONF_AGENT_ID)
        if hub is not None and agent_id:
            await hub.async_remove_agent(agent_id)
            if not hub.agents:
                await hub.async_stop()
                hass.data.get(DOMAIN, {}).pop("hub", None)
        return True

    agent = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if agent is not None:
        await agent.async_stop()
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload an entry after its options were changed."""
    await hass.config_entries.async_reload(entry.entry_id)
