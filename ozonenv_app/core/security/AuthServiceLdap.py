# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import logging
import os
from ldap.ldapservice import LdapService
from ozonenv_app.core.security.AuthServiceJWT import AuthServiceJWT
from ozonenv.core.BaseModels import (
    Settings,
    Session,
    CoreModel
)
logger = logging.getLogger(__name__)


class AuthServiceLdap(AuthServiceJWT):

    def __init__(self):
        super().__init__()
        self.ldap_settings: CoreModel
        self.jwt_secret = os.getenv("JWT_SECRET_KEY")
        self.jwt_alg = os.getenv("JWT_ALGORITHM", "HS256")

    async def load_params(self):
        self.ldap_settings = await self.params_m.by_name(
            os.getenv("LDAP_PARAMS_NAME", "ldap"))

    async def ldap_auth(self, username="", password="") -> CoreModel:
        auth = None
        if self.ldap_settings:
            ldape = LdapService(
                self.ldap_setting.value['ldap_name'],
                self.ldap_setting.value['ldap_url'],
                self.ldap_setting.value['ldap_base_dn'],
                self.ldap_setting.value['ldap_bind_dn']
            )
            auth = await run_in_threadpool(
                lambda: ldape.authenticate(username, password))
            if auth:
                return await self.user_m.by_name(username)
        if not auth:
            return await self.user_auth(username, password)