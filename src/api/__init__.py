"""HTTP gateway subsystem: the only place FastAPI types appear."""
from api.app import create_app

__all__ = ['create_app']
