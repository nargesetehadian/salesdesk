"""Typed, validated settings read once from environment variables.

Development and production are independent: production-only requirements (secrets, access
control) are enforced only when the real bot starts, never for the demo or tooling commands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENVIRONMENTS = ("development", "production")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    env: str = "development"
    telegram_token: str = ""
    bale_token: str = ""
    db_path: Path = Path("data/salesdesk.db")
    demo_db_path: Path = Path("data/demo.db")
    backup_dir: Path = Path("data/backups")
    backup_keep: int = 7
    timezone: ZoneInfo = ZoneInfo("Asia/Tehran")
    stale_days: int = 10
    digest_hour: int = 8
    reminder_minutes: int = 60
    team_code: str = ""
    ceo_ids: frozenset[tuple[str, str]] = frozenset()  # (platform or "*", external id)
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    dashboard_key: str = ""
    public_url: str = ""
    log_level: str = "INFO"
    tokens: dict[str, str] = field(init=False, default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tokens",
            {n: t for n, t in (("telegram", self.telegram_token), ("bale", self.bale_token)) if t},
        )  # fmt: skip

    def is_ceo(self, platform: str, ext_id: str) -> bool:
        return (platform, ext_id) in self.ceo_ids or ("*", ext_id) in self.ceo_ids

    def validate(self, *, need_platform: bool = True, enforce_production: bool | None = None) -> None:
        """Always checks value ranges; checks production secrets only when `enforce_production`
        (default: whenever env is production)."""
        problems = []
        if self.env not in ENVIRONMENTS:
            problems.append(f"APP_ENV must be one of {ENVIRONMENTS}, got {self.env!r}")
        if not 0 <= self.digest_hour <= 23:
            problems.append("DIGEST_HOUR must be between 0 and 23")
        if self.reminder_minutes < 1 or self.stale_days < 1:
            problems.append("REMINDER_MINUTES and STALE_DAYS must be at least 1")
        if not 0 <= self.web_port <= 65535:
            problems.append("WEB_PORT must be between 0 and 65535")
        if need_platform and not self.tokens:
            problems.append("set BALE_BOT_TOKEN and/or TELEGRAM_BOT_TOKEN")
        if enforce_production is None:
            enforce_production = self.env == "production"
        if enforce_production and self.env == "production":
            if len(self.dashboard_key) < 16:
                problems.append("DASHBOARD_KEY must be at least 16 characters in production")
            if not self.team_code:
                problems.append("TEAM_CODE is required in production (invite code for employees)")
            if not self.ceo_ids:
                problems.append("CEO_IDS is required in production")
        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))

    @classmethod
    def from_env(cls, *, need_platform: bool = True) -> Settings:
        """`need_platform=True` is the real bot start-up (strict); False is for demo/tooling."""
        from dotenv import load_dotenv

        load_dotenv()
        get = lambda k, d="": os.getenv(k, d).strip()  # noqa: E731

        def number(name: str, default: int) -> int:
            raw = get(name, str(default))
            try:
                return int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None

        tz_name = get("TIMEZONE", "Asia/Tehran")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"TIMEZONE {tz_name!r} is not a known time zone (try Asia/Tehran)") from None
        ceo = set()
        for item in filter(None, (p.strip() for p in get("CEO_IDS").split(","))):
            platform, _, ext = item.rpartition(":")
            ceo.add((platform or "*", ext))
        settings = cls(
            env=get("APP_ENV", "development").lower(),
            telegram_token=get("TELEGRAM_BOT_TOKEN"),
            bale_token=get("BALE_BOT_TOKEN"),
            db_path=Path(get("DB_PATH", "data/salesdesk.db")),
            demo_db_path=Path(get("DEMO_DB_PATH", "data/demo.db")),
            backup_dir=Path(get("BACKUP_DIR", "data/backups")),
            backup_keep=number("BACKUP_KEEP", 7),
            timezone=tz,
            stale_days=number("STALE_DAYS", 10),
            digest_hour=number("DIGEST_HOUR", 8),
            reminder_minutes=number("REMINDER_MINUTES", 60),
            team_code=get("TEAM_CODE"),
            ceo_ids=frozenset(ceo),
            web_host=get("WEB_HOST", "127.0.0.1"),
            web_port=number("WEB_PORT", 8080),
            dashboard_key=get("DASHBOARD_KEY"),
            public_url=get("PUBLIC_URL").rstrip("/"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate(need_platform=need_platform, enforce_production=need_platform)
        return settings
