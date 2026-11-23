"""Config flow for remote_push: pick a role and fill in the fields."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AGENT_ID,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_HOST,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_PORT,
    CONF_PREFIX,
    CONF_ROLE,
    CONF_SECRET,
    CONF_SECURE,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
    ROLE_AGENT,
    ROLE_HUB,
)

_LOGGER = logging.getLogger(__name__)

TEXT = selector.TextSelector()
PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
BOOLEAN = selector.BooleanSelector()
PORT = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1, max=65535, mode=selector.NumberSelectorMode.BOX
    )
)
ENTITIES = selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))


class CannotConnect(HomeAssistantError):
    """Error to indicate the hub cannot be reached."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate the access token was rejected."""


def _join(values: Any) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        return values
    return ", ".join(str(value) for value in values)


def _split(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        items = values.replace("\n", ",").split(",")
        return [item.strip() for item in items if item.strip()]
    return [str(value) for value in values]


def _filters_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Optional(
            CONF_INCLUDE_DOMAINS,
            default=_join(defaults.get(CONF_INCLUDE_DOMAINS)),
        ): TEXT,
        vol.Optional(
            CONF_EXCLUDE_DOMAINS,
            default=_join(defaults.get(CONF_EXCLUDE_DOMAINS)),
        ): TEXT,
        vol.Optional(
            CONF_INCLUDE_ENTITIES,
            default=[str(e) for e in defaults.get(CONF_INCLUDE_ENTITIES) or []],
        ): ENTITIES,
        vol.Optional(
            CONF_EXCLUDE_ENTITIES,
            default=[str(e) for e in defaults.get(CONF_EXCLUDE_ENTITIES) or []],
        ): ENTITIES,
    }


def hub_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for registering an agent on the public (hub) instance."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AGENT_ID, default=defaults.get(CONF_AGENT_ID, "")
            ): TEXT,
            vol.Required(CONF_SECRET, default=defaults.get(CONF_SECRET, "")): PASSWORD,
            vol.Optional(
                CONF_PREFIX,
                default=defaults.get(CONF_PREFIX)
                or f"{defaults.get(CONF_AGENT_ID, '')}_",
            ): TEXT,
            **_filters_schema(defaults),
        }
    )


def agent_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for the instance behind NAT that connects out to the hub."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_AGENT_ID, default=defaults.get(CONF_AGENT_ID, "")
            ): TEXT,
            vol.Required(CONF_SECRET, default=defaults.get(CONF_SECRET, "")): PASSWORD,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): TEXT,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): PORT,
            vol.Required(
                CONF_SECURE, default=bool(defaults.get(CONF_SECURE, False))
            ): BOOLEAN,
            vol.Required(
                CONF_VERIFY_SSL, default=bool(defaults.get(CONF_VERIFY_SSL, True))
            ): BOOLEAN,
            vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")): PASSWORD,
            **_filters_schema(defaults),
        }
    )


def normalize(user_input: dict[str, Any], role: str) -> dict[str, Any]:
    """Coerce form values into the types the runtime expects."""
    data: dict[str, Any] = {
        CONF_ROLE: role,
        CONF_AGENT_ID: str(user_input.get(CONF_AGENT_ID, "")).strip().lower(),
        CONF_SECRET: str(user_input.get(CONF_SECRET, "")).strip(),
        CONF_INCLUDE_DOMAINS: _split(user_input.get(CONF_INCLUDE_DOMAINS)),
        CONF_EXCLUDE_DOMAINS: _split(user_input.get(CONF_EXCLUDE_DOMAINS)),
        CONF_INCLUDE_ENTITIES: _split(user_input.get(CONF_INCLUDE_ENTITIES)),
        CONF_EXCLUDE_ENTITIES: _split(user_input.get(CONF_EXCLUDE_ENTITIES)),
    }
    if role == ROLE_HUB:
        prefix = str(user_input.get(CONF_PREFIX, "")).strip()
        data[CONF_PREFIX] = prefix or f"{data[CONF_AGENT_ID]}_"
        return data

    data[CONF_HOST] = str(user_input.get(CONF_HOST, "")).strip()
    try:
        data[CONF_PORT] = int(user_input.get(CONF_PORT, DEFAULT_PORT))
    except (TypeError, ValueError):
        data[CONF_PORT] = DEFAULT_PORT
    data[CONF_SECURE] = bool(user_input.get(CONF_SECURE, False))
    data[CONF_VERIFY_SSL] = bool(user_input.get(CONF_VERIFY_SSL, True))
    data[CONF_TOKEN] = str(user_input.get(CONF_TOKEN, "")).strip()
    return data


def _check_required(data: dict[str, Any], role: str) -> bool:
    if role == ROLE_HUB:
        return bool(data[CONF_AGENT_ID] and data[CONF_SECRET])
    return bool(
        data[CONF_AGENT_ID]
        and data[CONF_SECRET]
        and data[CONF_HOST]
        and data[CONF_TOKEN]
    )


async def validate_agent_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Check that the hub answers on the given URL with the given token."""
    scheme = "https" if data[CONF_SECURE] else "http"
    url = f"{scheme}://{data[CONF_HOST]}:{data[CONF_PORT]}/api/"
    session = async_get_clientsession(hass, data[CONF_VERIFY_SSL])
    headers = {"Authorization": f"Bearer {data[CONF_TOKEN]}"}
    try:
        async with asyncio.timeout(15):
            async with session.get(url, headers=headers) as resp:
                status = resp.status
    except asyncio.TimeoutError as err:
        raise CannotConnect(f"timeout talking to {url}") from err
    except (aiohttp.ClientError, ssl.SSLError) as err:
        raise CannotConnect(str(err)) from err

    if status in (401, 403):
        raise InvalidAuth("hub rejected the access token")
    if status >= 400:
        raise CannotConnect(f"hub answered HTTP {status}")


class RemotePushConfigFlow(ConfigFlow, domain=DOMAIN):
    """Choose hub or agent, then fill in the matching form."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.async_show_menu(step_id="user", menu_options=[ROLE_HUB, ROLE_AGENT])

    async def async_step_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Register an agent on this (public) instance."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = normalize(user_input, ROLE_HUB)
            if not _check_required(data, ROLE_HUB):
                errors["base"] = "invalid_input"
            else:
                for entry in self._async_current_entries():
                    if (
                        entry.data.get(CONF_ROLE) == ROLE_HUB
                        and entry.data.get(CONF_AGENT_ID) == data[CONF_AGENT_ID]
                    ):
                        return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=f"Hub · agent {data[CONF_AGENT_ID]}", data=data
                )
        return self.async_show_form(
            step_id=ROLE_HUB, data_schema=hub_schema(user_input), errors=errors
        )

    async def async_step_agent(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Configure this instance to connect out to the hub."""
        errors: dict[str, str] = {}

        for entry in self._async_current_entries():
            if entry.data.get(CONF_ROLE, ROLE_AGENT) == ROLE_AGENT:
                return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            data = normalize(user_input, ROLE_AGENT)
            if not _check_required(data, ROLE_AGENT):
                errors["base"] = "invalid_input"
            else:
                try:
                    await validate_agent_input(self.hass, data)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except Exception:  # noqa: BLE001 - surface anything unexpected
                    _LOGGER.exception("unexpected error validating agent")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=f"Agent {data[CONF_AGENT_ID]} → {data[CONF_HOST]}",
                        data=data,
                    )

        return self.async_show_form(
            step_id=ROLE_AGENT, data_schema=agent_schema(user_input), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RemotePushOptionsFlow()


class RemotePushOptionsFlow(OptionsFlow):
    """Edit the fields of an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        entry = self.config_entry
        role = entry.data.get(CONF_ROLE, ROLE_AGENT)
        defaults = {**entry.data, **entry.options}

        if user_input is not None:
            return self.async_create_entry(
                title="", data=normalize({**defaults, **user_input}, role)
            )

        if role == ROLE_HUB:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_SECRET, default=defaults.get(CONF_SECRET, "")
                    ): PASSWORD,
                    vol.Optional(
                        CONF_PREFIX, default=defaults.get(CONF_PREFIX, "")
                    ): TEXT,
                    **_filters_schema(defaults),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_SECRET, default=defaults.get(CONF_SECRET, "")
                    ): PASSWORD,
                    vol.Required(
                        CONF_HOST, default=defaults.get(CONF_HOST, "")
                    ): TEXT,
                    vol.Required(
                        CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
                    ): PORT,
                    vol.Required(
                        CONF_SECURE, default=bool(defaults.get(CONF_SECURE, False))
                    ): BOOLEAN,
                    vol.Required(
                        CONF_VERIFY_SSL,
                        default=bool(defaults.get(CONF_VERIFY_SSL, True)),
                    ): BOOLEAN,
                    vol.Required(
                        CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")
                    ): PASSWORD,
                    **_filters_schema(defaults),
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                CONF_AGENT_ID: str(defaults.get(CONF_AGENT_ID, "")),
                CONF_ROLE: role,
            },
        )
