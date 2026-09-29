from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HERMES_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    searches_file: Path = Path("searches.yaml")
    # Pausa entre buscas, em segundos. Volume baixo protege o IP de casa de bloqueio.
    pause_min: float = 4.0
    pause_max: float = 9.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hermes.db"
