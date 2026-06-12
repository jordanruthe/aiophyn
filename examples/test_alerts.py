"""Run an example script to quickly test alert endpoints."""
import asyncio
import logging

from aiohttp import ClientSession

from aiophyn import async_get_api
from aiophyn.errors import PhynError

try:
    from config import USERNAME, PASSWORD, BRAND, PROXY, PROXY_PORT
except ImportError:
    raise SystemExit("Copy examples/config.example to examples/config.py and fill in your credentials.")

_LOGGER = logging.getLogger()


async def main() -> None:
    """Create the aiohttp session and run the example."""
    logging.basicConfig(level=logging.INFO)
    async with ClientSession() as session:
        try:
            api = await async_get_api(
                USERNAME, PASSWORD,
                phyn_brand=BRAND,
                session=session,
                proxy=PROXY,
                proxy_port=PROXY_PORT,
            )

            homes = await api.home.get_homes(USERNAME)
            home_id = homes[0]["id"]
            _LOGGER.info("home_id: %s", home_id)

            from aiophyn.alert import Alert
            alerts = await api.alert.get_latest(USERNAME, home_id, alert_type=Alert.ALERT_TYPES)
            _LOGGER.info("latest alerts (%d): %s", len(alerts), alerts)

            summary = await api.alert.get_active_summary(USERNAME)
            _LOGGER.info("active summary: %s", summary)

            # if alerts:
            #     first_id = alerts[0]["id"]
            #     _LOGGER.info("marking alert %s as read", first_id)
            #     result = await api.alert.mark_read(first_id)
            #     _LOGGER.info("mark_read result: %s", result)

        except PhynError as err:
            _LOGGER.error("There was an error: %s", err)


asyncio.run(main())
