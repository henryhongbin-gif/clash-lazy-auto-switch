"""
Configuration management.

Reads settings from environment variables and an optional .env file.
CLI arguments can override environment values.
The secret must ONLY come from env/.env – never from CLI args.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _load_dotenv() -> None:
    """Load .env file without any third-party dependency."""
    search_paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in search_paths:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        break


_load_dotenv()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    return int(raw.strip())


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    return float(raw.strip())


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_list(key: str, default: tuple[str, ...]) -> list[str]:
    raw = os.getenv(key)
    if raw is None:
        return list(default)
    items: list[str] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part:
            items.append(part)
    return items if items else list(default)


@dataclass
class Config:
    """All runtime configuration for the auto-switch script."""

    # ---- connection ----
    api_base: str = field(default_factory=lambda: _env_str("CLASH_API", "http://127.0.0.1:9097"))
    socket: str | None = field(default_factory=lambda: os.getenv("CLASH_SOCKET") or None)
    secret: str | None = field(default_factory=lambda: os.getenv("CLASH_SECRET") or None)
    group: str | None = field(default_factory=lambda: os.getenv("CLASH_GROUP") or None)

    # ---- thresholds ----
    threshold: int = field(default_factory=lambda: _env_int("CLASH_THRESHOLD", 200))
    hard_threshold: int = field(default_factory=lambda: _env_int("CLASH_HARD_THRESHOLD", 300))
    immediate_scan_threshold: int = field(
        default_factory=lambda: _env_int("CLASH_IMMEDIATE_SCAN_THRESHOLD", 500)
    )
    degraded_scan_count: int = field(
        default_factory=lambda: _env_int("CLASH_DEGRADED_SCAN_COUNT", 2)
    )

    # ---- timing ----
    interval: int = field(default_factory=lambda: _env_int("CLASH_INTERVAL", 3))
    timeout_ms: int = field(default_factory=lambda: _env_int("CLASH_TIMEOUT_MS", 800))
    stable_samples: int = field(default_factory=lambda: _env_int("CLASH_STABLE_SAMPLES", 3))

    ideal_max_delay: int = field(default_factory=lambda: _env_int("CLASH_IDEAL_MAX_DELAY", 300))
    safe_max_delay: int = field(default_factory=lambda: _env_int("CLASH_SAFE_MAX_DELAY", 400))

    # ---- direct network ----
    direct_check_url: str = field(
        default_factory=lambda: _env_str(
            "CLASH_DIRECT_CHECK_URL",
            "http://captive.apple.com/hotspot-detect.html",
        )
    )
    direct_check_timeout: float = field(
        default_factory=lambda: _env_float("CLASH_DIRECT_CHECK_TIMEOUT", 2.0)
    )

    # ---- prefilter ----
    prefilter_url: str = field(
        default_factory=lambda: _env_str(
            "CLASH_PREFILTER_URL",
            "https://www.google.com/generate_204",
        )
    )
    prefilter_timeout_ms: int = field(
        default_factory=lambda: _env_int("CLASH_PREFILTER_TIMEOUT_MS", 1500)
    )

    # ---- test targets ----
    test_urls: list[str] = field(
        default_factory=lambda: _env_list(
            "CLASH_TEST_URL",
            (
                "https://www.google.com/generate_204",
                "https://www.youtube.com/generate_204",
                "https://chatgpt.com/cdn-cgi/trace",
                "https://claude.ai/",
            ),
        )
    )

    # ---- exclusion ----
    exclude_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "CLASH_EXCLUDE_KEYWORD",
            (
                "剩余流量",
                "套餐到期",
                "距离下次重置",
            ),
        )
    )

    # ---- provider ----
    provider_priority: list[str] = field(
        default_factory=lambda: _env_list(
            "CLASH_PROVIDER_PRIORITY",
            ("良心云", "灵鹿", "魔戒"),
        )
    )

    refresh_before_scan: bool = field(
        default_factory=lambda: _env_bool("CLASH_REFRESH_BEFORE_SCAN", False)
    )
    auto_refresh_on_failure: bool = field(
        default_factory=lambda: _env_bool("CLASH_AUTO_REFRESH_ON_FAILURE", True)
    )
    refresh_cooldown: int = field(
        default_factory=lambda: _env_int("CLASH_REFRESH_COOLDOWN", 60)
    )
    provider_refresh_interval: int = field(
        default_factory=lambda: _env_int("CLASH_PROVIDER_REFRESH_INTERVAL", 1800)
    )

    # ---- scan behaviour ----
    fast_recovery: bool = field(
        default_factory=lambda: _env_bool("CLASH_FAST_RECOVERY", True)
    )
    switch_to_best_even_if_slow: bool = field(
        default_factory=lambda: _env_bool("CLASH_SWITCH_TO_BEST_EVEN_IF_SLOW", False)
    )
    lower_provider_regress_after: int = field(
        default_factory=lambda: _env_int("CLASH_LOWER_PROVIDER_REGRESS_AFTER", 600)
    )

    # ---- region sticky (NEW) ----
    region_sticky: bool = field(
        default_factory=lambda: _env_bool("REGION_STICKY", True)
    )
    preferred_region: str = field(
        default_factory=lambda: _env_str("PREFERRED_REGION", "新加坡")
    )
    backup_regions: list[str] = field(
        default_factory=lambda: _env_list(
            "BACKUP_REGIONS",
            ("日本", "香港", "台湾", "美国"),
        )
    )
    min_region_hold_seconds: int = field(
        default_factory=lambda: _env_int("MIN_REGION_HOLD_SECONDS", 1800)
    )
    switch_cooldown_seconds: int = field(
        default_factory=lambda: _env_int("SWITCH_COOLDOWN_SECONDS", 600)
    )

    # ---- dry-run (NEW) ----
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    # ---- log (NEW) ----
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))

    # ---- runtime-only flags (set by CLI, not env) ----
    once: bool = False
    force_scan: bool = False

    def apply_overrides(self, **kwargs: Any) -> None:
        """Apply CLI-sourced overrides (None values are skipped)."""
        for key, value in kwargs.items():
            if value is not None:
                setattr(self, key, value)
