#!/usr/bin/env python3
"""
Auto-switch Clash/Mihomo selector node when the current node is too slow.

Works with Clash Verge / Clash Verge Rev when "External Controller" is enabled.
No third-party Python packages are required.

Usage:
  python3 -m clash_auto_switch.main [--once] [--force-scan] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any

from .clash_api import Api
from .config import Config
from .healthcheck import direct_network_online
from .switcher import (
    SwitcherState,
    _set_log_fn,
    maybe_periodic_refresh,
    next_interval,
    switch_if_needed,
)

LOG_LEVELS: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "ERROR": 40,
}


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

def log(config: Config, level: str, message: str) -> None:
    required = LOG_LEVELS.get(level.upper(), 20)
    if required < LOG_LEVELS.get(config.log_level.upper(), 20):
        return
    label = level.upper().ljust(5)
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), label, message, flush=True)


# these are also registered as the switcher's log callback
def _log_cb(level: str, message: str) -> None:
    """Callback used by switcher module for internal logging."""
    # We use a module-level config reference set in main()
    cfg = getattr(_log_cb, "_config", None)
    if cfg:
        log(cfg, level, message)


def log_info(config: Config, message: str) -> None:
    log(config, "INFO", message)


def log_warn(config: Config, message: str) -> None:
    log(config, "WARN", message)


def log_error(config: Config, message: str) -> None:
    log(config, "ERROR", message)


# ---------------------------------------------------------------------------
# setup helpers
# ---------------------------------------------------------------------------

def _maybe_autodetect_socket(config: Config) -> None:
    if config.socket:
        return
    candidates = [
        "/tmp/verge/verge-mihomo.sock",
        os.path.expanduser("~/.config/clash-verge/clash-mihomo.sock"),
    ]
    for path in candidates:
        if os.path.exists(path):
            config.socket = path
            log_info(config, f"auto-detected socket: {path}")
            return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(config: Config) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-switch Clash/Mihomo node by delay threshold.",
    )
    parser.add_argument("--api", default=None, help="override CLASH_API")
    parser.add_argument("--socket", default=None, help="override CLASH_SOCKET")
    parser.add_argument("--group", default=None, help="override CLASH_GROUP")
    parser.add_argument(
        "--threshold", type=int, default=None, help="override CLASH_THRESHOLD"
    )
    parser.add_argument(
        "--hard-threshold",
        type=int,
        default=None,
        help="override CLASH_HARD_THRESHOLD",
    )
    parser.add_argument(
        "--immediate-scan-threshold",
        type=int,
        default=None,
        help="override CLASH_IMMEDIATE_SCAN_THRESHOLD",
    )
    parser.add_argument(
        "--degraded-scan-count",
        type=int,
        default=None,
        help="override CLASH_DEGRADED_SCAN_COUNT",
    )
    parser.add_argument(
        "--interval", type=int, default=None, help="override CLASH_INTERVAL"
    )
    parser.add_argument(
        "--timeout-ms", type=int, default=None, help="override CLASH_TIMEOUT_MS"
    )
    parser.add_argument(
        "--stable-samples",
        type=int,
        default=None,
        help="override CLASH_STABLE_SAMPLES",
    )
    parser.add_argument(
        "--ideal-max-delay",
        type=int,
        default=None,
        help="override CLASH_IDEAL_MAX_DELAY",
    )
    parser.add_argument(
        "--safe-max-delay",
        type=int,
        default=None,
        help="override CLASH_SAFE_MAX_DELAY",
    )
    parser.add_argument(
        "--direct-check-url",
        default=None,
        help="override CLASH_DIRECT_CHECK_URL",
    )
    parser.add_argument(
        "--direct-check-timeout",
        type=float,
        default=None,
        help="override CLASH_DIRECT_CHECK_TIMEOUT",
    )
    parser.add_argument(
        "--prefilter-url",
        default=None,
        help="override CLASH_PREFILTER_URL",
    )
    parser.add_argument(
        "--prefilter-timeout-ms",
        type=int,
        default=None,
        help="override CLASH_PREFILTER_TIMEOUT_MS",
    )
    parser.add_argument(
        "--test-url",
        action="append",
        default=None,
        help="add a test URL (can repeat; replaces CLASH_TEST_URL list)",
    )
    parser.add_argument(
        "--exclude-keyword",
        action="append",
        default=None,
        help="add an exclude keyword (can repeat; replaces CLASH_EXCLUDE_KEYWORD)",
    )
    parser.add_argument(
        "--provider-priority",
        action="append",
        default=None,
        help="add a provider (can repeat; replaces CLASH_PROVIDER_PRIORITY)",
    )
    parser.add_argument(
        "--refresh-before-scan",
        action="store_true",
        default=None,
        help="refresh providers before scanning",
    )
    parser.add_argument(
        "--auto-refresh-on-failure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="auto-refresh when no node works",
    )
    parser.add_argument(
        "--refresh-cooldown",
        type=int,
        default=None,
        help="override CLASH_REFRESH_COOLDOWN",
    )
    parser.add_argument(
        "--provider-refresh-interval",
        type=int,
        default=None,
        help="override CLASH_PROVIDER_REFRESH_INTERVAL",
    )
    parser.add_argument(
        "--fast-recovery",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="stop scanning after first ideal node",
    )
    parser.add_argument(
        "--switch-to-best-even-if-slow",
        action="store_true",
        default=None,
        help="switch even when all candidates are above threshold",
    )
    parser.add_argument(
        "--lower-provider-regress-after",
        type=int,
        default=None,
        help="override CLASH_LOWER_PROVIDER_REGRESS_AFTER",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="log decisions without actually switching (overrides DRY_RUN env)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="override LOG_LEVEL",
    )
    parser.add_argument("--once", action="store_true", help="run once and exit")
    parser.add_argument(
        "--force-scan",
        action="store_true",
        help="force a full scan even when current node is healthy",
    )

    return parser.parse_args()


def _apply_cli_overrides(args: argparse.Namespace, config: Config) -> None:
    overrides: dict[str, Any] = {}
    arg_map = {
        "api": "api_base",
        "socket": "socket",
        "group": "group",
        "threshold": "threshold",
        "hard_threshold": "hard_threshold",
        "immediate_scan_threshold": "immediate_scan_threshold",
        "degraded_scan_count": "degraded_scan_count",
        "interval": "interval",
        "timeout_ms": "timeout_ms",
        "stable_samples": "stable_samples",
        "ideal_max_delay": "ideal_max_delay",
        "safe_max_delay": "safe_max_delay",
        "direct_check_url": "direct_check_url",
        "direct_check_timeout": "direct_check_timeout",
        "prefilter_url": "prefilter_url",
        "prefilter_timeout_ms": "prefilter_timeout_ms",
        "refresh_before_scan": "refresh_before_scan",
        "auto_refresh_on_failure": "auto_refresh_on_failure",
        "refresh_cooldown": "refresh_cooldown",
        "provider_refresh_interval": "provider_refresh_interval",
        "fast_recovery": "fast_recovery",
        "switch_to_best_even_if_slow": "switch_to_best_even_if_slow",
        "lower_provider_regress_after": "lower_provider_regress_after",
        "dry_run": "dry_run",
        "log_level": "log_level",
        "once": "once",
        "force_scan": "force_scan",
    }
    for arg_name, config_name in arg_map.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            overrides[config_name] = val

    if args.test_url is not None:
        overrides["test_urls"] = args.test_url
    if args.exclude_keyword is not None:
        overrides["exclude_keywords"] = args.exclude_keyword
    if args.provider_priority is not None:
        overrides["provider_priority"] = args.provider_priority

    config.apply_overrides(**overrides)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    config = Config()
    args = parse_args(config)
    _apply_cli_overrides(args, config)
    _maybe_autodetect_socket(config)

    # wire up switcher logging callback
    _log_cb._config = config  # type: ignore[attr-defined]
    _set_log_fn(_log_cb)

    if config.dry_run:
        log_info(config, "DRY-RUN mode enabled - no actual switch will be performed")

    if config.region_sticky:
        log_info(
            config,
            f"region-sticky enabled: preferred={config.preferred_region}, "
            f"backups={config.backup_regions}",
        )

    if not config.secret:
        log_warn(
            config,
            "CLASH_SECRET not set - API calls will be unauthenticated",
        )

    timeout = max(2.0, config.timeout_ms / 1000 + 1)
    api = Api(config.api_base, config.secret, timeout, config.socket)

    state = SwitcherState()
    stopping = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stopping:
        delay: int | None = None
        try:
            online = direct_network_online(config)
            was_online = state.direct_online
            state.direct_online = online

            if not online:
                state.needs_refresh_after_reconnect = True
                if was_online:
                    log_warn(config, "direct network offline; waiting for reconnect")
            else:
                if not was_online:
                    log_info(config, "direct network restored; refreshing providers")
                if state.needs_refresh_after_reconnect:
                    api.refresh_providers()
                    state.cache_invalidated_at = time.monotonic()
                    state.last_auto_refresh = time.monotonic()
                    state.last_periodic_refresh = time.monotonic()
                    state.needs_refresh_after_reconnect = False

            maybe_periodic_refresh(api, config, state)

            if online:
                delay, switched, target = switch_if_needed(api, config, state)

        except Exception as exc:
            log_error(config, str(exc))

        if config.once:
            break

        wait = next_interval(config, state, delay)
        for _ in range(wait):
            if stopping:
                break
            time.sleep(1)

    log_info(config, "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
