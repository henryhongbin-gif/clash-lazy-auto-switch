"""
Node selection and switching strategy.

This is the decision-making core:
- Collect eligible candidates from Clash proxy groups
- Sort candidates by provider priority -> region -> line type -> name
- Enforce region-sticky policy (prefer Singapore, fall back in order)
- Enforce switch cooldowns (10 min intra-region, 30 min cross-region)
- Support dry-run mode (log intent without calling API)
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any

from .clash_api import Api, SPECIAL_NAMES
from .config import Config
from .healthcheck import (
    Candidate,
    Score,
    cached_score,
    proxy_score,
    remember_score,
)
from .region import Region, get_region, is_excluded, region_rank

OTHER_PROVIDER = "其他"


# ---------------------------------------------------------------------------
# runtime state (kept across cycles)
# ---------------------------------------------------------------------------

@dataclass
class SwitcherState:
    last_switch_time: float = 0.0
    last_region_switch_time: float = 0.0
    current_region: Region = Region.OTHER
    degraded_count: int = 0
    post_switch_rounds: int = 0
    score_cache: dict[str, tuple[float, int, int]] = field(default_factory=dict)
    cache_invalidated_at: float = 0.0
    last_auto_refresh: float = 0.0
    last_periodic_refresh: float = field(default_factory=time.monotonic)
    direct_online: bool = True
    needs_refresh_after_reconnect: bool = False
    current_provider: str = OTHER_PROVIDER
    lower_provider_since: float = 0.0


# ---------------------------------------------------------------------------
# log helper (kept here to avoid circular imports)
# ---------------------------------------------------------------------------

_LOG_FN = None


def _set_log_fn(fn: Any) -> None:
    global _LOG_FN  # noqa: PLW0603
    _LOG_FN = fn


def _log(level: str, msg: str) -> None:
    if _LOG_FN is not None:
        _LOG_FN(level, msg)


# ---------------------------------------------------------------------------
# candidate collection
# ---------------------------------------------------------------------------

def _provider_for_node(
    name: str,
    provider_members: dict[str, set[str]],
    priority: list[str],
) -> str:
    for provider in priority:
        if name in provider_members.get(provider, set()):
            return provider
    lowered = name.lower()
    for provider in priority:
        if provider.lower() in lowered:
            return provider
    return OTHER_PROVIDER


def _make_candidate(
    name: str,
    proxies: dict[str, Any],
    provider_members: dict[str, set[str]],
    provider_priority: list[str],
    exclude_keywords: list[str],
) -> Candidate | None:
    if not isinstance(name, str) or name in SPECIAL_NAMES:
        return None
    if is_excluded(name, exclude_keywords):
        return None
    item = proxies.get(name)
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("all"), list):
        return None
    provider = _provider_for_node(name, provider_members, provider_priority)
    region = get_region(name)
    # Stability rule: Hong Kong nodes are never eligible, even when a custom
    # exclude keyword list accidentally omits HK/Hong Kong variants.
    if region == Region.HONGKONG:
        return None
    return Candidate(name=name, provider=provider, region=region)


def collect_candidates(
    group_name: str,
    group: dict[str, Any],
    proxies: dict[str, Any],
    provider_members: dict[str, set[str]],
    config: Config,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()

    for name in group.get("all") or []:
        direct = _make_candidate(
            name,
            proxies,
            provider_members,
            config.provider_priority,
            config.exclude_keywords,
        )
        if direct:
            if direct.name not in seen:
                candidates.append(direct)
                seen.add(direct.name)
            continue

        nested_group = proxies.get(name)
        if (
            not isinstance(name, str)
            or not isinstance(nested_group, dict)
            or not isinstance(nested_group.get("all"), list)
        ):
            continue

        provider = (
            name
            if name in config.provider_priority
            else _provider_for_node(name, provider_members, config.provider_priority)
        )
        for nested_name in nested_group.get("all") or []:
            nested = _make_candidate(
                nested_name,
                proxies,
                provider_members,
                config.provider_priority,
                config.exclude_keywords,
            )
            if not nested:
                continue
            nested = Candidate(
                name=nested.name,
                provider=provider if provider != OTHER_PROVIDER else nested.provider,
                region=nested.region,
                parent_group=name,
            )
            if nested.name not in seen:
                candidates.append(nested)
                seen.add(nested.name)

    return candidates


# ---------------------------------------------------------------------------
# sort key - region priority is built in
# ---------------------------------------------------------------------------

def candidate_sort_key(
    candidate: Candidate,
    config: Config,
) -> tuple[int, int, int, int, str]:
    """Sort key for candidates.

    Priority order:
      1. 专线 first           (0 = 专线, 1 = non-专线)
      2. Preferred region     (0 = preferred, 1 = 1st backup, ...)
      3. 流媒体 first         (0 = 流媒体, 1 = non-流媒体)
      4. Provider priority    (0 = top provider, 1 = next, ...)
      5. Name (alphabetical tie-break)
    """
    name = candidate.name
    if not isinstance(name, str):
        return (99, 99, 99, 99, "")

    name_str: str = name
    line_rank = 0 if "专线" in name_str else 1
    rrank = region_rank(name_str, config.preferred_region, config.backup_regions)
    media_rank = 0 if "流媒体" in name_str else 1

    try:
        prov_rank = config.provider_priority.index(candidate.provider)
    except ValueError:
        prov_rank = len(config.provider_priority)

    return (line_rank, rrank, media_rank, prov_rank, name_str)


def _provider_rank(provider: str, config: Config) -> int:
    try:
        return config.provider_priority.index(provider)
    except ValueError:
        return len(config.provider_priority)


def _current_provider_for_node(
    now_node: str,
    provider_members: dict[str, set[str]],
    config: Config,
) -> str:
    if now_node in config.provider_priority:
        return now_node
    return _provider_for_node(now_node, provider_members, config.provider_priority)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

def _score_candidate(
    api: Api,
    config: Config,
    state: SwitcherState,
    candidate: Candidate,
) -> Score | None:
    cached = cached_score(
        state.score_cache,
        candidate,
        state.cache_invalidated_at,
        config.hard_threshold,
    )
    if cached:
        return cached

    prefilter = api.proxy_delay(
        candidate.name,
        config.prefilter_url,
        config.prefilter_timeout_ms,
    )
    if prefilter is None:
        return None

    score = proxy_score(
        api,
        candidate.name,
        config.test_urls,
        config.timeout_ms,
        config.stable_samples,
    )
    if score is None:
        return None

    average, maximum, samples = score
    remember_score(state.score_cache, candidate.name, average, maximum)
    return Score(
        candidate=candidate,
        average=average,
        maximum=maximum,
        samples=samples,
    )


def scan_best_candidate(
    api: Api,
    config: Config,
    state: SwitcherState,
    candidates: list[Candidate],
    now_node: str,
    allowed_regions: set[Region] | None,
    allow_slow: bool = True,
) -> Score | None:
    """Scan *candidates* and return the best Score that fits criteria."""

    for provider in config.provider_priority + [OTHER_PROVIDER]:
        provider_candidates = [
            c
            for c in candidates
            if c.provider == provider
            and (allowed_regions is None or c.region in allowed_regions)
        ]
        if not provider_candidates:
            continue

        provider_candidates.sort(key=lambda c: candidate_sort_key(c, config))

        provider_scores: list[Score] = []
        scan_targets = [c for c in provider_candidates if c.name != now_node]
        if not scan_targets:
            continue

        workers = max(1, min(config.scan_workers, len(scan_targets)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_score_candidate, api, config, state, candidate)
                for candidate in scan_targets
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    item = future.result()
                except Exception:
                    continue
                if item is None:
                    continue
                provider_scores.append(item)

        provider_scores.sort(
            key=lambda it: (it.average, it.maximum, it.candidate.name)
        )

        ideal = [
            s
            for s in provider_scores
            if s.average <= config.threshold
            and s.maximum <= config.ideal_max_delay
        ]
        if ideal:
            return ideal[0]

        safe = [
            s
            for s in provider_scores
            if s.average <= config.hard_threshold
            and s.maximum <= config.safe_max_delay
        ]
        if safe:
            return safe[0]

        if allow_slow and config.switch_to_best_even_if_slow and provider_scores:
            return provider_scores[0]

    return None


# ---------------------------------------------------------------------------
# region-sticky logic
# ---------------------------------------------------------------------------

def _region_scan_order(config: Config) -> list[Region]:
    """Return preferred + backup regions in configured order.

    Hong Kong is intentionally removed from the sequence, because HK nodes are
    excluded by policy and must never become a fallback region.
    """
    preferred_region = get_region(config.preferred_region)
    regions: list[Region] = []
    if preferred_region not in {Region.OTHER, Region.HONGKONG}:
        regions.append(preferred_region)
    for r in config.backup_regions:
        breg = get_region(r)
        if breg not in {Region.OTHER, Region.HONGKONG} and breg not in regions:
            regions.append(breg)
    return regions


def scan_region_sticky_candidate(
    api: Api,
    config: Config,
    state: SwitcherState,
    candidates: list[Candidate],
    now_node: str,
) -> tuple[Score | None, bool, str]:
    """Scan regions by health, not by candidate existence.

    Region-sticky means:
      1. Fully test preferred-region nodes first.
      2. Only if no healthy preferred node exists, test backups in order.
      3. If configured regions have no healthy nodes, test all non-HK nodes.
    """
    if not config.region_sticky:
        return (
            scan_best_candidate(api, config, state, candidates, now_node, None),
            False,
            "",
        )

    tested_regions: set[Region] = set()
    for region in _region_scan_order(config):
        region_candidates = [c for c in candidates if c.region == region]
        if not region_candidates:
            continue
        tested_regions.add(region)
        score = scan_best_candidate(
            api,
            config,
            state,
            candidates,
            now_node,
            {region},
            allow_slow=False,
        )
        if score is None:
            _log("INFO", f"region unhealthy: {region.value}; trying next region")
            continue
        is_cross = (
            state.current_region != Region.OTHER
            and state.current_region != score.candidate.region
        )
        reason = ""
        if is_cross:
            reason = (
                f"cross-region: healthy node found "
                f"({state.current_region.value} -> {score.candidate.region.value})"
            )
        return score, is_cross, reason

    score = scan_best_candidate(
        api,
        config,
        state,
        [c for c in candidates if c.region not in tested_regions],
        now_node,
        None,
        allow_slow=False,
    )
    if score is None:
        return None, False, ""
    is_cross = (
        state.current_region != Region.OTHER
        and state.current_region != score.candidate.region
    )
    reason = "region-sticky fallback: configured regions unhealthy, scanning remaining non-HK nodes"
    return score, is_cross, reason


def _primary_provider_regress_due(
    current_provider: str,
    config: Config,
    state: SwitcherState,
) -> bool:
    if not config.provider_priority:
        return False
    if config.lower_provider_regress_after <= 0:
        return False
    if _provider_rank(current_provider, config) == 0:
        return False
    if state.lower_provider_since <= 0:
        return False
    return (
        time.monotonic() - state.lower_provider_since
        >= config.lower_provider_regress_after
    )


def _primary_provider_candidates(
    candidates: list[Candidate],
    config: Config,
) -> list[Candidate]:
    if not config.provider_priority:
        return []
    primary = config.provider_priority[0]
    return [candidate for candidate in candidates if candidate.provider == primary]


# ---------------------------------------------------------------------------
# main switch logic
# ---------------------------------------------------------------------------

def choose_group(
    proxies: dict[str, Any],
    preferred: str | None,
) -> str:
    """Pick the proxy group to operate on."""
    if preferred and preferred in proxies:
        return preferred
    if preferred:
        raise RuntimeError(f"group not found: {preferred}")

    for name in (
        "Proxy",
        "GLOBAL",
        "🚀 节点选择",
        "节点选择",
        "国外流量",
    ):
        item = proxies.get(name)
        if isinstance(item, dict) and isinstance(item.get("all"), list):
            return name

    selectors = [
        n
        for n, item in proxies.items()
        if isinstance(item, dict)
        and item.get("type") in {"Selector", "URLTest", "Fallback", "LoadBalance"}
        and isinstance(item.get("all"), list)
    ]
    if not selectors:
        raise RuntimeError("no selector-like proxy group found")
    return selectors[0]


def _apply_switch(api: Api, group_name: str, best: Score) -> None:
    if best.candidate.parent_group:
        api.switch_proxy(best.candidate.parent_group, best.candidate.name)
        api.switch_proxy(group_name, best.candidate.parent_group)
    else:
        api.switch_proxy(group_name, best.candidate.name)


def switch_if_needed(
    api: Api,
    config: Config,
    state: SwitcherState,
) -> tuple[int | None, bool, str]:
    """One cycle: evaluate current node, scan, maybe switch.

    Returns (delay_ms, switched, switch_target_name).
    """
    proxies = api.load_proxies()
    group_name = choose_group(proxies, config.group)
    group = proxies[group_name]
    provider_members = api.load_provider_members()
    now = group.get("now")

    if not isinstance(now, str) or not now:
        raise RuntimeError(f"group has no current node: {group_name}")

    # measure current node
    current = proxy_score(api, now, config.test_urls, config.timeout_ms, 1)
    current_delay = current[0] if current else None
    current_maximum = current[1] if current else None
    current_unhealthy = current_delay is None or current_maximum is None
    current_provider = _current_provider_for_node(now, provider_members, config)
    now_ts_cycle = time.monotonic()

    if _provider_rank(current_provider, config) == 0:
        state.lower_provider_since = 0.0
    elif (
        state.current_provider != current_provider
        or state.lower_provider_since <= 0
    ):
        state.lower_provider_since = now_ts_cycle
    state.current_provider = current_provider

    if (
        not config.force_scan
        and state.post_switch_rounds > 0
        and current_delay is not None
        and current_maximum is not None
        and current_maximum <= config.hard_threshold
    ):
        state.degraded_count = 0
        return (current_maximum, False, "")

    # decide whether to scan
    trigger_scan = config.force_scan
    provider_regress_scan = False
    if current_delay is None or current_maximum is None:
        trigger_scan = True
    elif current_maximum > config.immediate_scan_threshold:
        state.degraded_count = 0
        trigger_scan = True
        current_unhealthy = True
    elif current_maximum > config.hard_threshold:
        state.degraded_count += 1
        trigger_scan = trigger_scan or state.degraded_count >= config.degraded_scan_count
        current_unhealthy = True
        if not trigger_scan:
            return (current_maximum, False, "")
    else:
        state.degraded_count = 0

    if not trigger_scan and _primary_provider_regress_due(
        current_provider, config, state,
    ):
        trigger_scan = True
        provider_regress_scan = True

    if not trigger_scan:
        return (current_maximum, False, "")

    if config.refresh_before_scan:
        api.refresh_providers()
        state.cache_invalidated_at = time.monotonic()
        proxies = api.load_proxies()
        group = proxies[group_name]
        provider_members = api.load_provider_members()

    candidates = collect_candidates(
        group_name, group, proxies, provider_members, config,
    )

    if provider_regress_scan:
        primary_candidates = _primary_provider_candidates(candidates, config)
        best, switched_region, cross_reason = scan_region_sticky_candidate(
            api, config, state, primary_candidates, now,
        )
        if best is None:
            state.lower_provider_since = time.monotonic()
            _log("INFO", "primary provider regress skipped: no healthy node")
            return (current_maximum, False, "")
        cross_reason = cross_reason or "primary provider recovered; switching back"
    else:
        # region-sticky health scan: preferred region is tested first; backups are
        # scanned only when the previous region has no healthy node.
        best, switched_region, cross_reason = scan_region_sticky_candidate(
            api, config, state, candidates, now,
        )
    if cross_reason:
        _log("WARN", cross_reason)

    if best is None and config.auto_refresh_on_failure:
        now_ts2 = time.monotonic()
        if now_ts2 - state.last_auto_refresh >= config.refresh_cooldown:
            state.last_auto_refresh = now_ts2
            api.refresh_providers()
            state.cache_invalidated_at = now_ts2
            proxies = api.load_proxies()
            group = proxies[group_name]
            provider_members = api.load_provider_members()
            candidates = collect_candidates(
                group_name, group, proxies, provider_members, config,
            )
            best, switched_region, cross_reason = scan_region_sticky_candidate(
                api, config, state, candidates, now,
            )
            if cross_reason:
                _log("WARN", cross_reason)

    if best is None:
        _log("WARN", "no safe candidate found after scan; keeping current node")
        return (current_maximum, False, "")

    if best.average > config.hard_threshold and not config.switch_to_best_even_if_slow:
        _log(
            "WARN",
            f"best candidate above hard threshold: {best.candidate.name}, "
            f"avg={best.average}ms, max={best.maximum}ms; keeping current node",
        )
        return (current_maximum, False, "")

    # cooldown check
    now_ts = time.monotonic()
    time_since_last = now_ts - state.last_switch_time
    time_since_region = now_ts - state.last_region_switch_time
    best_region = best.candidate.region if best.candidate.region else Region.OTHER

    # determine cross-region status for cooldown
    is_cross = state.current_region != Region.OTHER and state.current_region != best_region
    if is_cross and best_region != Region.OTHER:
        switched_region = True

    if not switched_region:
        if time_since_last < config.switch_cooldown_seconds and not current_unhealthy:
            wait = round(config.switch_cooldown_seconds - time_since_last)
            _log(
                "INFO",
                f"switch on cooldown: {wait}s remaining before next intra-region switch",
            )
            return (current_maximum, False, "")
        if current_unhealthy and time_since_last < config.switch_cooldown_seconds:
            _log(
                "WARN",
                "current node unhealthy; bypassing intra-region switch cooldown",
            )
    else:
        if (
            time_since_region < config.min_region_hold_seconds
            and not current_unhealthy
            and not provider_regress_scan
        ):
            wait = round(config.min_region_hold_seconds - time_since_region)
            _log(
                "WARN",
                f"cross-region switch on cooldown: {wait}s remaining "
                f"({state.current_region.value} -> {best_region.value})",
            )
            return (current_maximum, False, "")
        _log(
            "WARN",
            f"cross-region switch: {state.current_region.value} -> "
            f"{best_region.value}, target={best.candidate.name}, "
            f"avg={best.average}ms, max={best.maximum}ms",
        )

    # apply switch
    target_name = best.candidate.name
    if config.dry_run:
        _log(
            "INFO",
            f"[dry-run] would switch: {group_name} -> {target_name}, "
            f"avg={best.average}ms, max={best.maximum}ms",
        )
        return (best.average, False, target_name)

    _apply_switch(api, group_name, best)
    _log(
        "WARN" if current_unhealthy else "INFO",
        f"switched: {group_name} -> {target_name}, "
        f"avg={best.average}ms, max={best.maximum}ms",
    )
    state.last_switch_time = now_ts
    state.degraded_count = 0
    state.post_switch_rounds = 3
    state.current_provider = best.candidate.provider
    if _provider_rank(best.candidate.provider, config) == 0:
        state.lower_provider_since = 0.0
    else:
        state.lower_provider_since = now_ts

    # Always track current region after any successful switch so that
    # subsequent cycles know which region we're actually in (not just
    # after cross-region switches).
    if best_region != Region.OTHER:
        state.current_region = best_region

    if switched_region and best_region != Region.OTHER:
        state.last_region_switch_time = now_ts

    return (best.average, True, target_name)


# ---------------------------------------------------------------------------
# periodic helpers
# ---------------------------------------------------------------------------

def maybe_periodic_refresh(
    api: Api,
    config: Config,
    state: SwitcherState,
) -> None:
    if not state.direct_online:
        return
    if config.provider_refresh_interval <= 0:
        return
    now = time.monotonic()
    if state.last_periodic_refresh and (
        now - state.last_periodic_refresh < config.provider_refresh_interval
    ):
        return
    state.last_periodic_refresh = now
    api.refresh_providers()
    state.cache_invalidated_at = now
    _log("INFO", "periodic provider refresh")


def next_interval(config: Config, state: SwitcherState, delay: int | None) -> int:
    if state.post_switch_rounds > 0:
        state.post_switch_rounds -= 1
        return 3
    if delay is not None:
        if delay <= config.threshold:
            return 15
        if config.threshold < delay <= config.hard_threshold:
            return 5
    return max(1, config.interval)
