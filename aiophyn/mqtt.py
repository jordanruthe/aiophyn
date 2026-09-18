""" Module providing a MQTT provider """

import asyncio
import logging

from typing import Any, Dict, Union, Optional

import inspect
import json
import time
import urllib
import ssl
import socket
import re
import socks
import paho.mqtt.client as paho_mqtt

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)

# How long to wait for the _on_disconnect callback before giving up.
# A missing callback (e.g. when MQTT is already down at unload time) would
# otherwise block disconnect_and_wait forever — the root cause of the reload
# hang described in issues #56 / #60.
_DISCONNECT_WAIT_TIMEOUT: float = 10.0

# How long to wait for a CONNACK after paho.connect() returns before treating
# the attempt as failed. Unchanged from the previous hard-coded value; named
# here only to distinguish it from the retry backstop below.
_CONNACK_WATCHDOG_INTERVAL: float = 5.0

# How long to wait before revisiting a connection we believe is down. This is
# the only recovery mechanism left if a reconnect loop exits without a live
# connection, so it must stay short -- but it is a backstop for a client that
# is already known to be down, not a handshake timeout, so it does not need to
# be as tight as _CONNACK_WATCHDOG_INTERVAL.
_RECONNECT_WATCHDOG_INTERVAL: float = 60.0

class AIOHelper:
    """Helper class for Asynchronous IO

    paho-mqtt invokes the socket callbacks from whichever thread drives the
    client. ``MQTTClient.connect()`` runs ``paho.connect()`` in an executor,
    so ``on_socket_open`` (and the write-register callbacks issued while
    sending CONNECT) arrive on a worker thread. Event-loop methods such as
    ``add_reader`` and ``create_task`` are not thread-safe, so every callback
    is marshalled onto the loop thread with ``call_soon_threadsafe`` when
    needed (issue #67).
    """
    def __init__(self, client: paho_mqtt.Client) -> None:
        self.loop = asyncio.get_running_loop()
        self.client = client
        self.client.on_socket_open = self._on_socket_open
        self.client.on_socket_close = self._on_socket_close
        self.client._on_socket_register_write = self._on_socket_register_write
        self.client._on_socket_unregister_write = \
            self._on_socket_unregister_write
        self.misc_task: Optional[asyncio.Task] = None
        self.sock: Optional[socket.socket] = None

    def _run_on_loop(self, func, *args) -> None:
        """Run ``func`` on the event-loop thread, directly if already there."""
        try:
            on_loop_thread = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_loop_thread = False
        if on_loop_thread:
            func(*args)
        else:
            self.loop.call_soon_threadsafe(func, *args)

    def _on_socket_open(self,
                        client: paho_mqtt.Client,
                        userdata: Any,
                        sock: socket.socket
                        ) -> None:
        # pylint: disable=unused-argument
        _LOGGER.info("MQTT Socket Opened")
        self._run_on_loop(self._socket_open_on_loop, client, sock)

    def _socket_open_on_loop(self, client: paho_mqtt.Client, sock: socket.socket) -> None:
        self.loop.add_reader(sock, client.loop_read)
        # Remember which socket this misc loop belongs to, so that a later
        # close callback for a *different* socket cannot cancel it.
        self.sock = sock
        if self.misc_task is not None and not self.misc_task.done():
            self.misc_task.cancel()
        self.misc_task = self.loop.create_task(self.misc_loop())

    def _on_socket_close(self, client: paho_mqtt.Client, userdata: Any, sock: socket.socket) -> None:
        # pylint: disable=unused-argument
        _LOGGER.info("MQTT Socket Closed")
        self._run_on_loop(self._socket_close_on_loop, sock)

    def _socket_close_on_loop(self, sock: socket.socket) -> None:
        self.loop.remove_reader(sock)
        if sock is not self.sock:
            # remove_reader() is scoped to the socket it was handed;
            # misc_task.cancel() was not. When two sockets overlap (open A,
            # open B, then stale A closes), A's close cancelled B's misc loop.
            # loop_misc() is the only caller of _check_keepalive() and the only
            # thing sending PINGREQ, so the client would then keep reporting
            # is_connected() with no way left to notice a dead link.
            #
            # Found by inspection while tracing the reconnect path, and
            # reproduced against AIOHelper directly; I have no field capture of
            # it, so this is offered as a latent defect rather than as the
            # explanation for any particular report.
            _LOGGER.debug("Ignoring close of a superseded MQTT socket")
            return
        self.sock = None
        if self.misc_task is not None:
            self.misc_task.cancel()

    def _on_socket_register_write(self,
                                  client: paho_mqtt.Client,
                                  userdata: Any,
                                  sock: socket.socket
                                  ) -> None:
        # pylint: disable=unused-argument
        self._run_on_loop(self.loop.add_writer, sock, client.loop_write)

    def _on_socket_unregister_write(self,
                                    client: paho_mqtt.Client,
                                    userdata: Any,
                                    sock: socket.socket
                                    ) -> None:
        # pylint: disable=unused-argument
        self._run_on_loop(self.loop.remove_writer, sock)

    async def misc_loop(self) -> None:
        """Loop for MQTT"""
        while self.client.loop_misc() == paho_mqtt.MQTT_ERR_SUCCESS:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
        _LOGGER.info("MQTT Misc Loop Complete")

class Timer:
    """ Class to run a job with a timeout """
    def __init__(self, callback):
        _LOGGER.info("Creating timer")
        self._timeout = 0
        self._callback = callback
        self._task = None

    async def _job(self, timeout):
        """ Run the job with a timeout """
        await asyncio.sleep(timeout)
        _LOGGER.debug("Executing timer callback")
        if inspect.iscoroutinefunction(self._callback):
            await self._callback()
        else:
            self._callback()

    def _running_task(self) -> Optional[asyncio.Task]:
        """The task currently executing, if we are on the event loop."""
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def cancel(self):
        """ Cancel a timer task """
        if self._task is not None:
            # Never cancel the task we are running inside: callbacks are
            # invoked from _job, so self._task is the caller when cancel() or
            # start() is reached from within the callback itself. Cancelling
            # it there aborts the callback at its next await.
            if self._task is not self._running_task():
                self._task.cancel()
            self._task = None

    def start(self, timeout):
        """ Start a timer task """
        if self._task is not None and self._task is not self._running_task():
            self._task.cancel()
        _LOGGER.debug("Starting timer job for %s seconds", timeout)
        self._task = asyncio.create_task(self._job(timeout))

class MQTTClient:
    """AIO MQTT client """
    def __init__(self, api, client_id: str =None, verify_ssl: bool =True, proxy: str =None, proxy_port: int =None):
        self.event_loop = asyncio.get_running_loop()
        self.api = api
        self.pending_acks = {}
        self.topics = []
        self.connect_evt: asyncio.Event = asyncio.Event()
        self.connect_task = None
        self.disconnect_evt: Optional[asyncio.Event] = None
        self.reconnect_evt: asyncio.Event = asyncio.Event()
        self.host = None 
        self.port = 443

        if client_id is None:
            client_id = "aiophyn-%s" % int(time.time())

        self.client = paho_mqtt.Client(client_id=client_id, transport="websockets")
        self.helper: AIOHelper = None
        self.reconnect_timer = Timer(self._process_reconnect)

        self.verify_ssl: bool = verify_ssl
        self.proxy: Optional[str] = proxy
        self.proxy_port: Optional[int] = proxy_port

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

        self._handlers = {
            "connect": [],
            "disconnect": [],
            "update": []
        }

    async def add_event_handler(self, type, target):
        """Add an event handler for MQTT events"""
        if type not in self._handlers.keys():
            return False

        if target in self._handlers[type]:
            return True
        self._handlers[type].append(target)

    async def connect(self):
        """ Create a conenction to the MQTT server """
        self.disconnect_evt = None
        self.host, path = await self.get_mqtt_info()
        self.client.ws_set_options(path, headers={'Host': self.host})

        if self.verify_ssl:
            context = ssl.SSLContext()
            self.client.tls_set_context(context)
        else:
            context = ssl.SSLContext()
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
            self.client.tls_set_context(context)
            self.client.tls_insecure_set(True)

        if self.proxy is not None and self.proxy_port is not None:
            self.client.proxy_set(proxy_type=socks.HTTP, proxy_addr=self.proxy, proxy_port=self.proxy_port)

        self.helper = AIOHelper(self.client)
        _LOGGER.info("Connecting to mqtt websocket: %s", self.host)
        self.connect_evt.clear()
        await self.event_loop.run_in_executor(
                None,
                self.client.connect,
                self.host,
                self.port,
            )
        # Arm the CONNACK watchdog only once paho.connect() has returned.
        # Arming it beforehand allowed _process_reconnect to fire while this
        # connect was still in flight and start a *second* connection with the
        # same client id; brokers that enforce unique client ids (AWS IoT)
        # resolve that by dropping one of the two.
        #
        # If the CONNACK was already processed while we were awaiting the
        # executor, _on_connect has run and armed the long keepalive interval.
        # Arming unconditionally here would clobber that with the short
        # watchdog and force a needless reconnect a minute later.
        if not self.client.is_connected():
            self.reconnect_timer.start(_CONNACK_WATCHDOG_INTERVAL)
    
    def disconnect(self):
        """Disconnect from server.

        This is an intentional disconnect: ``disconnect_evt`` stays set
        afterwards so that a late ``_on_disconnect`` callback does not spawn
        a reconnect loop on a client the caller has discarded.  ``connect()``
        clears it again.
        """
        self.disconnect_evt = asyncio.Event()
        _LOGGER.info("MQTT client disconnecting...")

        # Stop the reconnect machinery: the hourly keepalive timer would
        # otherwise resurrect this client via _process_reconnect, and an
        # in-flight reconnect loop (spawned by _on_disconnect or
        # _process_reconnect) should not keep retrying past an explicit
        # disconnect request.
        self.reconnect_timer.cancel()
        if self.connect_task is not None and not self.connect_task.done():
            self.connect_task.cancel()

        if not self.client.is_connected():
            # paho-mqtt only invokes on_disconnect for a socket it is
            # actually tearing down. If we were never connected, or already
            # dropped (e.g. mid-reconnect-loop after a real disconnect),
            # there is nothing to wait for and on_disconnect will never
            # fire — set the event ourselves so callers don't hang.
            self.disconnect_evt.set()
            return

        self.client.disconnect()

    async def disconnect_and_wait(self, timeout: Optional[float] = None) -> None:
        """Disconnect from the server and wait for the callback to confirm.

        Returns promptly when the client is already disconnected (paho never
        fires ``on_disconnect`` in that case).  Bounded by ``timeout``
        (default ``_DISCONNECT_WAIT_TIMEOUT``) so a caller that omits its own
        timeout can't be left hanging on a disconnect that paho-mqtt never
        acks, e.g. a dead peer with no FIN observed yet.
        """
        if timeout is None:
            timeout = _DISCONNECT_WAIT_TIMEOUT
        self.disconnect()
        try:
            await asyncio.wait_for(self.disconnect_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timed out after %ss waiting for MQTT disconnect callback; "
                "proceeding as disconnected",
                timeout,
            )

    async def get_mqtt_info(self):
        """ Gets WebSocket URL and parameters for a MQTT connection
            Returns a list of url and path
        """
        user_id = urllib.parse.quote_plus(self.api.username)
        try:
            wss_data = await self.api._request("post", f"{API_BASE}/users/{user_id}/iot_policy", token_type="id")
        except Exception as err:
            raise Exception("Could not get WebSocket/MQTT url from API") from err

        match = re.match(r'wss:\/\/([a-zA-Z0-9\.\-]+)(\/mqtt?.*)', wss_data['wss_url'])
        if not match:
            raise Exception("Could not find WebSocket/MQTT url")

        return match.group(1), match.group(2)


    async def subscribe(self, topic):
        """Subscribe to a MQTT topic"""
        _LOGGER.info("Attempting to subscribe to: %s", topic)
        # Track subscription intent (not SUBACK) so the reconnect loop
        # re-subscribes even if the ack is lost or the connection drops
        # before it arrives.
        if topic not in self.topics:
            self.topics.append(topic)
        res, msg_id = self.client.subscribe(topic, 0)
        if res != paho_mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(
                "Subscribe to %s failed (%s); will retry on reconnect",
                topic,
                paho_mqtt.error_string(res),
            )
            return
        self.pending_acks[msg_id] = topic

    def _on_connect(self,
                    client: paho_mqtt.Client,
                    user_data: Any,
                    flags: Dict[str, Any],
                    reason_code: Union[int, paho_mqtt.ReasonCodes],
                    properties: Optional[paho_mqtt.Properties] = None
                    ) -> None:
        # pylint: disable=unused-argument
        _LOGGER.info("MQTT Client Connected")
        if reason_code == 0:
            _LOGGER.info("Trying to run timer...")
            self.reconnect_timer.cancel()
            self.reconnect_timer.start(3600)
            self.connect_evt.set()
        else:
            if isinstance(reason_code, int):
                err_str = paho_mqtt.connack_string(reason_code)
            else:
                err_str = reason_code.getName()
            _LOGGER.info("MQTT Connection Failed: %s", err_str)

    def _on_disconnect(self,
                       client: paho_mqtt.Client,
                       user_data: Any,
                       reason_code: int,
                       properties: Optional[paho_mqtt.Properties] = None
                       ) -> None:
        # pylint: disable=unused-argument
        self.connect_evt.clear()

        if self.disconnect_evt is not None:
            self.disconnect_evt.set()
            _LOGGER.info("Client disconnected, not attempting to reconnect")
            return

        # The server connection was dropped, attempt to reconnect.
        #
        # This must NOT be gated on self.is_connected(). paho only moves its
        # internal state out of MQTT_CS_CONNECTED *after* this callback returns
        # (Client._loop_rc_handle sets MQTT_CS_CONNECTION_LOST below the
        # _do_on_disconnect call; Client._check_keepalive does not set it at
        # all), so client.is_connected() is still True here for every
        # unexpected drop. The previous "elif not self.is_connected()" guard
        # therefore never matched and no reconnect was ever scheduled: the
        # client stayed disconnected with connect_task=None and
        # reconnect_evt=False until something reloaded it.
        _LOGGER.info("MQTT Server Disconnected, reason: %s", paho_mqtt.error_string(reason_code))
        # Re-arm rather than cancel: if the reconnect loop below exits without
        # a live connection, this timer is the only thing left that can retry.
        self.reconnect_timer.start(_RECONNECT_WATCHDOG_INTERVAL)
        if self.connect_task is None or self.connect_task.done():
            self.connect_task = asyncio.create_task(self._do_reconnect(True))

    def is_connected(self) -> bool:
        """ Checks if the client is connected """
        return self.client.is_connected()

    async def _process_reconnect(self):
        _LOGGER.info("Processing reconnect request")

        # If a reconnect loop is already running (e.g. recovering from an
        # unexpected drop), don't interfere — it will self-heal and re-subscribe.
        # Restart the keepalive timer so we revisit in another hour.
        if self.reconnect_evt.is_set():
            _LOGGER.info("Reconnect already in progress, skipping keepalive cycle")
            self.reconnect_timer.start(3600)
            return

        # If disconnected with no loop running, spawn a reconnect now.
        if not self.is_connected():
            # Re-arm first: the reconnect loop may exit without a live
            # connection, and then this timer is the only way back.
            self.reconnect_timer.start(_RECONNECT_WATCHDOG_INTERVAL)
            if self.connect_task is None or self.connect_task.done():
                _LOGGER.info("MQTT disconnected at keepalive; spawning reconnect")
                self.connect_task = asyncio.create_task(self._do_reconnect(True))
            return

        # Connection is live and idle. Force a fresh connection to
        # re-fetch the wss URL (which refreshes credentials) and re-subscribe.
        self.disconnect_evt = asyncio.Event()
        self.client.disconnect()
        try:
            # Bounded, using the same constant disconnect_and_wait() uses. An
            # unbounded wait here is the one remaining route to a permanently
            # dead client: if the callback never arrives, disconnect_evt stays
            # set and every later _on_disconnect takes the "not attempting to
            # reconnect" branch forever.
            await asyncio.wait_for(self.disconnect_evt.wait(),
                                   timeout=_DISCONNECT_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timed out after %ss waiting for the MQTT disconnect callback "
                "during a keepalive refresh; reconnecting anyway",
                _DISCONNECT_WAIT_TIMEOUT,
            )
        finally:
            # Clear the intentional-disconnect marker before starting the
            # reconnect loop, so that any unexpected drop during reconnection
            # is treated as unintentional (i.e. triggers another reconnect).
            # In a finally: leaving this set would disable reconnects for good.
            self.disconnect_evt = None

        if self.connect_task is None or self.connect_task.done():
            self.connect_task = asyncio.create_task(self._do_reconnect(True))

    async def _do_reconnect(self, first: bool = False) -> None:
        if self.reconnect_evt.is_set():
            _LOGGER.info("Already attempting to reconnect, second attemp cancelled.")
            return

        _LOGGER.info("Attempting MQTT Connect/Reconnect")
        self.reconnect_evt.set()
        last_err: Exception = Exception()
        connect_attempts = 0
        t: float = 2.
        try:
            while True:
                if not first:
                    try:
                        if connect_attempts > 6:
                            t = 60.
                        elif connect_attempts > 3:
                            t = 10.
                        _LOGGER.debug("MQTT throttle for %s seconds", t)
                        await asyncio.sleep(t)
                    except asyncio.CancelledError:
                        raise
                first = False
                connect_attempts += 1
                try:
                    self.host, path = await self.get_mqtt_info()
                    self.client.ws_set_options(path, headers={'Host': self.host})
                    _LOGGER.info("Attempting to reconnnect...")
                    self.connect_evt.clear()
                    await self.event_loop.run_in_executor(
                            None,
                            self.client.connect,
                            self.host,
                            self.port,
                        )

                    await asyncio.wait_for(self.connect_evt.wait(), timeout=2.)
                    # connect_evt is only ever set by _on_connect. Without the
                    # clear() above, a set left over from an earlier attempt
                    # satisfied this wait immediately and the loop broke out
                    # believing it had connected. Confirm against paho itself.
                    if not self.client.is_connected():
                        _LOGGER.info("MQTT connection not established, retrying")
                        continue

                    # Re-subscribe to all topics; drop acks from the old session.
                    self.pending_acks.clear()
                    topics = list(set(self.topics))
                    tasks = [self.subscribe(topic) for topic in topics]
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if type(last_err) is not type(e) or last_err.args != e.args:
                        _LOGGER.warning("MQTT Connection Error")
                        last_err = e
                    continue
                break
        finally:
            # Always release the reconnect lock and clean up task/event state so
            # that subsequent disconnect events can trigger a new reconnect attempt.
            self.reconnect_evt.clear()
            self.connect_task = None
            # Never leave the client disconnected with nothing scheduled:
            # reconnect_evt clear + connect_task None + no timer means nothing
            # will ever retry.
            if not self.client.is_connected():
                self.reconnect_timer.start(_RECONNECT_WATCHDOG_INTERVAL)

    def _on_message(
        self, client: paho_mqtt.Client, userdata: Any, message: paho_mqtt.MQTTMessage
    ) -> None:
        # pylint: disable=unused-argument
        msg = message.payload.decode()
        _LOGGER.debug("Message received on %s: %s", message.topic, msg)
        try:
            data = json.loads(msg)
        except json.decoder.JSONDecodeError:
            _LOGGER.info("Received invalid JSON message: %s", msg)

        if message.topic.startswith("prd/app_subscriptions/"):
            device_id = message.topic.split('/')[2]
        else:
            device_id = None

        for h in self._handlers["update"]:
            asyncio.ensure_future(h(device_id, data))

    def _on_subscribe(
        self,
        client: paho_mqtt.Client,
        userdata: Any,
        mid: int,
        granted_qos: tuple[int] | list[paho_mqtt.ReasonCodes],
        properties: paho_mqtt.Properties | None = None,
    ) -> None:
        # pylint: disable=unused-argument
        if mid in self.pending_acks:
            _LOGGER.info("Subscribed to: %s", self.pending_acks[mid])
            del self.pending_acks[mid]
        else:
            _LOGGER.info("Subscribed: %s %s %s", userdata, str(mid), str(granted_qos))
