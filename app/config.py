import os
import shlex
from pathlib import Path
from typing import Literal

import re

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# The client's hub URL is its OWN site's neutral script (the installer writes it): https, a host, and exactly this path. Anything else (the league's address in any spelling, a typo, another path) = no hub.
_OWN_HUB_URL = re.compile(r"https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?/static/csp/hl_hub\.lua")
_OWN_TRAINER_URL = re.compile(r"https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?/static/csp/hl_trainer\.lua")


class Settings(BaseSettings):
    """Runtime config. Every field is overridable via ACM_* env vars or a .env file."""

    model_config = SettingsConfigDict(env_prefix="ACM_", env_file=".env", extra="ignore")

    # Which build of the manager this is: "opr" (the league's own, as it always was) or "client" (hosting for others). In «client» none of the league's CSP scripts is ever sent (their
    # addresses are the league's website: a client's drivers would load them from it and report to it), the chat words of the automatic telemetry token start with HL and Discord
    # gets a neutral user agent. A client that serves scripts of its own can say so when this has a way to name them.
    profile: Literal["opr", "client"] = "opr"
    claim_prefix: str = ""   # the chat words are <prefix>HELLO / <prefix>CLAIM: ("" = OPR, or HL in the client profile): 2 to 8 capital letters
    max_slots: int = 50      # most cars one server may hold (MAX_CLIENTS / entry list): the plan's cap for a client (32), 50 is the panel's own ceiling
    user_agent: str = ""     # what Discord is told we are ("" = OPR-AC-Manager, or HotLapHosting-Manager in the client profile)

    @field_validator("claim_prefix")
    @classmethod
    def _claim_prefix(cls, v: str) -> str:
        if v and not re.fullmatch(r"[A-Z]{2,8}", v):
            raise ValueError("ACM_CLAIM_PREFIX is 2 to 8 capital letters (the chat words are <prefix>HELLO and <prefix>CLAIM:)")
        return v

    @model_validator(mode="after")
    def _client_sends_no_script_of_the_league(self):
        if self.profile != "client" and self.claim_prefix not in ("", "OPR"):
            # the league's script says OPRHELLO: another word here and no token is ever issued, with nothing to say why
            raise ValueError("ACM_CLAIM_PREFIX other than OPR needs ACM_PROFILE=client (the script of the league's website speaks with OPR)")
        if self.profile == "client":
            # Phase 1 of the client's game scripts: the HUB only (neutral, served by the client's own site: its ACM_CSP_HUB_URL). The league's address is never sent to a client's drivers, so a hub URL that is
            # still the league's default means no hub.
            self.csp_hub = self.csp_hub and bool(_OWN_HUB_URL.fullmatch(self.csp_hub_url)) and "oppenpaddock" not in self.csp_hub_url.lower()
            # Phase 2: the trainer (+ telemetry) too, only from the client's own site (ACM_CSP_TRAINER_URL: the league's default, or anything that is not its own hl_trainer.lua, means no trainer). The claim exchange
            # (HLHELLO) answers only with ACM_CLAIM_SECRET set, and the client's site gives a code only to the drivers its admin switched on. The league's probe is never sent.
            self.csp_probe_servers = ""
            if not _OWN_TRAINER_URL.fullmatch(self.csp_trainer_url) or "oppenpaddock" in self.csp_trainer_url.lower():
                self.csp_trainer_servers = ""
            self.csp_hub_required = True if self.csp_hub_required is None else self.csp_hub_required
            self.csp_min_build = 3898 if self.csp_min_build is None else self.csp_min_build
            self.upload_permissive = self.upload_autodetect = True   # the owner's decision (2026-11-02): a client uploads any common archive (.zip .rar .7z .tar.gz…) and the kind is detected, as in the league's panel. The
            # price is the external unpacker (bsdtar/libarchive) for the non-zip formats: it runs in the disposable child of app/unpack.py (rlimits, timeout, real byte counter, magic bytes judged, one at a time) inside the unit's caps and the plan's quota
        if self.profile != "client":
            self.csp_hub_required = bool(self.csp_hub_required)
            self.csp_min_build = self.csp_min_build or 0
        return self

    def claim_word(self, suffix: str) -> str:
        """A word of the telemetry token's chat exchange: HELLO (the driver's script), CLAIM: (the manager's private answer) or PROBE: ."""
        return (self.claim_prefix or ("HL" if self.profile == "client" else "OPR")) + suffix

    def agent(self) -> str:
        """The user agent sent to Discord."""
        return self.user_agent or ("HotLapHosting-Manager" if self.profile == "client" else "OPR-AC-Manager")

    # The CSP hub script (opr_hub.lua, served by the website): added to the extra options of every server unless its checkbox in Control AC is off (`Server.csp_off`, app/csp.py `extra_for`).
    # `csp_hub_version` goes in the script URL: CSP caches a script by URL, so it is raised whenever the script changes. ACM_CSP_HUB=false turns it off everywhere.
    csp_hub: bool = True
    # CSP is MANDATORY on a hosted client's servers (the owner's decision, 2026-11-02: all its drivers use it, build 3898 = 0.3.0): the hub script is marked REQUIRED (CSP closes the game of whoever cannot load
    # it) and the server's TRACK carries the minimum build (`csp/<build>/../<track>`, CSP's own mechanism: cup.acstuff.club/docs/csp/misc/server-extra-options). None = the profile's default
    # (client: required, build 3898; the league: neither); 0 turns the minimum build off. Env: ACM_CSP_HUB_REQUIRED, ACM_CSP_MIN_BUILD
    csp_hub_required: bool | None = None
    csp_min_build: int | None = None
    csp_hub_url: str = "https://www.oppenpaddockracing.site/static/csp/opr_hub.lua"
    csp_hub_version: str = "20"   # 20: the capsule banners and the automatic blue flag and danger warning (/api/csp/auto); 19: 2/1 to green and the restart banners without NASCAR; 18: the banner of the place to give back; 17: the cut counter on the left; 16: the banners in English (language poll, key-based images); 15: the NASCAR banners load only with NASCAR = 1 in the hub's section (14: the closed-pits banner gets the lap it opens on written over it)
    # The three lists below take a selector (app/csp.py `server_listed`): `*` = every server, the ones made later too; `2` or `2,3` = those; empty = nobody; `!2` takes one out (`*,!2`).
    # Probe of the in-game race-control plugin (static/csp/opr_rc_probe.lua): sent as one more default script, only to the servers listed in ACM_CSP_PROBE_SERVERS (temporary: empty = nobody).
    csp_probe_url: str = "https://www.oppenpaddockracing.site/static/csp/opr_rc_probe.lua"
    csp_probe_servers: str = ""
    # The lap trainer (static/csp/opr_trainer.lua: braking-point bar, live delta, corner summaries, practice and qualifying only; it also SENDS the driver's telemetry in every session and asks for the
    # token with OPRHELLO): every server by default (checkbox per server in Control AC: `Server.csp_off`); ACM_CSP_TRAINER_SERVERS=`*,!3` / empty is only the ops brake and should stay unset.
    # `csp_trainer_version` goes in the URL like the hub's: raise it whenever the script changes.
    csp_trainer_url: str = "https://www.oppenpaddockracing.site/static/csp/opr_trainer.lua"
    csp_trainer_version: str = "12"   # 12: 5 frames a second instead of 10 (the ingest of the site is one process)
    csp_trainer_servers: str = "*"
    # Automatic telemetry token (app/live/acsp.py `_claim_issue`): the servers in ACM_CSP_CLAIM_SERVERS (every server by default, the ones made later too) answer a driver's "OPRHELLO" chat with a private
    # one-use code. Empty = nobody. It rests on the ACSP chat message reaching only the car it is addressed to (checked with the probe on server 2). The website's issue endpoint and the secret it shares with the manager
    # (the website's OPR_CLAIM_SECRET): ACM_CLAIM_URL, ACM_CLAIM_SECRET (empty secret = off).
    csp_claim_servers: str = "*"
    claim_url: str = "http://127.0.0.1:5100/api/telemetry/claim/issue"
    claim_secret: str = ""

    @field_validator("csp_probe_servers", "csp_trainer_servers", "csp_claim_servers")
    @classmethod
    def _selector(cls, v: str) -> str:
        """These take `*`, ids and `!id`, comma separated (app/csp.py `server_listed`). Anything else (`all`, `true`, `1;2`, `*,!*`) would switch things off or on without a word, so the manager
        refuses to start with it: the deploy then goes back to what was running."""
        for item in (x.strip() for x in v.split(",")):
            if item and not re.fullmatch(r"\*|!?\d+", item):
                raise ValueError(f"{item!r} is not a server selector: use *, a server id, or !id to leave one out (for example `*,!3`)")
        return v

    @field_validator("acserver_uid")
    @classmethod
    def _not_root(cls, v: int | None) -> int | None:
        """acServer as root would be worse than no separation, and as the manager's own user it would be none at all (the binary would own the manager's database and secrets):
        refuse to start rather than run a client's binary like that."""
        if v is not None and v < 1:
            raise ValueError("ACM_ACSERVER_UID must be a non-root user id (1 or more), or unset")
        if v is not None and v == os.geteuid():
            raise ValueError(f"ACM_ACSERVER_UID={v} is the user the manager runs as: there would be no separation; use another user")
        return v

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
    # Run every acServer as this Unix user (and group of the same number) instead of the manager's own: hosting for others, where the binary a client brings must not read the
    # manager's database or secrets. Needs CAP_SETUID/CAP_SETGID in the manager and `setpriv` (util-linux); the child loses every capability and cannot gain one (supervisor.spawn_prefix).
    # None = same user as the manager (as always). 0 (root) is refused. The instance folders must be writable by that group.
    acserver_uid: int | None = None
    # The plan's storage in MB for the admin panel's «espacio usado» card (app/storage.py); 0 = no limit shown. It only SHOWS the quota: nothing refuses an upload for it yet. Env: ACM_STORAGE_QUOTA_MB
    storage_quota_mb: int = 0
    unpack_timeout: int = 900   # seconds an uploaded archive may take to unpack before the child is killed (app/unpack.py)
    unpack_mem_mb: int = 1024   # memory cap of that child when limits_scope is on
    # Lenient uploads of cars/tracks (the league's own panel): any common archive (.7z, .rar, .tar.gz… through bsdtar), the car or track folder may sit anywhere inside
    # (mods ship `assettocorsa/content/tracks/<name>` plus a README) and its kind is detected. Off = strict (hosting for others): only .zip, one folder at the root, kind declared.
    upload_permissive: bool = False
    # The kind (car or track) is detected from the archive and its folder may sit anywhere inside (a mod's README beside it, `assettocorsa/content/cars/<name>`) while ONLY .zip is opened (no .rar/.7z/.tar:
    # those go through an external unpacker; that is upload_permissive). The panel never says whether an upload is a car or a track. The client profile turns on both (below). Env: ACM_UPLOAD_AUTODETECT
    upload_autodetect: bool = False
    port_range_start: int = 9600
    port_range_end: int = 9700
    resource_log_seconds: int = 30    # how often CPU/RAM/steal are written to <data_dir>/logs/resources-*.jsonl (app/resourcelog.py); 0 = off
    resource_log_keep_days: int = 30
    log_lines: int = 500  # per-instance stdout ring buffer
    # Extra hosts content may be fetched from by link, besides MediaFire / Google Drive / Dropbox.
    # Env (JSON): ACM_DOWNLOAD_HOSTS='["files.example.com"]'
    download_hosts: list[str] = []
    legacy_telemetry_ingest: bool = False   # POST /telemetry/ingest takes a SteamID64 and no login: only the old v0.1 in-game app (clients/OPRTelemetry) needs it
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
