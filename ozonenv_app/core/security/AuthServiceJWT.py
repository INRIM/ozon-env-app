# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import logging
import os

from ozonenv_app.core.security.AuthService import AuthService
from jose import JWTError, jwt

from ozonenv_app.core.services.DateEngine import DateEngine

logger = logging.getLogger(__name__)


class AuthServiceJWT(AuthService):

    def __init__(self):
        super().__init__()
        self.ldap_settings: CoreModel
        self.jwt_secret = os.getenv("JWT_SECRET_KEY")
        self.jwt_alg = os.getenv("JWT_ALGORITHM", "HS256")

    async def jwt_auth(
        self, security_scopes: SecurityScopes, token: str = Depends(oauth2_scheme)
    ) -> CoreModel:
        if security_scopes.scopes:
            authenticate_value = f'Bearer scope="{security_scopes.scope_str}"'
        else:
            authenticate_value = "Bearer"
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": authenticate_value},
        )
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_alg])
            username: str = payload.get("sub")
            if username is None:
                raise credentials_exception
            token_scopes = payload.get("scopes", [])
            token_data = TokenData(scopes=token_scopes, username=username)
        except (JWTError, ValidationError):
            raise credentials_exception
        user = await self.user_m.by_name(token_data.username)
        if user is None:
            raise credentials_exception
        for scope in security_scopes.scopes:
            if scope not in token_data.scopes:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Not enough permissions",
                    headers={"WWW-Authenticate": authenticate_value},
                )
        return user

    def create_token(
            self,
            uid: str,
            expires_delta: timedelta | None = None):
        to_encode = {"sub": uid, "scopes": []},
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(
                minutes=self.app_settings.session_expire_hours * 60)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(
            to_encode, self.jwt_secret, algorithm=self.jwt)
        return encoded_jwt