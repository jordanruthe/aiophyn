"""Run an example script to quickly test."""
import asyncio
import logging
from datetime import date

from aiohttp import ClientSession

from aiophyn import async_get_api
from aiophyn.errors import PhynError

_LOGGER = logging.getLogger()

USERNAME = "USERNAME_HERE"
PASSWORD = "PASSWORD_HERE"
BRAND = "phyn" # phyn or kohler

async def on_message(device_id, data):
    """Display a received MQTT message"""
    _LOGGER.info("Message for %s: %s", device_id, data)

async def main() -> None:
    """Create the aiohttp session and run the example."""
    logging.basicConfig(level=logging.INFO)
    try:
        api = await async_get_api(USERNAME, PASSWORD, phyn_brand=BRAND)

        all_home_info = await api.home.get_homes(USERNAME)
        _LOGGER.info(all_home_info)

        home_info = all_home_info[0]
        _LOGGER.info(home_info)

        first_device_id = home_info["device_ids"][0]
        device_state = await api.device.get_state(first_device_id)
        _LOGGER.info(device_state)

        await api.mqtt.add_event_handler("update", on_message)
        await api.mqtt.connect()

        for device in home_info['devices']:
            if device['product_code'] in ['PP1','PP2']:
                _LOGGER.info("Found Phyn Plus: %s", device)
                await api.mqtt.subscribe(f"prd/app_subscriptions/{device['device_id']}")

        await asyncio.sleep(10)

        await api.mqtt.disconnect_and_wait()

    except PhynError as err:
        _LOGGER.error("There was an error: %s", err)


asyncio.run(main())
