"""Request models for the browser API. Strict, extra-forbidden, validated at the boundary."""
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class NewSession(Input):
    title: str = Field(default='New conversation', min_length=1, max_length=200)


class UpdateSession(Input):
    title: str = Field(min_length=1, max_length=200)


class Message(Input):
    text: str = Field(min_length=1, max_length=16000)
    client_message_id: str = Field(min_length=1, max_length=200)


class MemoryEntry(Input):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1000)
    source_message_ids: list[int] = Field(default_factory=list, max_length=50)


class MemoryEdit(Input):
    expected_revision: int = Field(ge=0)
    entries: list[MemoryEntry] = Field(max_length=100)


class Forget(Input):
    expected_revision: int = Field(ge=0)
    entry_id: str = Field(min_length=1, max_length=100)


class Enable(Input):
    enabled: bool


class SkillUpload(Input):
    content: str = Field(min_length=1, max_length=40000)


class McpServerCreate(Input):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z][a-z0-9-]{0,63}$')
    transport: str = Field(pattern=r'^(stdio|http)$')
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=50)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpServerUpdate(Input):
    transport: str = Field(pattern=r'^(stdio|http)$')
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=50)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class SwitchSession(Input):
    session_id: str = Field(min_length=1, max_length=100)


class Retry(Input):
    confirm_duplicate_risk: bool = False
