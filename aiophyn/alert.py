"""Define /alerts endpoints."""
from typing import Awaitable, Callable, List, Optional, Union

from .const import API_BASE


class Alert:
    """Define an object to handle the endpoints."""

    def __init__(self, request: Callable[..., Awaitable]) -> None:
        """Initialize."""
        self._request: Callable[..., Awaitable] = request

    #: All known alert types, suitable for passing as ``alert_type`` to
    #: :meth:`get_latest`.
    ALERT_TYPES: List[str] = [
        "battery",
        "freeze_warn",
        "high_pressure",
        "humidity",
        "leak",
        "offline_leak",
        "periodic_leak",
        "pinhole_leak",
        "temperature",
        "water_detected",
    ]

    async def get_latest(
        self,
        user_id: str,
        home_id: str,
        alert_type: Union[str, List[str]],
        limit: int = 100,
    ) -> list:
        """Return the latest alerts for a home.

        :param user_id: Phyn account email address
        :type user_id: ``str``
        :param home_id: Unique identifier for the home
        :type home_id: ``str``
        :param alert_type: One or more alert types to include. Pass a single
            string or a list of strings. Known types: ``battery``,
            ``freeze_warn``, ``high_pressure``, ``humidity``, ``leak``,
            ``offline_leak``, ``periodic_leak``, ``pinhole_leak``,
            ``temperature``, ``water_detected``. Use
            :attr:`ALERT_TYPES` to request all types at once.
        :type alert_type: ``str`` or ``list``
        :param limit: Maximum number of alerts to return, defaults to 100
        :type limit: ``int``
        :rtype: ``list``
        """
        if isinstance(alert_type, list):
            alert_type = ",".join(alert_type)
        params = {
            "user_id": user_id,
            "home_id": home_id,
            "type": alert_type,
            "limit": limit,
        }
        return await self._request("get", f"{API_BASE}/alerts/latest", params=params)

    async def get_active_summary(
        self,
        user_id: str,
        filter_type: str = "unresolved_unread",
    ) -> dict:
        """Return a summary of active alerts grouped by device.

        :param user_id: Phyn account email address
        :type user_id: ``str``
        :param filter_type: Filter to apply, defaults to ``unresolved_unread``
        :type filter_type: ``str``
        :rtype: ``dict``
        """
        params = {
            "user_id": user_id,
            "filter_type": filter_type,
        }
        return await self._request(
            "get", f"{API_BASE}/alerts/summary/active", params=params
        )

    async def mark_read(self, alert_id: str) -> dict:
        """Mark an alert as read.

        :param alert_id: Unique identifier for the alert
        :type alert_id: ``str``
        :rtype: ``dict``
        """
        return await self._request(
            "post", f"{API_BASE}/alerts/{alert_id}/status/read"
        )
