"""FastAPI application assembly: state, middleware, exception mapping."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from api import routes
from channels.feishu.webhook import FeishuModule
from errors import ServiceError
from settings import Settings
from storage import Store

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.resolved_database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info('Starting Data Agent on %s:%s', settings.host, settings.port)
        try:
            store.check_schema()
            logger.info('Database ready (backend: %s)', 'postgresql' if not store.db.is_sqlite else 'sqlite')
            yield
        finally:
            logger.info('Shutting down')
            store.dispose()

    app = FastAPI(title='Data Agent', lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    app.state.feishu = FeishuModule(store, settings)
    app.include_router(routes.router)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        logger.info('%s %s -> %s %s', request.method, request.url.path, exc.status, exc.detail)
        return JSONResponse({'detail': exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        logger.info('%s %s validation error: %s', request.method, request.url.path,
                    str(exc.errors())[:200])
        return JSONResponse({'detail': 'Invalid request data'}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.error('Database error on %s %s: %s', request.method, request.url.path, exc)
        return JSONResponse({'detail': 'Storage temporarily unavailable'}, status_code=503)

    @app.middleware('http')
    async def origin_guard(request: Request, call_next):
        if request.url.path.startswith('/api/v1/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('origin') != settings.public_origin:
                logger.warning('Blocked %s %s from untrusted origin %s',
                              request.method, request.url.path, request.headers.get('origin'))
                return JSONResponse({'detail': 'Untrusted request origin'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response

    return app
