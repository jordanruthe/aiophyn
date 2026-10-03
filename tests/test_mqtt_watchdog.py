"""Tests for the MQTT silence watchdog.

A Phyn device stops publishing after it opens a new cloud session (Wi-Fi
rejoin, reboot, power cut) until a client SUBSCRIBEs again, while the MQTT
connection stays healthy. The watchdog notices the silence and resubscribes.

Verifies that:
- nothing happens while messages flow, or while the client is down,
  reconnecting, mid-handshake or intentionally disconnected;
- after _SILENCE_THRESHOLD of silence it runs resubscribe, then
  unsubscribe+subscribe, then reconnect, then stands down exactly once;
- no step runs while the Phyn cloud reports the device offline, the hold is
  logged once, and a failing cloud read does not block the step;
- a step that raises does not escape;
- a SUBACK, whoever sent it, restarts the silence clock, and the resume line
  reports the SUBACK delay and the seq_num pair;
- disconnect() cancels the watchdog task.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import aiophyn.mqtt as mqtt_module
from aiophyn.mqtt import MQTTClient

TOPIC = "prd/app_subscriptions/DEVICE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mqtt_client(*, connected: bool = True, online: object = "online") -> MQTTClient:
    """Minimal subscribed MQTTClient, bypassing __init__ (mock paho and API).

    ``online`` is the cloud's online_status ("online"/"offline"), or an
    exception instance to make the cloud read fail.
    """
    mqtt = MQTTClient.__new__(MQTTClient)
    mqtt.disconnect_evt = None
    mqtt.connect_task = None
    mqtt.reconnect_timer = MagicMock()
    mqtt.reconnect_evt = asyncio.Event()
    mqtt.connect_evt = asyncio.Event()
    mqtt.topics = [TOPIC]
    mqtt.pending_acks = {}

    paho = MagicMock()
    paho.is_connected = MagicMock(return_value=connected)
    mqtt.client = paho

    mqtt.subscribe = AsyncMock()
    mqtt._process_reconnect = AsyncMock()

    api = MagicMock()
    if isinstance(online, Exception):
        api.device.get_state = AsyncMock(side_effect=online)
    else:
        api.device.get_state = AsyncMock(
            return_value={"online_status": {"v": online, "ts": 0, "sid": "secret-sid"}}
        )
    mqtt.api = api

    now = time.monotonic()
    mqtt._wd_last_rx = now
    mqtt._wd_last_suback = now
    return mqtt


def _go_silent(mqtt: MQTTClient, seconds: float = 120.0) -> None:
    """Pretend the last message and the last SUBACK were ``seconds`` ago."""
    then = time.monotonic() - seconds
    mqtt._wd_last_rx = then
    mqtt._wd_last_suback = then


def _skip_wait(mqtt: MQTTClient) -> None:
    """Let the next step run now instead of after _SILENCE_STEP_SPACING."""
    mqtt._wd_next_at = 0.0


def _message(mqtt: MQTTClient, seq: int) -> None:
    msg = MagicMock()
    msg.topic = TOPIC
    msg.payload = ('{"seq_num": %d}' % seq).encode()
    mqtt._handlers = {"update": []}
    mqtt._on_message(mqtt.client, None, msg)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=2))


# ---------------------------------------------------------------------------
# When the watchdog must stay quiet
# ---------------------------------------------------------------------------

class TestNoAction:
    def test_recent_message_does_nothing(self):
        async def _t():
            mqtt = _make_mqtt_client()
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
            mqtt.api.device.get_state.assert_not_awaited()
            assert not mqtt._wd_silent
        _run(_t())

    def test_disconnected_reconnecting_handshake_or_intentional_does_nothing(self):
        async def _t():
            cases = []
            m = _make_mqtt_client(connected=False); cases.append(m)
            m = _make_mqtt_client(); m.reconnect_evt.set(); cases.append(m)
            m = _make_mqtt_client(); m.pending_acks[7] = TOPIC; cases.append(m)
            m = _make_mqtt_client(); m.disconnect_evt = asyncio.Event(); cases.append(m)
            m = _make_mqtt_client(); m.topics = []; cases.append(m)
            for mqtt in cases:
                _go_silent(mqtt)
                await mqtt._watchdog_tick()
                mqtt.subscribe.assert_not_awaited()
                mqtt._process_reconnect.assert_not_awaited()
                assert not mqtt._wd_silent
        _run(_t())

    def test_recent_suback_restarts_the_silence_clock(self):
        async def _t():
            mqtt = _make_mqtt_client()
            _go_silent(mqtt)
            mqtt.pending_acks[3] = TOPIC
            mqtt._on_subscribe(mqtt.client, None, 3, [0])
            assert 3 not in mqtt.pending_acks
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
        _run(_t())


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

class TestLadder:
    def test_first_step_is_a_plain_resubscribe(self):
        async def _t():
            mqtt = _make_mqtt_client()
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_awaited_once_with(TOPIC)
            mqtt.client.unsubscribe.assert_not_called()
            mqtt._process_reconnect.assert_not_awaited()
            assert mqtt._wd_last_step == "resubscribe"
            assert mqtt._wd_next_at > time.monotonic()
        _run(_t())

    def test_waits_between_steps(self):
        async def _t():
            mqtt = _make_mqtt_client()
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
            await mqtt._watchdog_tick()  # inside _SILENCE_STEP_SPACING
            assert mqtt.subscribe.await_count == 1
        _run(_t())

    def test_step_order_then_stand_down_once(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _go_silent(mqtt)
            await mqtt._watchdog_tick()              # resubscribe
            _skip_wait(mqtt)
            await mqtt._watchdog_tick()              # unsubscribe+subscribe
            mqtt.client.unsubscribe.assert_called_once_with(TOPIC)
            assert mqtt.subscribe.await_count == 2
            _skip_wait(mqtt)
            await mqtt._watchdog_tick()              # reconnect
            mqtt._process_reconnect.assert_awaited_once()
            for _ in range(3):
                _skip_wait(mqtt)
                await mqtt._watchdog_tick()          # stand down, then nothing
            assert mqtt.subscribe.await_count == 2
            mqtt._process_reconnect.assert_awaited_once()
        with caplog.at_level(logging.WARNING, logger="aiophyn.mqtt"):
            _run(_t())
        assert sum("WATCHDOG stand down" in r.message for r in caplog.records) == 1

    def test_raising_step_does_not_escape(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            mqtt.subscribe = AsyncMock(side_effect=OSError("socket gone"))
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
            assert mqtt._wd_step == 1
        with caplog.at_level(logging.WARNING, logger="aiophyn.mqtt"):
            _run(_t())
        assert any("raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cloud state gate
# ---------------------------------------------------------------------------

class TestOfflineHold:
    def test_offline_device_is_held_and_logged_once(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client(online="offline")
            _go_silent(mqtt)
            for _ in range(3):
                _skip_wait(mqtt)
                await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
            assert mqtt.api.device.get_state.await_count == 3
            assert mqtt._wd_next_at > time.monotonic()   # re-check scheduled
            return mqtt
        with caplog.at_level(logging.INFO, logger="aiophyn.mqtt"):
            _run(_t())
        holds = [r for r in caplog.records if "holding" in r.message]
        assert len(holds) == 1

    def test_step_taken_once_device_is_back(self):
        async def _t():
            mqtt = _make_mqtt_client(online="offline")
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
            mqtt.api.device.get_state = AsyncMock(
                return_value={"online_status": {"v": "online", "ts": 0}}
            )
            _skip_wait(mqtt)
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_awaited_once_with(TOPIC)
        _run(_t())

    def test_failed_cloud_read_does_not_block_the_step(self):
        async def _t():
            mqtt = _make_mqtt_client(online=RuntimeError("api down"))
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_awaited_once_with(TOPIC)
        _run(_t())

    def test_session_id_and_credentials_are_not_logged(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _go_silent(mqtt)
            await mqtt._watchdog_tick()
        with caplog.at_level(logging.DEBUG, logger="aiophyn.mqtt"):
            _run(_t())
        assert not any("secret-sid" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Resume reporting and lifecycle
# ---------------------------------------------------------------------------

class TestResume:
    def test_message_resets_state_and_reports_suback_delay_and_seq(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _message(mqtt, 41)
            _go_silent(mqtt)
            await mqtt._watchdog_tick()                    # step 1
            mqtt._wd_last_suback = time.monotonic()        # its SUBACK
            _message(mqtt, 0)
            assert not mqtt._wd_silent
            assert mqtt._wd_step == 0 and mqtt._wd_last_step is None
        with caplog.at_level(logging.WARNING, logger="aiophyn.mqtt"):
            _run(_t())
        line = next(r.getMessage() for r in caplog.records
                    if "stream resumed" in r.getMessage())
        assert "after SUBACK" in line and "'resubscribe'" in line
        assert "seq 41 -> 0" in line

    def test_resume_without_a_step_is_reported_as_such(self, caplog):
        async def _t():
            mqtt = _make_mqtt_client(online="offline")
            _message(mqtt, 5)
            _go_silent(mqtt)
            await mqtt._watchdog_tick()                    # held, no step
            _message(mqtt, 6)
        with caplog.at_level(logging.WARNING, logger="aiophyn.mqtt"):
            _run(_t())
        assert any("resumed on its own" in r.getMessage() and "seq 5 -> 6" in r.getMessage()
                   for r in caplog.records)

    def test_disconnect_cancels_the_watchdog_task(self):
        async def _t():
            mqtt = _make_mqtt_client(connected=False)
            mqtt._wd_task = asyncio.create_task(asyncio.sleep(60))
            mqtt.disconnect()
            await asyncio.sleep(0)
            assert mqtt._wd_task.cancelled()
        _run(_t())

    def test_kill_switch_exists(self):
        assert mqtt_module._SILENCE_WATCHDOG_ENABLED is True
