import asyncio
import logging
import inspect
import json
import ssl
import paho.mqtt.client as paho_mqtt
import socket
from datetime import datetime, timezone
import urllib
import re

from typing import Any, Dict, Union, Optional

from .const import API_BASE

_LOGGER = logging.getLogger(__name__)

class AIOHelper:
    def __init__(self, client: paho_mqtt.Client) -> None:
        self.loop = asyncio.get_running_loop()
        self.client = client
        self.client.on_socket_open = self._on_socket_open
        self.client.on_socket_close = self._on_socket_close
        self.client._on_socket_register_write = self._on_socket_register_write
        self.client._on_socket_unregister_write = \
            self._on_socket_unregister_write
        self.misc_task: Optional[asyncio.Task] = None

    def _on_socket_open(self,
                        client: paho_mqtt.Client,
                        userdata: Any,
                        sock: socket.socket
                        ) -> None:
        _LOGGER.info("MQTT Socket Opened")
        self.loop.add_reader(sock, client.loop_read)
        self.misc_task = self.loop.create_task(self.misc_loop())

    def _on_socket_close(self, client: paho_mqtt.Client, userdata: Any, sock: socket.socket) -> None:
        _LOGGER.info("MQTT Socket Closed")
        self.loop.remove_reader(sock)
        if self.misc_task is not None:
            self.misc_task.cancel()

    def _on_socket_register_write(self,
                                  client: paho_mqtt.Client,
                                  userdata: Any,
                                  sock: socket.socket
                                  ) -> None:
        self.loop.add_writer(sock, client.loop_write)

    def _on_socket_unregister_write(self,
                                    client: paho_mqtt.Client,
                                    userdata: Any,
                                    sock: socket.socket
                                    ) -> None:
        self.loop.remove_writer(sock)

    async def misc_loop(self) -> None:
        while self.client.loop_misc() == paho_mqtt.MQTT_ERR_SUCCESS:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
        _LOGGER.info("MQTT Misc Loop Complete")

class Timer:
    def __init__(self, callback):
        _LOGGER.info("Creating timer")
        self._timeout = 0
        self._callback = callback
        self._task = None

    async def _job(self, timeout):
        await asyncio.sleep(timeout)
        _LOGGER.debug("Executing timer callback")
        if inspect.iscoroutinefunction(self._callback):
            await self._callback()
        else:
            self._callback()

    def cancel(self):
        if self._task is not None:
            self._task.cancel()

    def start(self, timeout):
        _LOGGER.debug("Starting timer job for %s seconds" % timeout)
        asyncio.create_task(self._job(timeout))

class MQTTClient:
    def __init__(self, api):
        self.event_loop = asyncio.get_running_loop()
        self.api = api
        self.pending_acks = {}
        self.topics = []
        self.connect_evt: asyncio.Event = asyncio.Event()
        self.connect_task = None
        self.disconnect_evt: Optional[asyncio.Event] = None
        self.host = None
        self.port = 443

        self.client = paho_mqtt.Client(client_id="homeassistant", transport="websockets")
        self.reconnect_timer = Timer(self._process_reconnect)

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
        if type not in self._handlers.keys():
            return False

        if target in self._handlers[type]:
            return True
        self._handlers[type].append(target)

    async def connect(self):
        self.host, path = await self.get_mqtt_info()
        self.client.ws_set_options(path, headers={'Host': self.host})
        self.client.tls_set()

        self.helper = AIOHelper(self.client)
        _LOGGER.info("Connecting to mqtt websocket: %s" % self.host)
        await self.event_loop.run_in_executor(
                None,
                self.client.connect,
                self.host,
                self.port,
            )

    async def get_mqtt_info(self):
        user_id = urllib.parse.quote_plus(self.api._username)
        headers = {
            "user_id": user_id
        }
        try:
            wss_data = await self.api._request("post", f"{API_BASE}/users/{user_id}/iot_policy", token_type="id")
        except:
            Exception("Could not get WebSocket/MQTT url from API")

        match = re.match(r'wss:\/\/([a-zA-Z0-9\.\-]+)(\/mqtt?.*)', wss_data['wss_url'])
        if not match:
            raise Exception("Could not find WebSocket/MQTT url")

        return match.group(1), match.group(2)


    async def subscribe(self, topic):
        _LOGGER.info("Attempting to subscribe to: %s" % topic)
        res, msg_id = self.client.subscribe(topic, 0)
        self.pending_acks[msg_id] = topic


    def _on_connect(self,
                    client: paho_mqtt.Client,
                    user_data: Any,
                    flags: Dict[str, Any],
                    reason_code: Union[int, paho_mqtt.ReasonCodes],
                    properties: Optional[paho_mqtt.Properties] = None
                    ) -> None:
        _LOGGER.info("MQTT Client Connected")
        if reason_code == 0:
            _LOGGER.info("Trying to run timer...")
            self.reconnect_timer.start(3600)
            self.connect_evt.set()
        else:
            if isinstance(reason_code, int):
                err_str = paho_mqtt.connack_string(reason_code)
            else:
                err_str = reason_code.getName()
            _LOGGER.info(f"MQTT Connection Failed: {err_str}")

    def _on_disconnect(self,
                       client: paho_mqtt.Client,
                       user_data: Any,
                       reason_code: int,
                       properties: Optional[paho_mqtt.Properties] = None
                       ) -> None:
        _LOGGER.info("MQTT Server Disconnected, reason: "
                     f"{paho_mqtt.error_string(reason_code)}")
        _LOGGER.info("Disconnect: %s %s" % (self.connect_evt, self.disconnect_evt))
        if self.disconnect_evt is not None:
            self.disconnect_evt.set()
        elif self.is_connected():
            # The server connection was dropped, attempt to reconnect
            _LOGGER.info("MQTT Server Disconnected, reason: "
                         f"{paho_mqtt.error_string(reason_code)}")
            if self.connect_task is None:
                self.connect_task = asyncio.create_task(self._do_reconnect(True))
        self.connect_evt.clear()

    def is_connected(self) -> bool:
        return self.connect_evt.is_set()

    async def _process_reconnect(self):
        self.host, path = await self.get_mqtt_info()
        self.client.ws_set_options(path, headers={'Host': self.host})
        self.client.disconnect()

    async def _do_reconnect(self, first: bool = False) -> None:
        _LOGGER.info("Attempting MQTT Connect/Reconnect")
        last_err: Exception = Exception()
        while True:
            if not first:
                try:
                    await asyncio.sleep(2.)
                except asyncio.CancelledError:
                    raise
            first = False
            try:
                await self.event_loop.run_in_executor(
                        None,
                        self.client.reconnect,
                    )
                await self.connect_evt.wait()

                # Re-subscribe to all topics
                topics = list(set(self.topics))
                tasks = [self.subscribe(topic) for topic in topics]
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if type(last_err) is not type(e) or last_err.args != e.args:
                    _LOGGER.exception("MQTT Connection Error")
                    last_err = e
                continue
            break
        self.connect_task = None

    def _on_message(
        self, client: paho_mqtt.Client, userdata: Any, message: paho_mqtt.MQTTMessage
    ) -> None:
        msg = message.payload.decode()
        _LOGGER.debug("Message received on %s" % message.topic)
        try:
            data = json.loads(msg)
        except json.decoder.JSONDecodeError:
            _LOGGER.info("Received invalid JSON message: %s" % msg)

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
        if mid in self.pending_acks:
            _LOGGER.info("Subscribed to: %s" % self.pending_acks[mid])
            self.topics.append(self.pending_acks[mid])
            del self.pending_acks[mid]
        else:
            _LOGGER.info(("Subscribed: %s" % userdata) +str(mid)+" "+str(granted_qos))
