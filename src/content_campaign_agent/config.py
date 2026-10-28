from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir():
    packaged = Path(__file__).parent / "data"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    model_timeout_seconds: float = Field(default=60, gt=0)
    max_concurrent_tasks: int = Field(default=3, ge=1, le=32)
    max_concurrent_model_requests: int = Field(default=3, ge=1, le=32)
    output_dir: Path = Path(".runs")
    data_dir: Path = Field(default_factory=default_data_dir)
    langfuse_enabled: bool = True
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"
    langfuse_project_id: str = ""

    def public_snapshot(self):
        return self.model_dump(
            mode="json",
            exclude={"deepseek_api_key", "langfuse_public_key", "langfuse_secret_key"},
        )
