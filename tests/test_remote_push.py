"""End-to-end test for remote_push with stubbed Home Assistant APIs.

Runs without a real Home Assistant install. Requires: aiohttp, voluptuous.
Usage: python tests/test_remote_push.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path

import aiohttp
import voluptuous as vol
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

# ---------------------------------------------------------------------------
# Minimal Home Assistant stubs
# ---------------------------------------------------------------------------
ha = types.ModuleType("homeassistant")
components = types.ModuleType("homeassistant.components")
websocket_api = types.ModuleType("homeassistant.components.websocket_api")
const = types.ModuleType("homeassistant.const")
core = types.ModuleType("homeassistant.core")
helpers = types.ModuleType("homeassistant.helpers")
cv_mod = types.ModuleType("homeassistant.helpers.config_validation")
aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
typing_mod = types.ModuleType("homeassistant.helpers.typing")

EVENT_CALL_SERVICE = "call_service"
EVENT_STATE_CHANGED = "state_changed"
EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
const.EVENT_CALL_SERVICE = EVENT_CALL_SERVICE
const.EVENT_STATE_CHANGED = EVENT_STATE_CHANGED
const.EVENT_HOMEASSISTANT_STOP = EVENT_HOMEASSISTANT_STOP
const.EVENT_HOMEASSISTANT_STARTED = EVENT_HOMEASSISTANT_STARTED


def callback(func):
    return func


def split_entity_id(entity_id: str) -> tuple[str, str]:
    domain, _, object_id = str(entity_id).partition(".")
    if not domain or not object_id:
        raise ValueError(f"invalid entity id: {entity_id}")
    return domain, object_id


core.callback = callback
core.split_entity_id = split_entity_id
core.HomeAssistant = type("HomeAssistant", (), {})
core.Event = type("Event", (), {})
core.Context = type("Context", (), {})
core.CoreState = type("CoreState", (), {"running": "running", "starting": "starting"})


class _CV:
    @staticmethod
    def string(value):
        return str(value)

    @staticmethod
    def boolean(value):
        return bool(value)

    @staticmethod
    def port(value):
        port = int(value)
        assert 0 < port < 65536
        return port

    @staticmethod
    def ensure_list(value):
        return value if isinstance(value, list) else [value]

    @staticmethod
    def entity_ids(value):
        return [str(item).lower() for item in _CV.ensure_list(value)]


cv_mod.string = _CV.string
cv_mod.boolean = _CV.boolean
cv_mod.port = _CV.port
cv_mod.ensure_list = _CV.ensure_list
cv_mod.entity_ids = _CV.entity_ids

COMMANDS: dict[str, object] = {}


def websocket_command(schema):
    def decorate(func):
        command_type = None
        for key, value in schema.items():
            if getattr(key, "schema", None) == "type":
                command_type = getattr(value, "schema", value)
                break
        if command_type is None:
            raise RuntimeError("websocket_command schema needs a type")
        func._command_type = command_type
        COMMANDS[command_type] = func
        return func

    return decorate


def async_register_command(hass, command):
    hass.ws_commands[command._command_type] = command


websocket_api.websocket_command = websocket_command
websocket_api.async_register_command = async_register_command
websocket_api.async_response = lambda func: func
websocket_api.DOMAIN = "websocket_api"

_SESSION: aiohttp.ClientSession | None = None


def async_get_clientsession(hass, verify_ssl=True):
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession()
    return _SESSION

aiohttp_client.async_get_clientsession = async_get_clientsession
typing_mod.ConfigType = dict

sys.modules.update(
    {
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.websocket_api": websocket_api,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.config_validation": cv_mod,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.typing": typing_mod,
    }
)
ha.components = components
components.websocket_api = websocket_api
helpers.config_validation = cv_mod
helpers.aiohttp_client = aiohttp_client
helpers.typing = typing_mod


# ---------------------------------------------------------------------------
# Fake Home Assistant objects
# ---------------------------------------------------------------------------
class FakeEvent:
    def __init__(self, event_type, data):
        self.event_type = event_type
        self.data = data


class FakeState:
    def __init__(self, entity_id, state, attributes):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


class FakeBus:
    def __init__(self, hass):
        self.hass = hass
        self.listeners = []
        self.events = []

    def async_listen(self, event_type, action):
        entry = (event_type, action)
        self.listeners.append(entry)
        return lambda: self.listeners.remove(entry) if entry in self.listeners else None

    def async_listen_once(self, event_type, action):
        entry = (event_type, action)

        def unsub():
            if entry in self.listeners:
                self.listeners.remove(entry)

        self.listeners.append(entry)
        return unsub

    def async_fire(self, event_type, data):
        event = FakeEvent(event_type, data)
        self.events.append(event)
        for current_type, action in list(self.listeners):
            if current_type != event_type:
                continue
            result = action(event)
            if inspect.isawaitable(result):
                asyncio.create_task(result)


class FakeStates:
    def __init__(self, hass):
        self.hass = hass
        self.entities = {}

    def async_set(self, entity_id, state, attributes=None):
        self.entities[entity_id] = FakeState(entity_id, state, dict(attributes or {}))
        self.hass.bus.async_fire(
            EVENT_STATE_CHANGED,
            {
                "entity_id": entity_id,
                "old_state": None,
                "new_state": self.entities[entity_id],
            },
        )

    def async_remove(self, entity_id):
        removed = self.entities.pop(entity_id, None)
        if removed is not None:
            self.hass.bus.async_fire(
                EVENT_STATE_CHANGED,
                {"entity_id": entity_id, "old_state": removed, "new_state": None},
            )

    def async_all(self):
        return list(self.entities.values())


class FakeServices:
    def __init__(self, hass):
        self.hass = hass
        self.handlers = {}
        self.calls = []

    def async_register(self, domain, service, handler, schema=None):
        self.handlers[(domain, service)] = handler

    def async_remove(self, domain, service):
        self.handlers.pop((domain, service), None)

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))
        self.hass.bus.async_fire(
            EVENT_CALL_SERVICE,
            {"domain": domain, "service": service, "service_data": dict(data)},
        )
        for entity_id in data.get("entity_id", []) or []:
            self.hass.states.async_set(entity_id, "on", {})


class FakeHass:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.data = {}
        self.ws_commands = {}
        self.bus = FakeBus(self)
        self.states = FakeStates(self)
        self.services = FakeServices(self)


class FakeCall:
    def __init__(self, data):
        self.data = data


# ---------------------------------------------------------------------------
# Fake websocket server speaking the HA auth handshake
# ---------------------------------------------------------------------------
class FakeConnection:
    def __init__(self, ws):
        self.ws = ws
        self._disconnect_callback = None

    def async_register_disconnect_callback(self, callback):
        self._disconnect_callback = callback

    async def send_json(self, data):
        await self.ws.send_json(data)

    def send_result(self, msg_id, data):
        asyncio.create_task(
            self.ws.send_json(
                {"id": msg_id, "type": "result", "success": True, "result": data}
            )
        )

    def send_error(self, msg_id, code, message):
        asyncio.create_task(
            self.ws.send_json(
                {
                    "id": msg_id,
                    "type": "result",
                    "success": False,
                    "error": {"code": code, "message": message},
                }
            )
        )


def make_app(hub_hass: FakeHass) -> web.Application:
    async def handle(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.1"})
        hello = await ws.receive_json()
        assert hello["type"] == "auth", hello
        assert hello["access_token"] == "hub-token", hello
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.1"})
        connection = FakeConnection(ws)
        async for message in ws:
            data = message.json()
            command = hub_hass.ws_commands.get(data.get("type"))
            if command is None:
                connection.send_error(data.get("id"), "unknown_command", data.get("type"))
                continue
            try:
                await command(hub_hass, connection, data)
            except Exception as err:  # surface errors in the test instead of hanging
                connection.send_error(data.get("id"), "error", str(err))
        if connection._disconnect_callback is not None:
            connection._disconnect_callback()
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", handle)
    return app


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> None:
    from remote_push.agent import PushAgent
    from remote_push.const import EVENT_SERVICE_RESULT
    from remote_push.hub import PushHub

    hub_hass = FakeHass()
    hub = PushHub(
        hub_hass,
        [{"agent_id": "b", "secret": "shared-secret", "prefix": "b_"}],
    )
    hub_hass.data["remote_push"] = {"hub": hub}
    await hub.async_start()

    runner = web.AppRunner(make_app(hub_hass))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    agent_hass = FakeHass()
    agent_hass.states.async_set("sensor.temp", "23", {"friendly_name": "Temp"})
    agent_hass.states.async_set("light.living", "off", {"friendly_name": "Living"})
    agent_hass.states.async_set("sensor.ignored", "0", {})

    agent = PushAgent(
        agent_hass,
        {
            "agent_id": "b",
            "secret": "shared-secret",
            "host": "127.0.0.1",
            "port": port,
            "secure": False,
            "token": "hub-token",
            "include_domains": ["sensor", "light"],
            "exclude_entities": ["sensor.ignored"],
        },
    )
    await agent.async_start()

    assert await wait_for(
        lambda: "sensor.b_temp" in hub_hass.states.entities
        and "light.b_living" in hub_hass.states.entities
    ), "initial state push failed"
    assert "sensor.b_ignored" not in hub_hass.states.entities, "exclude filter failed"
    assert hub_hass.states.entities["sensor.b_temp"].state == "23"
    assert hub_hass.states.entities["sensor.b_temp"].attributes["friendly_name"] == "[b] Temp"
    print("PASS  initial state push + prefix + filters")

    agent_hass.states.async_set("sensor.temp", "24", {"friendly_name": "Temp"})
    assert await wait_for(
        lambda: hub_hass.states.entities["sensor.b_temp"].state == "24"
    ), "state_changed push failed"
    print("PASS  incremental state_changed push")

    await hub.async_call_service(
        FakeCall(
            {
                "agent_id": "b",
                "domain": "light",
                "service": "turn_on",
                "entity_id": ["light.b_living"],
                "service_data": {"brightness": 200},
            }
        )
    )
    assert await wait_for(
        lambda: any(
            call[0] == "light"
            and call[1] == "turn_on"
            and call[2].get("entity_id") == ["light.living"]
            and call[2].get("brightness") == 200
            for call in agent_hass.services.calls
        )
    ), "proxy service call not forwarded"
    assert await wait_for(
        lambda: any(
            event.event_type == EVENT_SERVICE_RESULT and event.data["ok"]
            for event in hub_hass.bus.events
        )
    ), "service result not reported"
    print("PASS  service call forwarding + result event")

    hub_hass.bus.async_fire(
        EVENT_CALL_SERVICE,
        {
            "domain": "light",
            "service": "turn_off",
            "service_data": {"entity_id": "light.b_living"},
        },
    )
    assert await wait_for(
        lambda: any(
            call[1] == "turn_off" and call[2].get("entity_id") == ["light.living"]
            for call in agent_hass.services.calls
        )
    ), "entity-level service interception failed"
    print("PASS  entity-level service interception")

    await agent._async_stop()
    assert await wait_for(
        lambda: "sensor.b_temp" not in hub_hass.states.entities
    ), "stale states were not removed on disconnect"
    print("PASS  disconnect cleanup")

    if _SESSION is not None:
        await _SESSION.close()
    await runner.cleanup()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
