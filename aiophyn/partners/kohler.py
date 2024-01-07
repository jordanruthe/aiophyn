""" Define Kohler Partner class """

import logging
import re
import uuid
import json
import base64
import binascii
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from datetime import datetime, timedelta

from typing import Optional

from aiohttp import ClientSession, ClientTimeout, CookieJar

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: int = 10

class KOHLER_API:
    def __init__(
        self, username: str, password: str, session: Optional[ClientSession] = None,
        verify_ssl: bool = True, proxy: Optional[str] = None, proxy_port: Optional[int] = None
    ):
        self._username: str = username
        self._password: str = password
        self._phyn_password: str = None
        self._user_id: str = None

        self._token: str = None
        self._token_expiration = None
        self._refresh_token = None
        self._refresh_token_expiration = None
        self._mobile_data = None

        self.verify_ssl = verify_ssl
        self.ssl = False if verify_ssl is False else None
        self.proxy = proxy
        self.proxy_port = proxy_port
        self.proxy_url: Optional[str] = None
        if self.proxy is not None and self.proxy_port is not None:
            self.proxy_url = "https://%s:%s" % (proxy, proxy_port)

        self._session: ClientSession = None

    def get_cognito_info(self):
        return self._mobile_data['cognito']

    def get_mqtt_info(self):
        return self._mobile_data['wss']

    def get_phyn_password(self):
        return self._phyn_password

    async def authenticate(self):
        use_running_session = self._session and not self._session.closed
        if not use_running_session:
            self._session = ClientSession(timeout=ClientTimeout(total=DEFAULT_TIMEOUT), cookie_jar=CookieJar())

        await self.b2c_login()
        token = await self.get_phyn_token()
        await self._session.close()
        self._phyn_password = await self.token_to_password(token)

    async def b2c_login(self):
        _LOGGER.debug("Logging into Kohler")
        client_request_id = str(uuid.uuid4())

        # Get CSRF token and initialize values
        params = {
          "response_type": "code",
          "client_id": "8caf9530-1d13-48e6-867c-0f082878debc",
          "client-request-id": client_request_id,
          "scope": "https%3A%2F%2Fkonnectkohler.onmicrosoft.com%2Ff5d87f3d-bdeb-4933-ab70-ef56cc343744%2Fapiaccess%20openid%20offline_access%20profile",
          "redirect_uri": "msauth%3A%2F%2Fcom.kohler.hermoth%2F2DuDM2vGmcL4bKPn2xKzKpsy68k%253D",
          "prompt": "login",
        }
        get_vars = '&'.join([ f"{x[0]}={x[1]}" for x in params.items() ])
        resp = await self._session.get(
            'https://konnectkohler.b2clogin.com/tfp/konnectkohler.onmicrosoft.com/B2C_1A_signin/oAuth2/v2.0/authorize?'
            + get_vars, ssl=self.ssl, proxy=self.proxy_url)
        match = re.search(r'"(StateProperties=([a-zA-Z0-9]+))"', await resp.text())
        state_properties = match.group(1)

        cookies = self._session.cookie_jar.filter_cookies('https://konnectkohler.b2clogin.com')
        csrf = None
        for key, cookie in cookies.items():
            if key == "x-ms-cpim-csrf":
                csrf = cookie.value

        # Login
        headers = {
            "X-CSRF-TOKEN": csrf,
        }
        login_vars = {
            "request_type": "RESPONSE",
            "signInName": self._username,
            "password": self._password,
        }
        resp = await self._session.post("https://konnectkohler.b2clogin.com/konnectkohler.onmicrosoft.com/B2C_1A_signin/SelfAsserted?p=B2C_1A_signin&" + state_properties, headers=headers, data=login_vars,
                                        ssl=self.ssl, proxy=self.proxy_url)

        params = {
            "rememberMe": "false",
            "csrf_token": csrf,
            "tx": state_properties,
            "p": "B2C_1A_signin"
        }
        args = '&'.join([ f"{x[0]}={x[1]}" for x in params.items() ])
        resp = await self._session.get("https://konnectkohler.b2clogin.com/konnectkohler.onmicrosoft.com/B2C_1A_signin/api/CombinedSigninAndSignup/confirmed?" + args, allow_redirects=False,
                                       ssl=self.ssl, proxy=self.proxy_url)
        matches = re.search(r'code=([^&]+)', resp.headers['Location'])
        code = matches.group(1)

        # Get tokens
        headers = {
            "x-app-name": "com.kohler.hermoth",
            "x-app-ver": "2.7",
        }
        params = {
            "client-request-id": client_request_id,
            "client_id": "8caf9530-1d13-48e6-867c-0f082878debc",
            "client_info": "1",
            "x-app-name": "com.kohler.hermoth",
            "x-app-ver": "2.7",
            "redirect_uri": "msauth://com.kohler.hermoth/2DuDM2vGmcL4bKPn2xKzKpsy68k%3D",
            "scope": "https://konnectkohler.onmicrosoft.com/f5d87f3d-bdeb-4933-ab70-ef56cc343744/apiaccess openid offline_access profile",
            "grant_type": "authorization_code",
            "code": code,
        }
        resp = await self._session.post("https://konnectkohler.b2clogin.com/tfp/konnectkohler.onmicrosoft.com/B2C_1A_signin/%2FoAuth2%2Fv2.0%2Ftoken", data=params,
                                        ssl=self.ssl, proxy=self.proxy_url)

        data = await resp.json()
        if "client_info" not in data:
            await self._session.close()
            raise Exception("Unable to get client data")

        client_info = json.loads(base64.b64decode(data['client_info'] + '==').decode())
        self._user_id = re.sub('-b2c_1a_signin$', '', client_info['uid'])
        #await self.home.set_user_id(self._uid)

        self._token = data['access_token']
        self._token_expiration = datetime.now() + timedelta(seconds=data['expires_in'])
        self._refresh_token = data['refresh_token']
        self._refresh_token_expiration = datetime.now() + timedelta(seconds=data['refresh_token_expires_in'])
        _LOGGER.debug("Received Kohler Token")

    async def get_phyn_token(self):
        params = {
          "partner": "kohler",
          "partner_user_id": self._user_id,
          "email": self._username,
        }
        args = "&".join([ f"{x[0]}={x[1]}" for x in params.items() ])
        headers = {
          "Accept": "application/json",
          "Accept-encoding": "gzip",
          "Authorization": f"Bearer partner-{self._token}",
          "Content-Type": "application/json",
          "User-Agent": "okhttp/4.10.0"
        }

        _LOGGER.info("Getting Kohler settings from Phyn")
        resp = await self._session.get(f"https://api.phyn.com/settings/app/com.kohler.mobile?{args}", headers=headers,
                                       ssl=self.ssl, proxy=self.proxy_url)
        mobile_data = await resp.json()
        if "error_msg" in mobile_data:
            await self._session.close()
            raise Exception("Kohler %s" % mobile_data['error_msg'])

        if "cognito" not in mobile_data:
            await self._session.close()
            raise Exception("Unable to find cognito information")
        self._mobile_data = mobile_data

        _LOGGER.debug("Getting token from Phyn")
        params = {
          "email": self._username,
          "partner": "kohler",
          "partner_user_id": self._user_id
        }
        args = "&".join(["%s=%s" % (x, params[x]) for x in params.keys()])
        headers = {
          "Accept": "application/json, text/plain, */*",
          "Accept-encoding": "gzip",
          "Authorization": "Bearer partner-%s" % self._token,
          "Content-Type": "application/json",
          "x-api-key": mobile_data['pws_api']['app_api_key']
        }
        resp = await self._session.get(f"https://api.phyn.com/partner-user-setup/token?{args}", headers=headers,
                                       ssl=self.ssl, proxy=self.proxy_url)
        data = await resp.json()
        if "token" not in data:
            await self._session.close()
            raise Exception("Token not found")
        _LOGGER.debug("Token received")
        return data['token']

    async def token_to_password(self, token):
        b64hex = base64.b64decode((token + '=' * (5 - (len(token) % 4))).replace('_','/').replace('-','+')).hex()

        try:
            keydata = binascii.hexlify(base64.b64decode(self._mobile_data['partner']['comm_id'])).decode()
        except:
            raise Exception("Error getting password decryption key")

        key = keydata[32:]
        iv = b64hex[18:(18+32)]
        ct = b64hex[50:(len(b64hex)-64)]
        cipher = AES.new(bytes.fromhex(key), AES.MODE_CBC, iv=bytes.fromhex(iv))
        return unpad(cipher.decrypt(bytearray.fromhex(ct)), AES.block_size).decode()
