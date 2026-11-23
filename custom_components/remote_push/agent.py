"""Agent side of remote_push: runs on B and pushes states outbound to A."""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

import aiohttp

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, Event, split_entity_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AGENT_ID,
    CONF_EXCLUDE_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_HOST,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_MAX_MSG_SIZE,
    CONF_PORT,
    CONF_SECRET,
    CONF_SECURE,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DEFAULT_MAX_MSG_SIZE,
    DEFAULT_PORT,
    RECONNECT_DELAY,
    WS_REGISTER,
    WS_REMOVE,
    WS_SERVICE_CALL,
    WS_SERVICE_RESULT,
    WS_STATE_CHANGED,
    WS_STATES,
)

_LOGGER = logging.getLogger(__name__)


class PushAgent:
    """Maintain an outbound websocket connection from B to hub A."""

    def __init__(self, hass: HomeAssistant, conf: dict[str, Any]) -> None:
        self.hass = hass
        self.conf = conf
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._seq = itertools.count(1)
        self._unsub_stop: Any = None

    async def async_start(self) -> None:
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_stop
        )
        self._task = self.hass.loop.create_task(self._run())
        _LOGGER.info("remote_push agent started, target=%s", self._url())

    async def _async_stop(self, event: Event | None = None) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()

    def _url(self) -> str:
        scheme = "wss" if self.conf.get(CONF_SECURE, False) else "ws"
        host = self.conf[CONF_HOST]
        port = self.conf.get(CONF_PORT, DEFAULT_PORT)
        return f"{scheme}://{host}:{port}/api/websocket"

    def _wanted(self, entity_id: str) -> bool:
        entity_id = entity_id.lower()
        domain, _ = split_entity_id(entity_id)
        if entity_id in {e.lower() for e in self.conf.get(CONF_EXCLUDE_ENTITIES, [])}:
            return False
        if domain in set(self.conf.get(CONF_EXCLUDE_DOMAINS, [])):
            return False
        include_entities = {
            e.lower() for e in self.conf.get(CONF_INCLUDE_ENTITIES, [])
        }
        include_domains = set(self.conf.get(CONF_INCLUDE_DOMAINS, []))
        if include_entities or include_domains:
            return entity_id in include_entities or domain in include_domains
        return True

    def _collect_states(self) -> list[dict[str, Any]]:
        result = []
        for state in self.hass.states.async_all():
            if not self._wanted(state.entity_id):
                continue
            result.append(
                {
                    "entity_id": state.entity_id,
                    "state": state.state,
                    "attributes": dict(state.attributes),
                }
            )
        return result

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep retrying on any transport error
                _LOGGER.exception("remote_push session failed, retrying")
            if self._stopping:
                break
            await asyncio.sleep(RECONNECT_DELAY)

    async def _session(self) -> None:
        session = async_get_clientsession(
            self.hass, self.conf.get(CONF_VERIFY_SSL, True)
        )
        url = self._url()
        _LOGGER.info("connecting to hub %s", url)
        ws = await session.ws_connect(
            url, max_msg_size=self.conf.get(CONF_MAX_MSG_SIZE, DEFAULT_MAX_MSG_SIZE)
        )
        self._ws = ws
        unsub = None
        try:
            await self._handshake(ws)
            states = self._collect_states()
            await ws.send_json(
                {
                    "id": next(self._seq),
                    "type": WS_STATES,
                    "states": states,
                }
            )
            _LOGGER.info("pushed %d states to hub", len(states))
            unsub = self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_state_changed
            )
            async for msg in ws:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
                try:
                    data = msg.json()
                except (TypeError, ValueError):
                    continue
                if data.get("type") == WS_SERVICE_CALL:
                    await self._handle_service_call(data)
        finally:
            if unsub is not None:
                unsub()
            self._ws = None
            await ws.close()

    async def _handshake(self, ws: Any) -> None:
        hello = await ws.receive_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected handshake: {hello}")

        token = self.conf.get(CONF_TOKEN)
        if not token:
            raise RuntimeError("access token for hub A is required")
        await ws.send_json({"type": "auth", "access_token": token})

        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "auth_ok":
                break
            if msg.get("type") == "auth_invalid":
                raise RuntimeError("hub rejected the access token")
            if msg.get("type") == "result" and not msg.get("success"):
                raise RuntimeError(f"auth failed: {msg}")

        register_id = next(self._seq)
        await ws.send_json(
            {
                "id": register_id,
                "type": WS_REGISTER,
                CONF_AGENT_ID: self.conf[CONF_AGENT_ID],
                CONF_SECRET: self.conf[CONF_SECRET],
            }
        )
        while True:
            msg = await ws.receive_json()
            if msg.get("id") == register_id:
                if not msg.get("success"):
                    raise RuntimeError(f"agent register failed: {msg}")
                break

    def _on_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if not entity_id or not self._wanted(entity_id):
            return
        new_state = event.data.get("new_state")
        if new_state is None:
            payload = {"type": WS_REMOVE, "entity_id": entity_id}
        else:
            payload = {
                "type": WS_STATE_CHANGED,
                "entity_id": entity_id,
                "state": new_state.state,
                "attributes": dict(new_state.attributes),
            }
        self._schedule_send(payload)

    def _schedule_send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            return
        self.hass.loop.create_task(self._safe_send(payload))

    async def _safe_send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_json(payload)
        except Exception as err:  # noqa: BLE001 - connection may drop mid-send
            _LOGGER.error("failed to send state to hub: %s", err)

    async def _handle_service_call(self, data: dict[str, Any]) -> None:
        domain = data.get("domain")
        service = data.get("service")
        service_data = dict(data.get("service_data") or {})
        try:
            await self.hass.services.async_call(
                domain, service, service_data, blocking=True
            )
            result = {"ok": True}
        except Exception as err:  # noqa: BLE001 - report back to hub
            result = {"ok": False, "error": str(err)[:200]}
        result["type"] = WS_SERVICE_RESULT
        result["id"] = data.get("id")
        await self._safe_send(result)
