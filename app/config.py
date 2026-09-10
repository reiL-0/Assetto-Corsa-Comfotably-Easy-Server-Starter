from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Every field is overridable via ACM_* env vars or a .env file."""

    model_config = SettingsConfigDict(env_prefix="ACM_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: str = "data"
    db_path: str = ""  # empty -> <data_dir>/acmanager.db

    # Serve the bundled React UI from app/static. Set false to run the manager
    # as a pure API backend for a separately hosted frontend.
    serve_ui: bool = True

    # Cross-origin sites allowed to call the API (a remote frontend calling in).
    # Env (JSON): ACM_CORS_ORIGINS='["https://league.example.com"]'
    cors_origins: list[str] = []

    def resolved_db_path(self) -> str:
        return self.db_path or f"{self.data_dir}/acmanager.db"


settings = Settings()
