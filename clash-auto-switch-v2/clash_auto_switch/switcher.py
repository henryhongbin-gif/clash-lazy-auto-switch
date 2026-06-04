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
    last_periodic_refresh: float = 0.0
    direct_online: bool = True
    needs_refresh_after_reconnect: bool = False


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


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

def scan_best_candidate(
    api: Api,
    config: Config,
    state: SwitcherState,
    candidates: list[Candidate],
    now_node: str,
    allowed_regions: set[Region] | None,
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
        for candidate in provider_candidates:
            if candidate.name == now_node:
                continue

            cached = cached_score(
                state.score_cache,
                candidate,
                state.cache_invalidated_at,
                config.hard_threshold,
            )
            if cached:
                provider_scores.append(cached)
                if (
                    config.fast_recovery
                    and cached.average <= config.threshold
                    and cached.maximum <= config.ideal_max_delay
                ):
                    return cached
                continue

            prefilter = api.proxy_delay(
                candidate.name,
                config.prefilter_url,
                config.prefilter_timeout_ms,
            )
            if prefilter is None:
                continue

            score = proxy_score(
                api,
                candidate.name,
                config.test_urls,
                config.timeout_ms,
                config.stable_samples,
            )
            if score is None:
                continue

            average, maximum, samples = score
            item = Score(
                candidate=candidate,
                average=average,
                maximum=maximum,
                samples=samples,
            )
            remember_score(state.score_cache, candidate.name, average, maximum)
            provider_scores.append(item)

            if (
                config.fast_recovery
                and average <= config.threshold
                and maximum <= config.ideal_max_delay
            ):
                return item

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

        if config.switch_to_best_even_if_slow and provider_scores:
            return provider_scores[0]

    return None


# ---------------------------------------------------------------------------
# region-sticky logic
# ---------------------------------------------------------------------------

def _determine_allowed_regions(
    config: Config,
    state: SwitcherState,
    candidates: list[Candidate],
) -> tuple[set[Region] | None, bool, str]:
    """Return (allowed_regions, is_cross_region, cross_reason).

    allowed_regions = None means "no filter, consider all candidates".
    """
    if not config.region_sticky or not candidates:
        return (None, False, "")

    preferred_region = get_region(config.preferred_region)
    backup_region_enums: list[Region] = []
    for r in config.backup_regions:
        breg = get_region(r)
        if breg != preferred_region and breg not in backup_region_enums:
            backup_region_enums.append(breg)

    now_ts = time.monotonic()
    can_cross = (
        now_ts - state.last_region_switch_time
        >= config.min_region_hold_seconds
    )

    # does the preferred region have candidates?
    pref_has = any(c.region == preferred_region for c in candidates)

    if pref_has:
        # preferred region is alive - always use it
        is_cross = (
            state.current_region != Region.OTHER
            and state.current_region != preferred_region
        )
        reason = ""
        if is_cross:
            if not can_cross:
                # still locked into current backup - stay there
                return ({state.current_region}, False, "")
            reason = (
                f"cross-region: switching back to preferred region "
                f"({state.current_region.value} -> {preferred_region.value})"
            )
        return ({preferred_region}, is_cross and can_cross, reason)

    # preferred region has no candidates - fallback
    if state.current_region == Region.OTHER or state.current_region == preferred_region:
        # we were in preferred, now it's dead; try backups
        for breg in backup_region_enums:
            if any(c.region == breg for c in candidates):
                if not can_cross and state.current_region == preferred_region:
                    # still locked into preferred region (even though dead)
                    _log(
                        "WARN",
                        f"preferred region dead but cross-region on cooldown "
                        f"({round(config.min_region_hold_seconds - (now_ts - state.last_region_switch_time))}s left)",
                    )
                    return ({preferred_region}, False, "")
                reason = (
                    f"cross-region: preferred region dead "
                    f"({preferred_region.value} -> {breg.value})"
                )
                return ({breg}, True, reason)
        # no backup has candidates either - scan all
        return (None, True, "cross-region: preferred and all backups dead, scanning all")

    # we are in a backup region
    if state.current_region in backup_region_enums:
        if any(c.region == state.current_region for c in candidates):
            # current backup still has nodes - stay
            return ({state.current_region}, False, "")
        # current backup dead - try next backup
        for breg in backup_region_enums:
            if any(c.region == breg for c in candidates):
                if not can_cross:
                    _log(
                        "WARN",
                        f"current backup region {state.current_region.value} dead "
                        f"but cross-region on cooldown",
                    )
                    return ({state.current_region}, False, "")
                reason = (
                    f"cross-region: current backup dead "
                    f"({state.current_region.value} -> {breg.value})"
                )
                return ({breg}, True, reason)
        return (None, True, "cross-region: all backup regions dead, scanning all")

    return (None, False, "")


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

    # decide whether to scan
    trigger_scan = config.force_scan
    if current_delay is None or current_maximum is None:
        trigger_scan = True
    elif current_maximum > config.immediate_scan_threshold:
        state.degraded_count = 0
        trigger_scan = True
    elif current_maximum > config.hard_threshold:
        state.degraded_count += 1
        trigger_scan = trigger_scan or state.degraded_count >= config.degraded_scan_count
        if not trigger_scan:
            return (current_maximum, False, "")
    else:
        state.degraded_count = 0

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

    # region-sticky filtering
    allowed_regions, switched_region, cross_reason = _determine_allowed_regions(
        config, state, candidates,
    )
    if cross_reason:
        _log("WARN", cross_reason)

    # scan
    best = scan_best_candidate(
        api, config, state, candidates, now, allowed_regions,
    )

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
            best = scan_best_candidate(
                api, config, state, candidates, now, allowed_regions,
            )

    if best is None:
        return (current_maximum, False, "")

    if best.average > config.hard_threshold and not config.switch_to_best_even_if_slow:
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
        if time_since_last < config.switch_cooldown_seconds:
            wait = round(config.switch_cooldown_seconds - time_since_last)
            _log(
                "INFO",
                f"switch on cooldown: {wait}s remaining before next intra-region switch",
            )
            return (current_maximum, False, "")
    else:
        if time_since_region < config.min_region_hold_seconds:
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
    state.last_switch_time = now_ts
    state.degraded_count = 0
    state.post_switch_rounds = 3

    if switched_region and best_region != Region.OTHER:
        state.last_region_switch_time = now_ts
        state.current_region = best_region

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
