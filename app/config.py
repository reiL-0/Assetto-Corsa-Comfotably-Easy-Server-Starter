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

    # AC dedicated server. argv for the binary, e.g. "/opt/ac/acServer".
    # Empty -> start endpoints refuse.
    acserver_cmd: str = ""
    port_range_start: int = 9600
    port_range_end: int = 9700
    log_lines: int = 500  # per-instance stdout ring buffer
    # Stop an instance after this many seconds with no connected cars (0 = never).
    # A stopped server is started again via POST /servers/{id}/start.
    idle_stop_seconds: int = 0

    def resolved_db_path(self) -> str:
        return self.db_path or f"{self.data_dir}/acmanager.db"


settings = Settings()
