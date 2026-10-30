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

    # False desliga a nota: toda vaga que passa no pré-filtro de título vira card.
    llm_enabled: bool = True
    # Quem dá a nota. As chaves ficam sem prefixo: são os nomes que os SDKs usam.
    llm_provider: Literal["openai", "gemini"] = "gemini"
    llm_model: str | None = None  # vazio = o padrão do provedor, abaixo
    # Usado quando o modelo principal responde 503 (só Gemini). Vazio desliga.
    llm_fallback_model: str | None = "gemini-3.5-flash-lite"
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "HERMES_OPENAI_API_KEY")
    )
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY", "HERMES_GEMINI_API_KEY"),
    )
    score_min: int = 60

    telegram_token: str | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_TOKEN", "HERMES_TELEGRAM_TOKEN")
    )
    telegram_chat_id: int | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_CHAT_ID", "HERMES_TELEGRAM_CHAT_ID")
    )
    # Webhook de um canal do Discord que recebe uma cópia dos cards. Vazio desliga.
    discord_webhook_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DISCORD_WEBHOOK_URL", "HERMES_DISCORD_WEBHOOK_URL"),
    )
    # Gmail que recebe o redirecionamento do Outlook (IMAP + senha de app).
    gmail_address: str | None = Field(
        default=None, validation_alias=AliasChoices("GMAIL_ADDRESS", "HERMES_GMAIL_ADDRESS")
    )
    gmail_app_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GMAIL_APP_PASSWORD", "HERMES_GMAIL_APP_PASSWORD"),
    )
    # Só os e-mails endereçados a este endereço são lidos (o Outlook que redireciona).
    mail_only_to: str | None = None
    mail_every_minutes: float = 15
    mail_first_run_days: int = 7

    @property
    def mail_enabled(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password and self.mail_only_to)

    search_every_hours: float = 2
    # Modo canal: por quantos dias a memória lembra uma vaga já postada.
    remember_days: float = 7
    # Modo canal: quantas vagas por dia o LLM pode classificar quando as regras não
    # acham o nível (as outras vão para "outros"). 0 desliga. Usa o provedor, o modelo
    # e a chave de cima; independe de llm_enabled, que é da nota do modo bot.
    level_llm_per_day: int = 0
    digest_hour: int = 8  # resumo diário e varredura dos 90 dias
    timezone: str = "America/Sao_Paulo"

    @property
    def llm_api_key(self) -> str | None:
        return {"openai": self.openai_api_key, "gemini": self.gemini_api_key}[self.llm_provider]

    @property
    def model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.db"
