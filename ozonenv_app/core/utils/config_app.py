from fastapi.security import (
    OAuth2PasswordBearer,
)
from ozonenv.core.BaseModels import BaseModel
from passlib.context import CryptContext


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


ui_endpoint = [
    "/",
    "/dashboard",
    "/login",
    "/logout",
    "/export",
    "/table",
    "/action",
    '/favicon.ico',
    '/static',
]
singin_endpoint = ['/token', '/login']

session_free_endpoit = [
    "/status",
    "/static"
]
public_endpoint = [
    '/',
    '/token',
    '/login',
    '/layout',
    # '/api_routes',
    '/static',
    '/redoc',
    '/status',
    '/favicon.ico',
]


class OAuth2Token(BaseModel):
    access_token: str
    token_type: str


responses = {
    401: {
        "description": "Token non valido",
        "content": {
            "application/json": {"example": {"detail": "Auth invalid"}}
        },
    },
    403: {
        "description": "Forbidden Request",
        "content": {"application/json": {"example": {"detail": "Forbidden"}}},
    },
    422: {
        "description": "Dati richiesta non corretti",
        "content": {
            "application/json": {"example": {"detail": "err messsage"}}
        },
    },
}
tags_metadata = [
    {
        "name": ":-)",
        "description": 'Forms Inrim: <a href="/resources/docs">'
        'Resouces Docs</a> and <a href="/builder/docs"'
        '>Builder Docs </a>',
    },
]
