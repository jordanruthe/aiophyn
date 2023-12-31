"""Define a base client for interacting with Phyn."""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import boto3
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError
from pycognito.aws_srp import AWSSRP

from .partners import KOHLER_API
from .mqtt import MQTTClient
from .device import Device
from .errors import BrandError, RequestError
from .home import Home

from .const import API_BASE


_LOGGER = logging.getLogger(__name__)

BRANDS = {
    'phyn': 0,
    'kohler': 1,
}

DEFAULT_HEADER_CONTENT_TYPE: str = "application/json"
DEFAULT_HEADER_USER_AGENT: str = "phyn/18 CFNetwork/1331.0.7 Darwin/21.4.0"
DEFAULT_HEADER_CONNECTION: str = "keep-alive"
DEFAULT_HEADER_API_KEY: list[str] = [
    "E7nfOgW6VI64fYpifiZSr6Me5w1Upe155zbu4lq8",
    "OOLFiYu7Ts5RKI4BV6WeI3zb38HU76vZ8x5lFX6Y",
]
DEFAULT_HEADER_ACCEPT: str = "application/json"
DEFAULT_HEADER_ACCEPT_ENCODING: str = "gzip, deflate, br"

COGNITO_REGION: str = "us-east-1"
COGNITO_POOL_ID: str = "us-east-1_UAv6IUsyh"
COGNITO_CLIENT_ID: str = "5q2m8ti0urmepg4lup8q0ptldq"

DEFAULT_TIMEOUT: int = 10


class API:
    """Define the API object."""

    def __init__(
        self, username: str, password: str, *, phyn_brand: str, session: Optional[ClientSession] = None
    ) -> None:
        """Initialize."""
        if phyn_brand not in BRANDS:
            raise BrandError("Invalid phyn brand")

        self._brand: str = BRANDS[phyn_brand]

        self._username: str = username
        if self._brand != BRANDS['phyn']:
            self._password: str = None
            self._partner_api = None
            self._partner_password: str = password
            self._cognito: dict[str] = None
        else:
            self._password: str = password
            self._cognito: dict[str] = {
                "app_client_id": COGNITO_CLIENT_ID,
                "pool_id": COGNITO_POOL_ID,
                "region": COGNITO_REGION,
            }

        self._session: ClientSession = session
        self._iot_id = None
        self._iot_credentials = None
        self.mqtt = None

        self._token: Optional[str] = None
        self._token_expiration: Optional[datetime] = None

        self.home: Home = Home(self._request)
        self.device: Device = Device(self._request)
        self.mqtt = MQTTClient(self)

    async def _request(self, method: str, url: str, token_type:str = "access", **kwargs) -> dict:
        """Make a request against the API."""
        if self._token_expiration and datetime.now() >= self._token_expiration:
            _LOGGER.info("Requesting new access token to replace expired one")

            # Nullify the token so that the authentication request doesn't use it:
            self._token = None

            # Nullify the expiration so the authentication request doesn't get caught
            # here:
            self._token_expiration = None

            await self.async_authenticate()

        kwargs.setdefault("headers", {})
        kwargs["headers"].update(
            {
                "Content-Type": DEFAULT_HEADER_CONTENT_TYPE,
                "User-Agent": DEFAULT_HEADER_USER_AGENT,
                "Connection": DEFAULT_HEADER_CONNECTION,
                "x-api-key": DEFAULT_HEADER_API_KEY[self._brand],
                "Accept": DEFAULT_HEADER_ACCEPT,
                "Accept-Encoding": DEFAULT_HEADER_ACCEPT_ENCODING,
            }
        )

        if token_type == "access":
            if self._token:
                kwargs["headers"]["Authorization"] = self._token
        elif token_type == "id":
            if self._id_token:
                kwargs["headers"]["Authorization"] = self._id_token

        use_running_session = self._session and not self._session.closed

        if use_running_session:
            session = self._session
        else:
            session = ClientSession(timeout=ClientTimeout(total=DEFAULT_TIMEOUT))

        try:
            async with session.request(method, url, **kwargs) as resp:
                data: dict = await resp.json(content_type=None)
                resp.raise_for_status()
                return data
        except ClientError as err:
            raise RequestError(f"There was an error while requesting {url}") from err
        finally:
            if not use_running_session:
                await session.close()

    async def async_authenticate(self) -> None:
        """Authenticate the user and set the access token with its expiration."""
        if self._brand == BRANDS["kohler"]:
            if self._password == None:
                _LOGGER.info("Auhenticating to Kohler")
                self._partner_api = KOHLER_API(self._username, self._partner_password)
                await self._partner_api.authenticate()
                self._password = self._partner_api.get_phyn_password()
                self._cognito = self._partner_api.get_cognito_info()
                self._mqtt_settings = self._partner_api.get_mqtt_info()

        executor = ThreadPoolExecutor()
        future = executor.submit(self._authenticate)
        auth_response = await asyncio.wrap_future(future)

        access_token = auth_response["AuthenticationResult"]["AccessToken"]
        expires_in = auth_response["AuthenticationResult"]["ExpiresIn"]
        id_token = auth_response["AuthenticationResult"]["IdToken"]
        refresh_token = auth_response["AuthenticationResult"]["RefreshToken"]

        self._token = access_token
        self._token_expiration = datetime.now() + timedelta(seconds=expires_in)
        self._id_token = id_token
        self._refresh_token = refresh_token

    def _authenticate(self):
        """boto3 is synchronous, so authenticate in a separate thread."""
        _LOGGER.info("Requesting token from AWS")
        client = boto3.client("cognito-idp", region_name=self._cognito['region'])
        aws = AWSSRP(
            username=self._username,
            password=self._password,
            pool_id=self._cognito['pool_id'],
            client_id=self._cognito['app_client_id'],
            client=client,
        )
        auth_response = aws.authenticate_user()
        return auth_response


async def async_get_api(
    username: str, password: str, *, phyn_brand: str = "phyn", session: Optional[ClientSession] = None,
    verify_ssl: bool = True
) -> API:
    """Instantiate an authenticated API object.

    :param session: An ``aiohttp`` ``ClientSession``
    :type session: ``aiohttp.client.ClientSession``
    :param email: A Phyn email address
    :type email: ``str``
    :param password: A Phyn password
    :type password: ``str``
    :param phyn_brand: A brand for phyn
    :type phyn_brand: ``str``
    :rtype: :meth:`aiophyn.api.API`
    """
    api = API(username, password, phyn_brand=phyn_brand, session=session)
    await api.async_authenticate()
    return api
