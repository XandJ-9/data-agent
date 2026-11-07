"""Request-scoped dependencies: authenticated identity (cookie session + CSRF)."""
import secrets

from fastapi import Request

from errors import ServiceError
from services import accounts


def store(request: Request):
    return request.app.state.store


def settings(request: Request):
    return request.app.state.settings


def current(request: Request) -> dict:
    identity = accounts.authenticate(request.app.state.store, request.cookies.get(accounts.COOKIE_NAME))
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-CSRF-Token', '')
        if not secrets.compare_digest(supplied, identity['csrf_token']):
            raise ServiceError(403, 'CSRF token required')
    return identity
