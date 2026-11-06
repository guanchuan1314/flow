from functools import lru_cache

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"  # dev | prod
    app_name: str = "Flow"
    secret_key: str = INSECURE_DEFAULT_SECRET
    # Encrypts MT5 manager passwords stored in the database. Defaults to SECRET_KEY; set it
    # separately so SECRET_KEY can be rotated without re-entering server passwords.
    credentials_key: str = ""
    # PostgreSQL only, e.g. postgresql+psycopg://crm:PASSWORD@127.0.0.1:5432/crm
    database_url: str = ""
    # Public base URL, used in emails (password reset links, referral links)
    public_url: str = "http://127.0.0.1:8000"
    # Comma-separated host names the app answers to (Host-header protection). "*" = any.
    allowed_hosts: str = "*"

    access_token_minutes: int = 60
    client_token_minutes: int = 60 * 12
    session_hours: int = 12  # longest a web sign-in lasts
    # Web sessions end after this long without a request
    staff_session_idle_minutes: int = 60
    client_session_idle_minutes: int = 120
    staff_2fa_required: bool = False
    # From MAX_FAILED_LOGINS failed logins on, each failure makes the account wait 1, 2, 4, ... minutes,
    # at most LOCKOUT_MINUTES
    max_failed_logins: int = 5
    lockout_minutes: int = 30
    password_min_length: int = 8
    password_reset_minutes: int = 30
    # Per-IP limit on login / register / reset attempts (requests per minute)
    auth_rate_limit_per_minute: int = 20

    mt5_gateway: str = "mock"  # mock | manager
    mt5_server: str = "127.0.0.1:443"
    mt5_manager_login: int = 0
    mt5_manager_password: str = ""
    mt5_timeout_ms: int = 30000
    # Simulator only: file that keeps simulated MT5 state across restarts (empty = memory only)
    mock_state_file: str = ""
    # Manager API local cache folder; each process gets its own sub-folder (api / worker).
    mt5_work_dir: str = "./mt5data"

    # Run the sync/copy worker threads inside the API process. Defaults to on for the
    # mock gateway (its in-memory state can't be shared across processes), off otherwise.
    embedded_worker: bool | None = None

    sync_interval_seconds: int = 10
    # Accounts with no open position or order and no deal in the last SYNC_ACTIVE_MINUTES are polled only every
    # SYNC_IDLE_INTERVAL_SECONDS (spread evenly), so the worker's load on MT5 follows activity, not account count.
    # A new deal (pushed or polled) makes an account active again.
    sync_idle_interval_seconds: int = 300
    sync_active_minutes: int = 30
    # Read every account's last terminal connection from the Manager API's local copy every N minutes (0 = off).
    # Costs MT5 nothing; the inactive demo accounts list is built from it.
    activity_refresh_minutes: int = 5
    # Server health page: read each server's load every N minutes (0 = only on demand); keep samples N days
    health_interval_minutes: int = 5
    health_keep_days: int = 7
    # Synchronizations started in the back-office run in the background, this many at once (Imports > History)
    sync_threads: int = 4
    # Import MT5 groups/users into account groups/trading accounts every N minutes (0 = manual only)
    mt5_import_interval_minutes: int = 15
    # MT5 groups never imported (fnmatch patterns, comma separated)
    mt5_sync_exclude_groups: str = r"managers\*,manager\*,preliminary*,coverage*"
    sync_overlap_seconds: int = 120
    storage_dir: str = "./storage"
    log_dir: str = "./logs/crm"  # empty = console only (e.g. when a service manager captures output)
    log_level: str = "INFO"
    # Apply pending Alembic migrations on startup. Recommended off in production:
    # run `python -m app.cli migrate` as a deployment step instead.
    auto_migrate: bool = True
    # Broker-specific modules (docs/custom/README.md); false runs Flow without them
    custom_enabled: bool = True
    custom_dir: str = "custom"

    # Shown to clients on the deposit page (bank details, reference format, ...)
    deposit_instructions: str = "Transfer funds to our bank account and enter the transfer reference below."

    # ---- public website & client area branding
    brand_tagline: str = "Trade global markets with confidence"
    accent_color: str = "#2563eb"  # buttons, links and highlights (#RRGGBB)
    company_legal_name: str = "Flow Ltd"
    # Shown in the footer; replace with your real licence wording.
    regulatory_text: str = ("Flow Ltd is a demonstration company. Replace this text with your "
                            "licence number and regulator before going live.")
    support_email: str = "support@example.com"
    support_phone: str = ""
    mt5_server_name: str = "Flow-Live"  # what clients pick in the MT5 terminal
    mt5_download_windows: str = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
    mt5_download_android: str = "https://play.google.com/store/apps/details?id=net.metaquotes.metatrader5"
    mt5_download_ios: str = "https://apps.apple.com/app/metatrader-5/id413251709"
    mt5_webterminal_url: str = "https://web.metatrader.app/terminal"
    # Instruments shown in public price widgets
    public_symbols: str = "EURUSD,GBPUSD,USDJPY,AUDUSD,XAUUSD,XAGUSD,US30,NAS100,BTCUSD"

    base_currency: str = "USD"
    live_requires_kyc: bool = True

    # Copy trading
    copy_max_open_age_seconds: int = 60  # don't mirror opens detected later than this
    copy_lot_step: str = "0.01"
    copy_min_lot: str = "0.01"

    # Email (SMTP). When smtp_host is empty, emails are written to the log instead.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = "no-reply@example.com"

    @property
    def is_prod(self) -> bool:
        return self.app_env.lower() in ("prod", "production")

    @model_validator(mode="after")
    def _check_production(self):
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL, e.g. "
                             "postgresql+psycopg://crm:PASSWORD@127.0.0.1:5432/crm (local demo: run-demo.ps1)")
        if self.is_prod:
            problems = self.production_problems()
            if problems:
                raise ValueError(PROD_ERROR_HEADER + "".join(f"\n  - {p}" for p in problems))
        return self

    def production_problems(self) -> list[str]:
        """Settings that are fine for a local demo but unsafe with APP_ENV=prod."""
        problems = []
        if self.secret_key == INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random string of at least 32 characters")
        if not self.public_url.startswith("https://"):
            problems.append("PUBLIC_URL must be an https:// URL")
        if not [h for h in self.allowed_hosts.split(",") if h.strip()] or "*" in self.allowed_hosts:
            problems.append('ALLOWED_HOSTS must list your host names (not "*")')
        if not self.credentials_key:
            problems.append("CREDENTIALS_KEY must be set (encrypts MT5 manager passwords and 2FA secrets)")
        if not self.staff_2fa_required:
            problems.append("STAFF_2FA_REQUIRED must be true")
        if not self.smtp_host:
            problems.append("SMTP_HOST must be set (password reset and notification emails)")
        if self.auto_migrate:
            problems.append("AUTO_MIGRATE must be false (run `python -m app.cli migrate` when deploying)")
        if self.mt5_gateway.lower() == "mock":
            problems.append("MT5_GATEWAY must be manager, not mock (the simulator)")
        if self.embedded_worker:
            problems.append("EMBEDDED_WORKER must not be true (run the separate worker service)")
        if self.mock_state_file:
            problems.append("MOCK_STATE_FILE must be empty (simulator only)")
        return problems


PROD_ERROR_HEADER = ("APP_ENV=prod but the configuration is not safe for production. Fix these settings in .env "
                     "(or set APP_ENV=dev for a local demo):")


class ConfigError(RuntimeError):
    """The settings in .env / the environment are invalid; the message says what to fix."""


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pydantic's report is noisy; show just what to fix
        lines = [("".join(f"{x}".upper() for x in e["loc"][:1]) + ": " if e["loc"] else "")
                 + str(e.get("ctx", {}).get("error") or e["msg"]) for e in exc.errors()]
        raise ConfigError("\n".join(lines)) from None
