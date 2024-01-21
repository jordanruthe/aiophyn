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
