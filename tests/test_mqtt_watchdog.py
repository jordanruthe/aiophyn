"""Tests for the MQTT silence watchdog.

A Phyn device stops publishing after it reconnects to the Phyn cloud (Wi-Fi
rejoin, reboot, power cut, or just its cloud connection dropping) until a
client SUBSCRIBEs again, while our MQTT connection stays healthy. The
watchdog notices the silence per device topic and resubscribes that topic.

Verifies that:
- nothing happens while messages flow, or while the client is down,
  reconnecting or intentionally disconnected, or while a topic's own
  SUBSCRIBE is pending;
- silence is tracked per device topic: one device going quiet is noticed
  while another keeps publishing, and only the quiet topic is resubscribed
  and only its device's state is read;
- after _SILENCE_THRESHOLD of silence an online (or unknown) device is
  resubscribed, then re-checked every _SILENCE_RETRY, slowing to
  _SILENCE_SLOW_RETRY after _SILENCE_FAST_PERIOD;
- an offline device is not resubscribed; it is re-checked with the same
  backoff and resubscribed as soon as the cloud reports it online;
- routine recoveries log at INFO; a WARNING appears only after
  _SILENCE_WARN_AFTER resubscribes to an online device did not help;
- the watchdog never uses the wall clock (no clock-skew dependence);
- a raising resubscribe or state read does not escape;
- disconnect() cancels the watchdog task.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import aiophyn.mqtt as mqtt_module
from aiophyn.mqtt import MQTTClient

DEV_A = "DEVICEA"
DEV_B = "DEVICEB"
TOPIC_A = f"prd/app_subscriptions/{DEV_A}"
TOPIC_B = f"prd/app_subscriptions/{DEV_B}"
LOGGER = "aiophyn.mqtt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeClock:
    """Stands in for the time module inside aiophyn.mqtt."""

    def __init__(self):
        self.t = 1_000_000.0

    def monotonic(self):
        return self.t

    def time(self):  # the watchdog must not need the wall clock
        raise AssertionError("watchdog used the wall clock")


@pytest.fixture
def clock(monkeypatch):
    c = _FakeClock()
    monkeypatch.setattr(mqtt_module, "time", c)
    return c


def _make_mqtt_client(states: dict | None = None, *, topics=(TOPIC_A,),
                      connected: bool = True) -> MQTTClient:
    """Minimal subscribed MQTTClient, bypassing __init__ (mock paho and API).

    ``states`` maps device id -> "online" / "offline" / an exception instance
    (cloud read fails).  Devices not listed are online.
    """
    states = dict(states or {})
    mqtt = MQTTClient.__new__(MQTTClient)
    mqtt.disconnect_evt = None
    mqtt.connect_task = None
    mqtt.reconnect_timer = MagicMock()
    mqtt.reconnect_evt = asyncio.Event()
    mqtt.connect_evt = asyncio.Event()
    mqtt.topics = list(topics)
    mqtt.pending_acks = {}
    mqtt._handlers = {"update": []}

    paho = MagicMock()
    paho.is_connected = MagicMock(return_value=connected)
    mqtt.client = paho
    mqtt.subscribe = AsyncMock()
    mqtt._process_reconnect = AsyncMock()

    async def get_state(device_id):
        v = states.get(device_id, "online")
        if isinstance(v, Exception):
            raise v
        return {"online_status": {"v": v, "ts": 0, "sid": "secret-sid"}}

    api = MagicMock()
    api.device.get_state = AsyncMock(side_effect=get_state)
    mqtt.api = api
    mqtt._states = states          # tests change device states through this
    return mqtt


def _message(mqtt: MQTTClient, topic: str, seq: int | None = None) -> None:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = (b"{}" if seq is None else ('{"seq_num": %d}' % seq).encode())
    mqtt._on_message(mqtt.client, None, msg)


def _start(mqtt: MQTTClient, clock: _FakeClock, *topics: str) -> None:
    """Every topic has just delivered a message."""
    for topic in topics or mqtt.topics:
        _message(mqtt, topic, 1)


def _state_reads(mqtt: MQTTClient) -> list:
    return [c.args[0] for c in mqtt.api.device.get_state.await_args_list]


def _subscribes(mqtt: MQTTClient) -> list:
    return [c.args[0] for c in mqtt.subscribe.await_args_list]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=2))


THRESHOLD = mqtt_module._SILENCE_THRESHOLD
RETRY = mqtt_module._SILENCE_RETRY
SLOW = mqtt_module._SILENCE_SLOW_RETRY
FAST_PERIOD = mqtt_module._SILENCE_FAST_PERIOD
WARN_AFTER = mqtt_module._SILENCE_WARN_AFTER


# ---------------------------------------------------------------------------
# When the watchdog must stay quiet
# ---------------------------------------------------------------------------

class TestNoAction:
    def test_recent_message_does_nothing(self, clock):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD - 1
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
            mqtt.api.device.get_state.assert_not_awaited()
        _run(_t())

    def test_disconnected_reconnecting_or_leaving_does_nothing(self, clock):
        async def _t():
            cases = [_make_mqtt_client(connected=False)]
            m = _make_mqtt_client(); m.reconnect_evt.set(); cases.append(m)
            m = _make_mqtt_client(); m.disconnect_evt = asyncio.Event(); cases.append(m)
            m = _make_mqtt_client(topics=()); cases.append(m)
            for mqtt in cases:
                _start(mqtt, clock)
            clock.t += 10 * THRESHOLD
            for mqtt in cases:
                await mqtt._watchdog_tick()
                mqtt.subscribe.assert_not_awaited()
                mqtt.api.device.get_state.assert_not_awaited()
        _run(_t())

    def test_pending_subscribe_skips_only_that_topic(self, clock):
        async def _t():
            mqtt = _make_mqtt_client(topics=(TOPIC_A, TOPIC_B))
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            mqtt.pending_acks[7] = TOPIC_A
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt) == [TOPIC_B]
        _run(_t())

    def test_suback_restarts_that_topics_silence_clock(self, clock):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            mqtt.pending_acks[3] = TOPIC_A
            mqtt._on_subscribe(mqtt.client, None, 3, [0])
            assert 3 not in mqtt.pending_acks
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
        _run(_t())

    def test_non_device_topics_are_ignored(self, clock):
        async def _t():
            mqtt = _make_mqtt_client(topics=("some/other/topic",))
            clock.t += 10 * THRESHOLD
            await mqtt._watchdog_tick()
            mqtt.subscribe.assert_not_awaited()
            mqtt.api.device.get_state.assert_not_awaited()
        _run(_t())


# ---------------------------------------------------------------------------
# Several devices on one client
# ---------------------------------------------------------------------------

class TestPerDevice:
    def test_quiet_device_noticed_while_another_publishes(self, clock):
        async def _t():
            mqtt = _make_mqtt_client(topics=(TOPIC_A, TOPIC_B))
            _start(mqtt, clock)
            for _ in range(int(THRESHOLD // 5) + 2):   # A keeps publishing, B is silent
                clock.t += 5
                _message(mqtt, TOPIC_A, 2)
                await mqtt._watchdog_tick()
            assert _state_reads(mqtt) == [DEV_B]
            assert _subscribes(mqtt) == [TOPIC_B]
        _run(_t())

    def test_each_device_checked_against_its_own_state(self, clock):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"}, topics=(TOPIC_A, TOPIC_B))
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            assert sorted(_state_reads(mqtt)) == [DEV_A, DEV_B]
            assert _subscribes(mqtt) == [TOPIC_B]       # A is offline: left alone
        _run(_t())

    def test_resume_on_one_topic_does_not_reset_the_other(self, clock):
        async def _t():
            mqtt = _make_mqtt_client(topics=(TOPIC_A, TOPIC_B))
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()                  # both resubscribed
            _message(mqtt, TOPIC_A, 0)                   # only A comes back
            clock.t += RETRY + 1
            _message(mqtt, TOPIC_A, 12)                  # ...and keeps publishing
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt) == [TOPIC_A, TOPIC_B, TOPIC_B]
        _run(_t())


    def test_timers_and_states_are_independent(self, clock, caplog):
        # A is offline long enough to back off to the slow interval and B is
        # online but stuck long enough to warn. Neither affects the other's
        # interval, attempt count or warning.
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"}, topics=(TOPIC_A, TOPIC_B))
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            a, b = mqtt._wd_topics[TOPIC_A], mqtt._wd_topics[TOPIC_B]
            assert a.next_at - clock.t == RETRY and b.next_at - clock.t == RETRY
            while clock.t - a.offline_since < FAST_PERIOD:
                clock.t += 5
                await mqtt._watchdog_tick()
            assert a.next_at - clock.t <= SLOW and a.next_at - clock.t > RETRY
            assert a.attempts == 0 and not a.warned          # never resubscribed
            assert b.warned and b.attempts >= WARN_AFTER     # B gave up its fast pace
            assert TOPIC_A not in _subscribes(mqtt)
            mqtt._states[DEV_A] = "online"                   # A comes back
            clock.t = a.next_at
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt).count(TOPIC_A) == 1
            assert a.attempts == 1 and not a.warned          # B's warning is not A's
            _message(mqtt, TOPIC_A, 0)
            assert not a.silent and b.silent                 # A's resume leaves B alone
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and DEV_B in warnings[0]


# ---------------------------------------------------------------------------
# Online device: resubscribe and retry
# ---------------------------------------------------------------------------

class TestResubscribe:
    def test_online_device_is_resubscribed_after_threshold(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt) == [TOPIC_A]
            mqtt._process_reconnect.assert_not_awaited()
            mqtt.client.unsubscribe.assert_not_called()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run(_t())
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_retries_every_retry_interval_not_every_tick(self, clock):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            clock.t += RETRY - 5
            await mqtt._watchdog_tick()
            assert mqtt.subscribe.await_count == 1
            clock.t += 6
            await mqtt._watchdog_tick()
            assert mqtt.subscribe.await_count == 2
        _run(_t())

    def test_retry_slows_after_fast_period(self, clock):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            w = mqtt._wd_topics[TOPIC_A]
            assert w.next_at - clock.t == RETRY
            clock.t += FAST_PERIOD
            await mqtt._watchdog_tick()
            assert w.next_at - clock.t == SLOW
        _run(_t())

    def test_unknown_state_still_resubscribes(self, clock):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: RuntimeError("api down")})
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt) == [TOPIC_A]
        _run(_t())

    def test_raising_resubscribe_does_not_escape(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            mqtt.subscribe = AsyncMock(side_effect=OSError("socket gone"))
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            assert mqtt._wd_topics[TOPIC_A].attempts == 1
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _run(_t())
        assert any("raised" in r.getMessage() for r in caplog.records)

    def test_warning_only_after_repeated_resubscribes_fail(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            for _ in range(WARN_AFTER):
                await mqtt._watchdog_tick()
                clock.t += RETRY + 1
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
            await mqtt._watchdog_tick()                  # one more: still silent
            w = mqtt._wd_topics[TOPIC_A]
            assert w.warned and w.next_at - clock.t == pytest.approx(SLOW)
            for _ in range(3):
                clock.t += SLOW + 1
                await mqtt._watchdog_tick()
            assert mqtt.subscribe.await_count == WARN_AFTER + 4
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "did not restore" in warnings[0].getMessage()

    def test_never_uses_the_wall_clock(self, clock):
        # The fake clock's time() raises; a full silence/resume cycle must pass.
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            _message(mqtt, TOPIC_A, 0)
            assert not mqtt._wd_topics[TOPIC_A].silent
        _run(_t())


# ---------------------------------------------------------------------------
# Offline device: hold and back off
# ---------------------------------------------------------------------------

class TestOffline:
    def test_offline_device_is_not_resubscribed_and_logged_once(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"})
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            for _ in range(3):
                await mqtt._watchdog_tick()
                clock.t += RETRY + 1
            mqtt.subscribe.assert_not_awaited()
            assert mqtt.api.device.get_state.await_count == 3
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        assert sum("reports the device offline" in r.getMessage() for r in caplog.records) == 1
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_offline_rechecks_fast_then_slow(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"})
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            w = mqtt._wd_topics[TOPIC_A]
            assert w.next_at - clock.t == RETRY
            reads = 1
            while clock.t - w.offline_since < FAST_PERIOD:
                clock.t = w.next_at
                await mqtt._watchdog_tick()
                reads += 1
            assert w.next_at - clock.t == SLOW
            assert reads <= FAST_PERIOD / RETRY + 1
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        assert sum("still offline" in r.getMessage() for r in caplog.records) == 1

    def test_resubscribed_as_soon_as_device_is_back(self, clock):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"})
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            mqtt._states[DEV_A] = "online"
            clock.t += RETRY
            await mqtt._watchdog_tick()
            assert _subscribes(mqtt) == [TOPIC_A]
        _run(_t())

    def test_offline_period_resets_the_warning_count(self, clock, caplog):
        # Resubscribes sent while the cloud still showed the device online
        # (it lags ~90 s) must not count towards the warning once it is back.
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            for _ in range(WARN_AFTER - 1):
                await mqtt._watchdog_tick()
                clock.t += RETRY + 1
            mqtt._states[DEV_A] = "offline"
            await mqtt._watchdog_tick()
            clock.t += RETRY + 1
            mqtt._states[DEV_A] = "online"
            await mqtt._watchdog_tick()
            clock.t += RETRY + 1
            await mqtt._watchdog_tick()
            assert mqtt._wd_topics[TOPIC_A].attempts == 2
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# Resume reporting, privacy and lifecycle
# ---------------------------------------------------------------------------

class TestResume:
    def test_resume_after_resubscribe_logged_at_info_with_seq(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _message(mqtt, TOPIC_A, 41)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            mqtt._watchdog_note_suback(TOPIC_A)
            clock.t += 0.4
            _message(mqtt, TOPIC_A, 0)
            w = mqtt._wd_topics[TOPIC_A]
            assert not w.silent and w.attempts == 0
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        line = next(r for r in caplog.records if "stream resumed" in r.getMessage())
        assert line.levelno == logging.INFO
        assert DEV_A in line.getMessage()
        assert "0.40s after SUBACK" in line.getMessage()
        assert "seq 41 -> 0" in line.getMessage()

    def test_resume_without_resubscribe_reported_as_such(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client({DEV_A: "offline"})
            _message(mqtt, TOPIC_A, 5)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()                  # offline: no resubscribe
            _message(mqtt, TOPIC_A, 6)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        assert any("resumed on its own" in r.getMessage() and "seq 5 -> 6" in r.getMessage()
                   for r in caplog.records)

    def test_resume_after_warning_is_also_a_warning(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            for _ in range(WARN_AFTER + 1):
                await mqtt._watchdog_tick()
                clock.t += SLOW + 1
            _message(mqtt, TOPIC_A, 0)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            _run(_t())
        resumed = next(r for r in caplog.records if "stream resumed" in r.getMessage())
        assert resumed.levelno == logging.WARNING

    def test_next_silence_starts_fresh(self, clock):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
            _message(mqtt, TOPIC_A, 0)
            clock.t += THRESHOLD - 1
            await mqtt._watchdog_tick()                  # not yet silent again
            assert mqtt.subscribe.await_count == 1
            clock.t += 2
            await mqtt._watchdog_tick()
            assert mqtt.subscribe.await_count == 2
        _run(_t())

    def test_session_id_is_not_logged(self, clock, caplog):
        async def _t():
            mqtt = _make_mqtt_client()
            _start(mqtt, clock)
            clock.t += THRESHOLD + 1
            await mqtt._watchdog_tick()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run(_t())
        assert not any("secret-sid" in r.getMessage() for r in caplog.records)

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
