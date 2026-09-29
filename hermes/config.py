from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HERMES_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    searches_file: Path = Path("searches.yaml")
    # Fatos e preferências do Gabriel para o prompt. Fica fora do git (dado pessoal);
    # o modelo público é profile.example.md.
    profile_file: Path = Path("profile.md")
    # Pausa entre buscas, em segundos. Volume baixo protege o IP de casa de bloqueio.
    pause_min: float = 4.0
    pause_max: float = 9.0

    # Sem prefixo: é o nome que o SDK da OpenAI e todo mundo usa.
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "HERMES_OPENAI_API_KEY")
    )
    openai_model: str = "gpt-5.4-mini"
    score_min: int = 60

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.db"
