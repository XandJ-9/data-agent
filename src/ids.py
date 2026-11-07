"""Identifier generation (UUIDv4 strings) shared across subsystems."""
import uuid


def identifier() -> str:
    return str(uuid.uuid4())
