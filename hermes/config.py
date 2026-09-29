from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Modelos baratos com saída estruturada, conferidos na conta em 29/09/2026.
DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "gemini": "gemini-3.8-flash"}


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

    # Quem dá a nota. As chaves ficam sem prefixo: são os nomes que os SDKs usam.
    llm_provider: Literal["openai", "gemini"] = "gemini"
    llm_model: str | None = None  # vazio = o padrão do provedor, abaixo
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "HERMES_OPENAI_API_KEY")
    )
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY", "HERMES_GEMINI_API_KEY"),
    )
    score_min: int = 60

    @property
    def llm_api_key(self) -> str | None:
        return {"openai": self.openai_api_key, "gemini": self.gemini_api_key}[self.llm_provider]

    @property
    def model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.db"
