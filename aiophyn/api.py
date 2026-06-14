"""Define a base client for interacting with Phyn."""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import boto3
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError, ClientResponseError
from botocore.exceptions import ClientError as BotocoreClientError
from pycognito.aws_srp import AWSSRP

from .alert import Alert
from .mqtt import MQTTClient
from .device import Device
from .errors import AuthenticationError, RequestError
from .home import Home


_LOGGER = logging.getLogger(__name__)

DEFAULT_HEADER_CONTENT_TYPE: str = "application/json"
DEFAULT_HEADER_USER_AGENT: str = "phyn/18 CFNetwork/1331.0.7 Darwin/21.4.0"
DEFAULT_HEADER_CONNECTION: str = "keep-alive"
DEFAULT_HEADER_API_KEY: str = "E7nfOgW6VI64fYpifiZSr6Me5w1Upe155zbu4lq8"
DEFAULT_HEADER_ACCEPT: str = "application/json"
DEFAULT_HEADER_ACCEPT_ENCODING: str = "gzip, deflate, br"

COGNITO_REGION: str = "us-east-1"
COGNITO_POOL_ID: str = "us-east-1_UAv6IUsyh"
COGNITO_CLIENT_ID: str = "5q2m8ti0urmepg4lup8q0ptldq"

DEFAULT_TIMEOUT: int = 10


class API:
    """Define the API object."""

    def __init__(
        self, username: str, password: str, *, phyn_brand: str = "phyn", session: Optional[ClientSession] = None,
        client_id: Optional[str] = None, verify_ssl: bool = True, proxy: Optional[str] = None,
        proxy_port: Optional[int] = None
    ) -> None:
        """Initialize."""
        self._username: str = username
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
        self._id_token = None
        self._refresh_token = None

        self.verify_ssl = verify_ssl
        self.proxy = proxy
        self.proxy_port = proxy_port
        self.proxy_url: Optional[str] = None
        if self.proxy is not None and self.proxy_port is not None:
            self.proxy_url = f"https://{proxy}:{proxy_port}"

        self._token: Optional[str] = None
        self._token_expiration: Optional[datetime] = None

        self.alert: Alert = Alert(self._request)
        self.home: Home = Home(self._request)
        self.device: Device = Device(self._request)
        self.mqtt = MQTTClient(self, client_id=client_id, verify_ssl=verify_ssl, proxy=proxy, proxy_port=proxy_port)

    @property
    def username(self) -> Optional[str]:
        """Get the API username"""
        return self._username

    async def _request(self, method: str, url: str, token_type: str = "access", _auth_retry: bool = True, **kwargs) -> dict:
        """Make a request against the API.

        :param method: GET or POST request
        :type method: str
        :param url: API URL
        :type url: str
        :param token_type: ID or Access token, defaults to "access"
        :type token_type: str, optional
        :raises RequestError: Error if issue accessing URL
        :return: JSON response
        :rtype: dict
        """
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
                "x-api-key": DEFAULT_HEADER_API_KEY,
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

        if self.proxy_url is not None:
            kwargs["proxy"] = self.proxy_url

        if not self.verify_ssl:
            kwargs["ssl"] = False

        use_running_session = self._session and not self._session.closed

        if use_running_session:
            session = self._session
        else:
            session = ClientSession(timeout=ClientTimeout(total=DEFAULT_TIMEOUT))

        try:
            async with session.request(method, url, **kwargs) as resp:
                resp.raise_for_status()
                data: dict = await resp.json(content_type=None)
                return data
        except ClientResponseError as err:
            if err.status in (401, 403) and _auth_retry:
                _LOGGER.info("Got %s; re-authenticating and retrying request", err.status)
                await self.async_authenticate()
                return await self._request(method, url, token_type, _auth_retry=False, **kwargs)
            if err.status in (401, 403):
                raise AuthenticationError(f"Unauthorized requesting {url}") from err
            raise RequestError(f"There was an error while requesting {url}") from err
        except ClientError as err:
            raise RequestError(f"There was an error while requesting {url}") from err
        finally:
            if not use_running_session:
                await session.close()

    async def _run_blocking(self, fn):
        """Run a blocking function in a thread pool executor."""
        return await asyncio.wrap_future(ThreadPoolExecutor().submit(fn))

    def _apply_auth_result(self, auth_response: dict) -> None:
        """Apply an AuthenticationResult dict to the instance token state."""
        res = auth_response["AuthenticationResult"]
        self._token = res["AccessToken"]
        self._token_expiration = datetime.now() + timedelta(seconds=res["ExpiresIn"])
        self._id_token = res["IdToken"]
        # REFRESH_TOKEN_AUTH does not return a new RefreshToken; keep the existing one.
        if "RefreshToken" in res:
            self._refresh_token = res["RefreshToken"]

    async def async_authenticate(self, *, allow_refresh: bool = True) -> None:
        """Authenticate the user and set the access token with its expiration.

        Prefers the Cognito REFRESH_TOKEN_AUTH flow when a refresh token is available,
        falling back to the full SRP login if the refresh token is expired or missing.
        """
        if allow_refresh and self._refresh_token:
            try:
                _LOGGER.info("Refreshing access token via refresh token")
                self._apply_auth_result(await self._run_blocking(self._refresh_token_auth))
                return
            except BotocoreClientError as err:
                _LOGGER.warning(
                    "Refresh-token auth failed (%s); falling back to SRP login",
                    err.response["Error"]["Code"],
                )
                self._refresh_token = None

        try:
            _LOGGER.info("Requesting token from AWS via SRP")
            self._apply_auth_result(await self._run_blocking(self._authenticate))
        except BotocoreClientError as err:
            raise AuthenticationError("Unable to authenticate with Phyn") from err

    def _refresh_token_auth(self) -> dict:
        """Use a Cognito refresh token to obtain a new access token (synchronous)."""
        client = boto3.client("cognito-idp", region_name=self._cognito['region'])
        return client.initiate_auth(
            ClientId=self._cognito['app_client_id'],
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": self._refresh_token},
        )

    def _authenticate(self) -> dict:
        """Full SRP authentication (synchronous, runs in a thread pool executor)."""
        client = boto3.client("cognito-idp", region_name=self._cognito['region'])
        aws = AWSSRP(
            username=self._username,
            password=self._password,
            pool_id=self._cognito['pool_id'],
            client_id=self._cognito['app_client_id'],
            client=client,
        )
        return aws.authenticate_user()


async def async_get_api(
    username: str, password: str, *, phyn_brand: str = "phyn", session: Optional[ClientSession] = None,
    client_id: Optional[str] = None, verify_ssl: bool = True, proxy: Optional[str] = None,
    proxy_port: Optional[int] = None
) -> API:
    """Instantiate an authenticated API object.

    :param session: An ``aiohttp`` ``ClientSession``
    :type session: ``aiohttp.client.ClientSession``
    :param email: A Phyn email address
    :type email: ``str``
    :param password: A Phyn password
    :type password: ``str``
    :param phyn_brand: Deprecated; accepted for backward compatibility but ignored.
    :type phyn_brand: ``str``
    :param client_id: A MQTT client id name
    :type client_id: ``str``
    :param verify_ssl: Should SSL certificates be verified
    :type verify_ssl: ``bool``
    :param proxy: HTTP proxy hostname/IP
    :type proxy: ``str``
    :param proxy_port: Port for HTTP proxy
    :type proxy_port: ``int``
    :rtype: :meth:`aiophyn.api.API`
    """
    api = API(username, password, phyn_brand=phyn_brand, session=session, client_id=client_id,
              verify_ssl=verify_ssl, proxy=proxy, proxy_port=proxy_port)
    await api.async_authenticate()
    return api
