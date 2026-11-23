"""Hub side of remote_push: receives states pushed by agents on public A."""

from __future__ import annotations

import itertools
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, Event, callback, split_entity_id
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AGENT_ID,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_PREFIX,
    CONF_SECRET,
    DOMAIN,
    EVENT_SERVICE_RESULT,
    SERVICE_CALL_SERVICE,
    WS_REGISTER,
    WS_REMOVE,
    WS_SERVICE_CALL,
    WS_SERVICE_RESULT,
    WS_STATE_CHANGED,
    WS_STATES,
)

_LOGGER = logging.getLogger(__name__)

CALL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AGENT_ID): cv.string,
        vol.Required("domain"): cv.string,
        vol.Required("service"): cv.string,
        vol.Optional("entity_id"): vol.Any(cv.string, vol.All(list, [cv.string])),
        vol.Optional("service_data", default={}): dict,
    }
)


def _hub(hass: HomeAssistant) -> "PushHub | None":
    return hass.data.get(DOMAIN, {}).get("hub")


class PushHub:
    """Keep mirrored entities for one or more remote agents."""

    def __init__(
        self, hass: HomeAssistant, agents_conf: list[dict[str, Any]] | None = None
    ) -> None:
        self.hass = hass
        self.agents: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, Any] = {}
        self.entity_map: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._unsub_bus: Any = None
        self._started = False

        for conf in agents_conf or []:
            self.add_agent(conf)

    def add_agent(self, conf: dict[str, Any]) -> dict[str, Any]:
        """Register or update one agent, both from YAML and config entries."""
        agent_id = str(conf[CONF_AGENT_ID]).lower()
        entry = {
            "secret": conf[CONF_SECRET],
            "prefix": conf.get(CONF_PREFIX) or f"{agent_id}_",
            "include_domains": set(conf.get(CONF_INCLUDE_DOMAINS) or []),
            "exclude_domains": set(conf.get(CONF_EXCLUDE_DOMAINS) or []),
            "include_entities": {
                str(e).lower() for e in conf.get(CONF_INCLUDE_ENTITIES) or []
            },
            "exclude_entities": {
                str(e).lower() for e in conf.get(CONF_EXCLUDE_ENTITIES) or []
            },
        }
        self.agents[agent_id] = entry
        return entry

    async def async_remove_agent(self, agent_id: str) -> None:
        """Forget an agent and drop its mirrored states."""
        agent_id = str(agent_id).lower()
        self.agents.pop(agent_id, None)
        connection = self.connections.pop(agent_id, None)
        for entity_id, owner in list(self.entity_map.items()):
            if owner == agent_id:
                self.hass.states.async_remove(entity_id)
                self.entity_map.pop(entity_id, None)
        if connection is not None:
            _LOGGER.info("agent %s removed, connection kept until it drops", agent_id)
        _LOGGER.info("agent %s removed, mirrored states cleared", agent_id)

    @property
    def started(self) -> bool:
        return self._started

    async def async_start(self) -> None:
        """Register websocket commands, services and event listener."""
        if self._started:
            return
        for command in (
            ws_register,
            ws_states,
            ws_state_changed,
            ws_remove,
            ws_service_result,
        ):
            websocket_api.async_register_command(self.hass, command)

        self._unsub_bus = self.hass.bus.async_listen(
            EVENT_CALL_SERVICE, self._on_service_call
        )
        self.hass.services.async_register(
            DOMAIN,
            SERVICE_CALL_SERVICE,
            self.async_call_service,
            schema=CALL_SERVICE_SCHEMA,
        )
        self._started = True
        _LOGGER.info("remote_push hub started, agents: %s", list(self.agents))

    async def async_stop(self) -> None:
        if not self._started:
            return
        if self._unsub_bus is not None:
            self._unsub_bus()
            self._unsub_bus = None
        self.hass.services.async_remove(DOMAIN, SERVICE_CALL_SERVICE)
        for agent_id in list(self.connections):
            self.detach_connection(self.connections[agent_id])
        self._started = False
        _LOGGER.info("remote_push hub stopped")

    def attach(self, agent_id: str, connection: Any) -> None:
        """Attach an agent connection, dropping the previous one if any."""
        old = self.connections.get(agent_id)
        if old is not None and old is not connection:
            self.detach_connection(old)
        self.connections[agent_id] = connection

    def agent_for_connection(self, connection: Any) -> str | None:
        for agent_id, conn in self.connections.items():
            if conn is connection:
                return agent_id
        return None

    def detach_connection(self, connection: Any) -> None:
        agent_id = self.agent_for_connection(connection)
        if agent_id is None:
            return
        self.connections.pop(agent_id, None)
        for entity_id, owner in list(self.entity_map.items()):
            if owner == agent_id:
                self.hass.states.async_remove(entity_id)
                self.entity_map.pop(entity_id, None)
        _LOGGER.warning("agent %s disconnected, mirrored states removed", agent_id)

    @staticmethod
    def _wanted(conf: dict[str, Any], entity_id: str) -> bool:
        entity_id = entity_id.lower()
        domain, _ = split_entity_id(entity_id)
        if entity_id in conf["exclude_entities"] or domain in conf["exclude_domains"]:
            return False
        if conf["include_entities"] or conf["include_domains"]:
            return entity_id in conf["include_entities"] or domain in conf["include_domains"]
        return True

    def _local_entity_id(self, agent_id: str, entity_id: str) -> str:
        domain, object_id = split_entity_id(entity_id)
        prefix = self.agents[agent_id]["prefix"]
        if prefix and not object_id.startswith(prefix):
            object_id = prefix + object_id
        return f"{domain}.{object_id}"

    def _original_entity_id(self, agent_id: str, local_entity_id: str) -> str:
        domain, object_id = split_entity_id(local_entity_id)
        prefix = self.agents[agent_id]["prefix"]
        if prefix and object_id.startswith(prefix):
            object_id = object_id[len(prefix) :]
        return f"{domain}.{object_id}"

    def apply_state(self, agent_id: str, state_data: dict[str, Any]) -> None:
        """Write one pushed state into the local state machine."""
        conf = self.agents.get(agent_id)
        entity_id = state_data.get("entity_id")
        if conf is None or not entity_id or not self._wanted(conf, entity_id):
            return

        local_id = self._local_entity_id(agent_id, entity_id)
        attributes = dict(state_data.get("attributes") or {})
        attributes["remote_push_agent"] = agent_id
        friendly_name = attributes.get("friendly_name")
        marker = f"[{agent_id}] "
        if friendly_name and not str(friendly_name).startswith(marker):
            attributes["friendly_name"] = marker + str(friendly_name)

        self.entity_map[local_id.lower()] = agent_id
        self.hass.states.async_set(
            local_id, state_data.get("state", "unknown"), attributes
        )

    def remove_state(self, agent_id: str, entity_id: str) -> None:
        local_id = self._local_entity_id(agent_id, entity_id)
        self.hass.states.async_remove(local_id)
        self.entity_map.pop(local_id.lower(), None)

    async def send_to_agent(self, agent_id: str, payload: dict[str, Any]) -> bool:
        connection = self.connections.get(agent_id)
        if connection is None:
            _LOGGER.warning("no live connection for agent %s", agent_id)
            return False
        try:
            send_message = getattr(connection, "send_message", None)
            if send_message is not None:
                send_message(payload)
            else:  # older HA / test doubles expose async send_json
                await connection.send_json(payload)
        except Exception as err:  # noqa: BLE001 - websocket may close at any time
            _LOGGER.error("failed to send to agent %s: %s", agent_id, err)
            return False
        return True

    @callback
    def _on_service_call(self, event: Event) -> None:
        """Forward calls targeting mirrored entities back to their agent."""
        data = event.data
        if data.get("domain") == DOMAIN:
            return
        service_data = dict(data.get("service_data") or {})
        entity_ids = service_data.get("entity_id") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        targets: dict[str, list[str]] = {}
        for entity_id in entity_ids:
            agent_id = self.entity_map.get(str(entity_id).lower())
            if agent_id is None:
                continue
            targets.setdefault(agent_id, []).append(
                self._original_entity_id(agent_id, str(entity_id))
            )

        for agent_id, original_ids in targets.items():
            payload = {
                "id": next(self._ids),
                "type": WS_SERVICE_CALL,
                "domain": data.get("domain"),
                "service": data.get("service"),
                "entity_id": original_ids,
                "service_data": {**service_data, "entity_id": original_ids},
            }
            self.hass.loop.create_task(self.send_to_agent(agent_id, payload))

    async def async_call_service(self, call: Any) -> None:
        """Explicit proxy service, cleaner than entity-level interception."""
        agent_id = str(call.data[CONF_AGENT_ID]).lower()
        if agent_id not in self.agents:
            _LOGGER.error("unknown agent %s", agent_id)
            return
        entity_ids = call.data.get("entity_id") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        entity_ids = [
            self._original_entity_id(agent_id, str(entity_id))
            for entity_id in entity_ids
        ]
        payload = {
            "id": next(self._ids),
            "type": WS_SERVICE_CALL,
            "domain": call.data["domain"],
            "service": call.data["service"],
            "entity_id": entity_ids,
            "service_data": {**call.data.get("service_data", {}), "entity_id": entity_ids},
        }
        await self.send_to_agent(agent_id, payload)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_REGISTER,
        vol.Required(CONF_AGENT_ID): cv.string,
        vol.Required(CONF_SECRET): cv.string,
    }
)
@websocket_api.async_response
async def ws_register(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], "not_ready", "hub is not set up")
        return
    agent_id = str(msg[CONF_AGENT_ID]).lower()
    conf = hub.agents.get(agent_id)
    if conf is None or conf["secret"] != msg[CONF_SECRET]:
        connection.send_error(msg["id"], "unauthorized", "unknown agent or bad secret")
        return
    hub.attach(agent_id, connection)
    subscriptions = getattr(connection, "subscriptions", None)
    if isinstance(subscriptions, dict):
        # HA >= 2026: connection close runs every subscription's unsubscribe.
        subscriptions[(DOMAIN, agent_id)] = lambda: hub.detach_connection(connection)
    elif hasattr(connection, "async_register_disconnect_callback"):
        connection.async_register_disconnect_callback(
            lambda: hub.detach_connection(connection)
        )
    connection.send_result(msg["id"], {"agent_id": agent_id, "prefix": conf["prefix"]})
    _LOGGER.info("agent %s registered", agent_id)


@websocket_api.websocket_command(
    {vol.Required("type"): WS_STATES, vol.Required("states"): list}
)
@websocket_api.async_response
async def ws_states(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    hub = _hub(hass)
    agent_id = hub.agent_for_connection(connection) if hub else None
    if agent_id is None:
        connection.send_error(msg["id"], "unauthorized", "register first")
        return
    applied = 0
    for state in msg["states"]:
        if isinstance(state, dict):
            hub.apply_state(agent_id, state)
            applied += 1
    connection.send_result(msg["id"], {"applied": applied})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_STATE_CHANGED,
        vol.Required("entity_id"): cv.string,
        vol.Required("state"): cv.string,
        vol.Optional("attributes", default={}): dict,
    }
)
@websocket_api.async_response
async def ws_state_changed(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    hub = _hub(hass)
    agent_id = hub.agent_for_connection(connection) if hub else None
    if agent_id is None:
        connection.send_error(msg["id"], "unauthorized", "register first")
        return
    hub.apply_state(
        agent_id,
        {
            "entity_id": msg["entity_id"],
            "state": msg["state"],
            "attributes": msg.get("attributes", {}),
        },
    )
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {vol.Required("type"): WS_REMOVE, vol.Required("entity_id"): cv.string}
)
@websocket_api.async_response
async def ws_remove(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    hub = _hub(hass)
    agent_id = hub.agent_for_connection(connection) if hub else None
    if agent_id is None:
        connection.send_error(msg["id"], "unauthorized", "register first")
        return
    hub.remove_state(agent_id, msg["entity_id"])
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SERVICE_RESULT,
        vol.Required("ok"): bool,
        vol.Optional("request_id"): vol.Any(int, None),
        vol.Optional("error"): cv.string,
    }
)
@websocket_api.async_response
async def ws_service_result(
    hass: HomeAssistant, connection: Any, msg: dict[str, Any]
) -> None:
    hub = _hub(hass)
    agent_id = hub.agent_for_connection(connection) if hub else None
    if agent_id is None:
        connection.send_error(msg["id"], "unauthorized", "register first")
        return
    hass.bus.async_fire(
        EVENT_SERVICE_RESULT,
        {
            "agent_id": agent_id,
            "request_id": msg.get("request_id"),
            "ok": msg["ok"],
            "error": msg.get("error"),
        },
    )
    connection.send_result(msg["id"], {})
