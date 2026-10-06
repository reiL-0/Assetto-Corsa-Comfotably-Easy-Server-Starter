import shlex
from pathlib import Path

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
    # Where the per-server CPU/RAM limits are enforced (systemd transient scope around acServer, see supervisor.limit_prefix): "" = not enforced,
    # "user" = the manager's own systemd user manager (needs `loginctl enable-linger <user>`), "system" = the system one (manager running as root).
    limits_scope: str = ""
    port_range_start: int = 9600
    port_range_end: int = 9700
    log_lines: int = 500  # per-instance stdout ring buffer
    # Extra hosts content may be fetched from by link, besides MediaFire / Google Drive / Dropbox.
    # Env (JSON): ACM_DOWNLOAD_HOSTS='["files.example.com"]'
    download_hosts: list[str] = []
    # Stop an instance after this many seconds with no connected cars (0 = never).
    # A stopped server is started again via POST /servers/{id}/start.
    idle_stop_seconds: int = 0
    # Discord webhook for server started / stopped / crashed posts (empty = none). Env: ACM_DISCORD_STATUS_WEBHOOK
    discord_status_webhook: str = ""
    # Discord webhook for league announcements (scheduled-start reminders). Env: ACM_DISCORD_WEBHOOK
    discord_webhook: str = ""
    # Discord bot for the RSVP announcements (✅/❔/❌ reactions) and the account link (OAuth2 "identify"). Env: ACM_DISCORD_*
    discord_bot_token: str = ""
    discord_channel: str = ""  # channel id where the bot posts the RSVP announcements
    discord_role: str = ""  # role id mentioned at the top of the announcement (empty = no mention)
    discord_client_id: str = ""
    discord_client_secret: str = ""
    public_url: str = ""  # the manager's public base URL, e.g. https://acm.example.com; the OAuth redirect is <public_url>/api/v1/auth/discord/callback

    def acserver_dir(self) -> Path | None:
        """Directory of the acServer binary (holds `content/`, `system/`), or None when no binary is configured."""
        return Path(shlex.split(self.acserver_cmd)[0]).resolve().parent if self.acserver_cmd else None

    def resolved_db_path(self) -> str:
        return self.db_path or f"{self.data_dir}/acmanager.db"


settings = Settings()
