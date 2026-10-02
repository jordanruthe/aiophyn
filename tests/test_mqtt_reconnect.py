"""Tests for the MQTT reconnect state machine (homeassistant-phyn #66).

Verifies that:
- _on_disconnect schedules a reconnect even though paho still reports
  is_connected() == True inside the callback (the #66 regression).
- _on_disconnect after an intentional disconnect() does NOT reconnect.
- _do_reconnect clears connect_evt before each attempt and re-arms the
  backstop timer when it exits without a live connection.
- _process_reconnect's keepalive refresh is bounded and always clears the
  intentional-disconnect marker.
- Timer.cancel()/start() invoked from inside the running callback do not
  cancel the callback itself.
- AIOHelper ignores the close of a superseded socket.
- ensure_connected() gives callers a cheap recovery path.
"""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import paho.mqtt.client as paho_mqtt
from paho.mqtt.client import _ConnectionState as CS

import aiophyn.mqtt as mqtt_module
from aiophyn.mqtt import AIOHelper, MQTTClient, Timer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mqtt_client(*, connected: bool) -> MQTTClient:
    """Minimal MQTTClient for testing, bypassing __init__ (mock paho)."""
    mqtt = MQTTClient.__new__(MQTTClient)
    mqtt.disconnect_evt = None
    mqtt.connect_task = None
    mqtt.reconnect_timer = MagicMock()
    mqtt.reconnect_evt = asyncio.Event()
    mqtt.connect_evt = asyncio.Event()
    mqtt.topics = []
    mqtt.pending_acks = {}
    mqtt.port = 443
    mqtt.host = None

    paho = MagicMock()
    paho.is_connected = MagicMock(return_value=connected)
    paho.disconnect = MagicMock()
    mqtt.client = paho
    return mqtt


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _on_disconnect
# ---------------------------------------------------------------------------

class TestOnDisconnect:
    def test_unexpected_drop_spawns_reconnect_while_paho_still_reports_connected(self):
        """Regression for #66.

        Inside paho's on_disconnect callback the client still reports
        is_connected() == True (paho flips its state only after the callback
        returns). The old ``elif not self.is_connected()`` guard therefore
        never matched and nothing was ever scheduled.
        """
        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            mqtt._do_reconnect = AsyncMock()

            mqtt._on_disconnect(mqtt.client, None, paho_mqtt.MQTT_ERR_CONN_LOST)
            await _settle()

            assert mqtt.connect_task is not None
            mqtt._do_reconnect.assert_awaited_once_with(True)
            mqtt.reconnect_timer.start.assert_called_once_with(
                mqtt_module._RECONNECT_WATCHDOG_INTERVAL
            )
            assert not mqtt.connect_evt.is_set()

        asyncio.run(asyncio.wait_for(_run(), timeout=2))

    def test_real_paho_callback_path_spawns_reconnect(self):
        """Drive paho's actual _loop_rc_handle so the callback timing is real."""
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._do_reconnect = AsyncMock()
            client = paho_mqtt.Client(client_id="x", transport="websockets")
            client.on_disconnect = mqtt._on_disconnect
            client._state = CS.MQTT_CS_CONNECTED
            client._sock = type("S", (), {"close": lambda self: None})()
            mqtt.client = client

            client._loop_rc_handle(paho_mqtt.MQTT_ERR_CONN_LOST)
            await _settle()

            assert mqtt.connect_task is not None
            mqtt._do_reconnect.assert_awaited_once()

        asyncio.run(asyncio.wait_for(_run(), timeout=2))

    def test_intentional_disconnect_does_not_reconnect(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            mqtt._do_reconnect = AsyncMock()
            mqtt.disconnect_evt = asyncio.Event()

            mqtt._on_disconnect(mqtt.client, None, 0)
            await _settle()

            assert mqtt.disconnect_evt.is_set()
            assert mqtt.connect_task is None
            mqtt._do_reconnect.assert_not_awaited()
            mqtt.reconnect_timer.start.assert_not_called()

        asyncio.run(_run())

    def test_does_not_spawn_second_loop_when_one_is_running(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            running = asyncio.get_running_loop().create_future()
            mqtt.connect_task = asyncio.ensure_future(running)

            mqtt._on_disconnect(mqtt.client, None, paho_mqtt.MQTT_ERR_CONN_LOST)
            await _settle()

            assert mqtt.connect_task is not None
            assert not mqtt.connect_task.done()
            running.cancel()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _do_reconnect
# ---------------------------------------------------------------------------

class TestDoReconnect:
    def test_clears_connect_evt_before_attempt_and_verifies_against_paho(self):
        """A stale connect_evt must not satisfy the CONNACK wait."""
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt.connect_evt.set()  # stale from an earlier session
            mqtt.get_mqtt_info = AsyncMock(return_value=("host", "/mqtt"))
            attempts = []

            def fake_connect(host, port):
                attempts.append(host)
                if len(attempts) == 2:
                    mqtt.client.is_connected.return_value = True
                    mqtt.event_loop.call_soon_threadsafe(mqtt.connect_evt.set)

            mqtt.client.connect = fake_connect
            mqtt.event_loop = asyncio.get_running_loop()

            # Make the inter-attempt throttle instant.
            orig_sleep = asyncio.sleep

            async def fast_sleep(t):
                await orig_sleep(0)

            mqtt_module.asyncio.sleep = fast_sleep
            try:
                await asyncio.wait_for(mqtt._do_reconnect(True), timeout=5)
            finally:
                mqtt_module.asyncio.sleep = orig_sleep

            # First attempt had no CONNACK -> retried; second connected.
            assert len(attempts) == 2
            assert not mqtt.reconnect_evt.is_set()
            assert mqtt.connect_task is None

        asyncio.run(_run())

    def test_rearms_backstop_timer_when_loop_exits_disconnected(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt.get_mqtt_info = AsyncMock(side_effect=asyncio.CancelledError())

            try:
                await mqtt._do_reconnect(True)
            except asyncio.CancelledError:
                pass

            assert not mqtt.reconnect_evt.is_set()
            assert mqtt.connect_task is None
            mqtt.reconnect_timer.start.assert_called_with(
                mqtt_module._RECONNECT_WATCHDOG_INTERVAL
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _process_reconnect
# ---------------------------------------------------------------------------

class TestProcessReconnect:
    def test_disconnected_spawns_reconnect(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._do_reconnect = AsyncMock()

            await mqtt._process_reconnect()
            await _settle()

            mqtt._do_reconnect.assert_awaited_once_with(True)
            mqtt.reconnect_timer.start.assert_called_once_with(
                mqtt_module._RECONNECT_WATCHDOG_INTERVAL
            )

        asyncio.run(_run())

    def test_keepalive_refresh_is_bounded_and_clears_marker(self, monkeypatch):
        """If paho never fires on_disconnect, the refresh must not hang and
        disconnect_evt must be cleared so later drops still reconnect."""
        monkeypatch.setattr(mqtt_module, "_DISCONNECT_WAIT_TIMEOUT", 0.05)

        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            mqtt._do_reconnect = AsyncMock()

            await asyncio.wait_for(mqtt._process_reconnect(), timeout=2)
            await _settle()

            mqtt.client.disconnect.assert_called_once()
            assert mqtt.disconnect_evt is None
            mqtt._do_reconnect.assert_awaited_once_with(True)

        asyncio.run(_run())

    def test_keepalive_refresh_normal_path(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            mqtt._do_reconnect = AsyncMock()

            def fake_disconnect():
                mqtt._on_disconnect(mqtt.client, None, 0)

            mqtt.client.disconnect = fake_disconnect

            await asyncio.wait_for(mqtt._process_reconnect(), timeout=2)
            await _settle()

            assert mqtt.disconnect_evt is None
            mqtt._do_reconnect.assert_awaited_once_with(True)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# ensure_connected
# ---------------------------------------------------------------------------

class TestEnsureConnected:
    def test_connected_returns_true_without_spawning(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=True)
            mqtt._do_reconnect = AsyncMock()

            assert mqtt.ensure_connected() is True
            await _settle()

            assert mqtt.connect_task is None
            mqtt.reconnect_timer.start.assert_not_called()

        asyncio.run(_run())

    def test_disconnected_spawns_reconnect(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._do_reconnect = AsyncMock()

            assert mqtt.ensure_connected() is False
            await _settle()

            mqtt._do_reconnect.assert_awaited_once_with(True)
            mqtt.reconnect_timer.start.assert_called_once_with(
                mqtt_module._RECONNECT_WATCHDOG_INTERVAL
            )

        asyncio.run(_run())

    def test_intentional_disconnect_is_respected(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._do_reconnect = AsyncMock()
            mqtt.disconnect()  # sets disconnect_evt

            assert mqtt.ensure_connected() is False
            await _settle()

            mqtt._do_reconnect.assert_not_awaited()

        asyncio.run(_run())

    def test_in_progress_reconnect_is_left_alone(self):
        async def _run():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._do_reconnect = AsyncMock()
            mqtt.reconnect_evt.set()

            assert mqtt.ensure_connected() is False
            await _settle()

            mqtt._do_reconnect.assert_not_awaited()
            mqtt.reconnect_timer.start.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------

class TestTimerReentrancy:
    def test_restart_from_inside_callback_does_not_cancel_callback(self):
        """_process_reconnect restarts the timer from within the timer's own
        callback; that must not cancel the callback mid-flight."""
        async def _run():
            events = []

            async def cb():
                events.append("start")
                timer.start(60)  # re-arm from inside
                await asyncio.sleep(0)
                events.append("end")

            timer = Timer(cb)
            timer.start(0.01)
            await asyncio.sleep(0.1)
            timer.cancel()

            assert events == ["start", "end"]

        asyncio.run(_run())

    def test_cancel_from_inside_callback_does_not_cancel_callback(self):
        async def _run():
            events = []

            async def cb():
                events.append("start")
                timer.cancel()
                await asyncio.sleep(0)
                events.append("end")

            timer = Timer(cb)
            timer.start(0.01)
            await asyncio.sleep(0.1)

            assert events == ["start", "end"]

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# AIOHelper superseded socket
# ---------------------------------------------------------------------------

class TestAIOHelperSupersededSocket:
    def test_stale_socket_close_keeps_live_misc_loop(self):
        async def _run():
            client = MagicMock()
            client.loop_misc = MagicMock(return_value=paho_mqtt.MQTT_ERR_SUCCESS)
            helper = AIOHelper(client)
            a1, b1 = socket.socketpair()
            a2, b2 = socket.socketpair()
            try:
                helper._on_socket_open(client, None, a1)
                helper._on_socket_open(client, None, a2)
                live = helper.misc_task
                await asyncio.sleep(0.02)

                helper._on_socket_close(client, None, a1)  # stale
                await asyncio.sleep(0.02)
                assert helper.misc_task is live and not live.done()

                helper._on_socket_close(client, None, a2)  # live
                await asyncio.sleep(0.02)
                assert live.done()
            finally:
                for s in (a1, b1, a2, b2):
                    s.close()

        asyncio.run(asyncio.wait_for(_run(), timeout=5))
