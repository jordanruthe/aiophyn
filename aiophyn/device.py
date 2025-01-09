"""Define /devices endpoints."""
from typing import Awaitable, Any, Callable, Optional

from .const import API_BASE


class Device:
    """Define an object to handle the endpoints."""

    def __init__(self, request: Callable[..., Awaitable]) -> None:
        """Initialize."""
        self._request: Callable[..., Awaitable] = request

    async def get_state(self, device_id: str) -> dict:
        """Return state of a device.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        return await self._request("get", f"{API_BASE}/devices/{device_id}/state")

    async def get_consumption(
        self,
        device_id: str,
        duration: str,
        precision: int = 6,
        details: Optional[str] = False,
        event_count: Optional[str] = False,
        comparison: Optional[str] = False,
    ) -> dict:
        """Return water consumption of a device.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :param duration: Date string formatted as 'YYYY/MM/DD', 'YYYY/MM', or 'YYYY'
        :type duration: ``str``
        :param precision: Decimal places of measurement precision
        :type precision: ``int``
        :param details: Include detailed breakdown of consumption
        :type details: ``bool``
        :param event_count: Include the event count
        :type event_count: ``bool``
        :param comparison: Include comparison data
        :type comparison: ``bool``
        :rtype: ``dict``
        """

        params = {
            "device_id": device_id,
            "duration": duration,
            "precision": precision,
        }

        if details:
            params["details"] = "Y"

        if event_count:
            params["event_count"] = "Y"

        if comparison:
            params["comparison"] = "Y"

        return await self._request(
            "get", f"{API_BASE}/devices/{device_id}/consumption/details", params=params
        )

    async def get_water_statistics(self, device_id: str, from_ts, to_ts):
        """Get statistics about a PW1 sensor

        :param device_id: Unique identifier for the device
        :type device_id: str
        :param from_ts: Lower bound timestamp. This is a timestamp with thousands as integer
        :type from_ts: int
        :param to_ts: Upper bound timestamp. This is a timestamp with thousands as integer
        :type to_ts: int
        :return: List of dictionaries of results. 
        :rtype: List[dict[str, Any]]
        """
        params = {
            "from_ts": from_ts,
            "to_ts": to_ts
        }

        return await self._request(
            "get", f"{API_BASE}/devices/{device_id}/water_statistics/history/", params=params
        )

    async def open_valve(self, device_id: str) -> None:
        """Open a device shutoff valve.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        return await self._request(
            "post",
            f"{API_BASE}/devices/{device_id}/sov/Open",
        )

    async def close_valve(self, device_id: str) -> None:
        """Close a device shutoff valve.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        return await self._request(
            "post",
            f"{API_BASE}/devices/{device_id}/sov/Close",
        )

    async def get_away_mode(self, device_id: str) -> dict:
        """Return away mode status of a device.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        return await self._request("get", f"{API_BASE}/preferences/device/{device_id}/leak_sensitivity_away_mode")


    async def enable_away_mode(self, device_id: str) -> None:
        """Enable the device's away mode.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        data = [
            {
                "name": "leak_sensitivity_away_mode",
                "value": "true",
                "device_id": device_id,
            }
        ]
        return await self._request(
            "post", f"{API_BASE}/preferences/device/{device_id}", json=data
        )

    async def disable_away_mode(self, device_id: str) -> None:
        """Disable the device's away mode.

        :param device_id: Unique identifier for the device
        :type device_id: ``str``
        :rtype: ``dict``
        """
        data = [
            {
                "name": "leak_sensitivity_away_mode",
                "value": "false",
                "device_id": device_id,
            }
        ]
        return await self._request(
            "post", f"{API_BASE}/preferences/device/{device_id}", json=data
        )
    
    async def get_autoshuftoff_status(self, device_id: str) -> dict:
        """Get phyn device preferences.

        :param device_id: Unique identifier for the device
        :type device_id: str
        :return: List of dicts with the following keys: created_ts, device_id, name, updated_ts, value
        :rtype: dict
        """
        return await self._request(
            "get", f"{API_BASE}/devices/{device_id}/auto_shutoff"
        )
    

    async def get_device_preferences(self, device_id: str) -> dict:
        """Get phyn device preferences.

        :param device_id: Unique identifier for the device
        :type device_id: str
        :return: List of dicts with the following keys: created_ts, device_id, name, updated_ts, value
        :rtype: dict
        """
        return await self._request(
            "get", f"{API_BASE}/preferences/device/{device_id}"
        )
    
    async def get_health_tests(self, device_id: str) -> dict:
        """Get phyn device preferences.

        :param device_id: Unique identifier for the device
        :type device_id: str
        :return: List of dicts with the following keys
        :rtype: dict
        """
        return await self._request(
            "get", f"{API_BASE}/devices/{device_id}/health_tests?list_type=grouped"
        )
    
    async def get_latest_firmware_info(self, device_id: str) -> dict:
        """Get Latest Firmware Information

        :param device_id: Unique identifier for the device
        :type device_id: str
        :return: Returns dict with fw_img_name, fw_version, product_code
        :rtype: dict
        """
        return await self._request(
            "get", f"{API_BASE}/firmware/latestVersion/v2?device_id={device_id}"
        )

    async def run_leak_test(self, device_id: str, extended_test: bool = False):
        """Run a leak test

        :param device_id: Unique identifier for the device
        :type device_id: str
        :param extended_test: True if the test be extended, defaults to False
        :type extended_test: bool, optional
        """
        data = {
            "initiator": "App",
            "test_duration": "e" if extended_test is True else "s"
        }
        return await self._request(
            "post", f"{API_BASE}/devices/{device_id}/health_tests", json=data
        )

    async def set_autoshutoff_enabled(self, device_id: str, shutoff_on: bool, time: int | None = None) -> None:
        """Set autoshutoff enabled

        :param device_id: Unique identifier for the device
        :type device_id: str
        :param shutoff_on: Turn autoshutoff on (True) or off (False). If false, also turn off for amount of time
        :type shutoff_on: bool
        :param time: Time for shutoff in seconds if disabling (30, 3600, 21600, 86400), or blank for indefinite
        :type time: int | None
        :param data: List of dicts which have the keys: device_id, name, value
        :type data: List[dict]
        """
        url = f"{API_BASE}/devices/{device_id}/auto_shutoff/status/"
        if shutoff_on == True:
            url += "Enable"
        else:
            url += "Disable"
            if time != None:
                url += "/%s" % time
        return await self._request(
            "post", url
        )

    async def set_device_preferences(self, device_id: str, data: list[dict]) -> None:
        """Set device preferences

        :param device_id: Unique identifier for the device
        :type device_id: str
        :param data: List of dicts which have the keys: device_id, name, value
        :type data: List[dict]
        """
        return await self._request(
            "post", f"{API_BASE}/preferences/device/{device_id}", json=data
        )
