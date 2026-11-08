from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATA_AGENT_", env_file=".env", extra="ignore")

    database_path: Path = Path("data/agent.db")
    database_url: SecretStr | None = None

    @property
    def resolved_database_url(self):
        if self.database_url is not None:
            return self.database_url
        from sqlalchemy.engine import URL
        return URL.create('sqlite', database=str(self.database_path))

    host: str = "127.0.0.1"
    port: int = 8100
    public_origin: str = "http://localhost:8100"
    cookie_secure: bool = False
    session_seconds: int = Field(default=43200, ge=60)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    llm_timeout: float = Field(default=60, gt=0, le=300)
    llm_max_tokens: int = Field(default=4096, ge=256, le=16384)
    context_tokens: int = Field(default=24000, ge=8000, le=200000)
    max_tool_rounds: int = Field(default=8, ge=1, le=20)
    lease_seconds: int = Field(default=120, ge=15)
    poll_seconds: float = Field(default=1, gt=0, le=60)
    worker_concurrency: int = Field(default=3, ge=1, le=20)
    log_level: str = 'INFO'
    skills_path: Path = Path(__file__).resolve().parent.parent / "skills"
    web_dist: Path = Path("web/dist")
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_verification_token: SecretStr = SecretStr("")
    feishu_encrypt_key: SecretStr = SecretStr("")
