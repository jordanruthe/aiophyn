""" Define Kohler Partner class """

import logging
import asyncio
import re
import uuid
import json
import base64
import hashlib
from Crypto import Random
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from datetime import datetime, timedelta

from typing import Optional

from aiohttp import ClientSession, ClientTimeout, CookieJar
from aiohttp.client_exceptions import ClientError

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: int = 10

class KOHLER_API:
    def __init__(
        self, username: str, password: str, session: Optional[ClientSession] = None
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

        self._session: ClientSession = session

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
        get_vars = '&'.join([ "%s=%s" % (x, params[x]) for x in params.keys() ])
        resp = await self._session.get('https://konnectkohler.b2clogin.com/tfp/konnectkohler.onmicrosoft.com/B2C_1A_signin/oAuth2/v2.0/authorize?' + get_vars)
        match = re.search(r'"(StateProperties=([a-zA-Z0-9]+))"', await resp.text())
        state_properties = match.group(1)

        cookies = self._session.cookie_jar.filter_cookies('https://konnectkohler.b2clogin.com')
        TRANS = None
        CSRF = None
        for key, cookie in cookies.items():
          if key == "x-ms-cpim-csrf":
            CSRF = cookie.value
          if key == "x-ms-cpim-trans":
            TRANS = cookie.value

        # Login
        headers = {
            "X-CSRF-TOKEN": CSRF,
        }
        login_vars = {
            "request_type": "RESPONSE",
            "signInName": self._username,
            "password": self._password,
        }
        resp = await self._session.post("https://konnectkohler.b2clogin.com/konnectkohler.onmicrosoft.com/B2C_1A_signin/SelfAsserted?p=B2C_1A_signin&" + state_properties, headers=headers, data=login_vars)

        params = {
            "rememberMe": "false",
            "csrf_token": CSRF,
            "tx": state_properties,
            "p": "B2C_1A_signin"
        }
        args = '&'.join([ "%s=%s" % (x, params[x]) for x in params.keys() ])
        resp = await self._session.get("https://konnectkohler.b2clogin.com/konnectkohler.onmicrosoft.com/B2C_1A_signin/api/CombinedSigninAndSignup/confirmed?" + args, allow_redirects=False)
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
        resp = await self._session.post("https://konnectkohler.b2clogin.com/tfp/konnectkohler.onmicrosoft.com/B2C_1A_signin/%2FoAuth2%2Fv2.0%2Ftoken", data=params)

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
        args = "&".join(["%s=%s" % (x, params[x]) for x in params.keys()])
        headers = {
          "Accept": "application/json",
          "Accept-encoding": "gzip",
          "Authorization": "Bearer partner-%s" % self._token,
          "Content-Type": "application/json",
          "User-Agent": "okhttp/4.10.0"
        }

        _LOGGER.info("Getting Kohler settings from Phyn")
        resp = await self._session.get("https://api.phyn.com/settings/app/com.kohler.mobile?%s" % args, headers=headers)
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
        resp = await self._session.get("https://api.phyn.com/partner-user-setup/token?%s" % args, headers=headers)
        data = await resp.json()
        if "token" not in data:
            await self._session.close()
            raise Exception("Token not found")
        _LOGGER.debug("Token received")
        return data['token']

    async def token_to_password(self, token):
        b64hex = base64.b64decode((token + '=' * (5 - (len(token) % 4))).replace('_','/').replace('-','+')).hex()
        key = "656c7330336659334872306e55446a4c" # AES Key for kohler
        iv = b64hex[18:(18+32)]
        ct = b64hex[50:(50+64)]
        cipher = AES.new(bytes.fromhex(key), AES.MODE_CBC, iv=bytes.fromhex(iv))
        return unpad(cipher.decrypt(bytearray.fromhex(ct)), AES.block_size).decode()
