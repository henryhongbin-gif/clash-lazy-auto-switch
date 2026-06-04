#!/usr/bin/env python3
"""
Auto-switch Clash/Mihomo selector node when the current node is too slow.

Works with Clash Verge / Clash Verge Rev when "External Controller" is enabled.
No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_TEST_URLS = (
    "https://www.google.com/generate_204",
    "https://www.youtube.com/generate_204",
    "https://chatgpt.com/cdn-cgi/trace",
    "https://claude.ai/",
)
DEFAULT_DIRECT_CHECK_URL = "http://captive.apple.com/hotspot-detect.html"
SPECIAL_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
DEFAULT_EXCLUDE_KEYWORDS = (
    "香港",
    "港",
    "HK",
    "Hong Kong",
    "HongKong",
    "HKG",
    "剩余流量",
    "套餐到期",
    "距离下次重置",
)
DEFAULT_PROVIDER_PRIORITY = ("ProviderA", "ProviderB", "ProviderC")
OTHER_PROVIDER = "其他"
SCORE_CACHE_TTL = 300


@dataclass
class Api:
    base: str
    secret: str | None
    timeout: float
    unix_socket: str | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if self.unix_socket:
            payload = self.request_unix(method, path, headers, data)
        else:
            url = self.base.rstrip("/") + path
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def request_unix(self, method: str, path: str, headers: dict[str, str], data: bytes | None) -> bytes:
        class UnixHTTPConnection(http.client.HTTPConnection):
            def __init__(self, socket_path: str, timeout: float) -> None:
                super().__init__("localhost", timeout=timeout)
                self.socket_path = socket_path

            def connect(self) -> None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                self.sock = sock

        conn = UnixHTTPConnection(self.unix_socket, self.timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            if resp.status >= 400:
                detail = payload.decode("utf-8", errors="replace")
                raise RuntimeError(f"{method} {path} failed: HTTP {resp.status} {detail}")
            return payload
        finally:
            conn.close()

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, body)


@dataclass(frozen=True)
class Candidate:
    name: str
    provider: str
    parent_group: str | None = None


@dataclass(frozen=True)
class Score:
    candidate: Candidate
    average: int
    maximum: int
    samples: tuple[int, ...]


@dataclass(frozen=True)
class CycleResult:
    delay: int | None = None
    switched: bool = False


def q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), message, flush=True)


def proxy_delay(api: Api, name: str, test_url: str, timeout_ms: int, timeout_penalty_ms: int | None = None) -> int | None:
    params = urllib.parse.urlencode({"url": test_url, "timeout": timeout_ms})
    try:
        data = api.get(f"/proxies/{q(name)}/delay?{params}")
    except Exception as exc:
        if timeout_penalty_ms is not None and ("HTTP 504" in str(exc) or "Timeout" in str(exc)):
            log(f"delay timeout: {name}: {exc}; counted as {timeout_penalty_ms}ms")
            return timeout_penalty_ms
        log(f"delay failed: {name}: {exc}")
        return None
    delay = data.get("delay")
    if isinstance(delay, int) and delay >= 0:
        return delay
    return None


def proxy_score(api: Api, name: str, test_urls: list[str], timeout_ms: int, samples: int) -> tuple[int, int, tuple[int, ...]] | None:
    delays: list[int] = []
    for index in range(max(1, samples)):
        # 改动4 · 四站测试并行化：同一轮内的四个测试 URL 同时发出，全部返回后再统一计算。
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(test_urls) or 1) as executor:
            futures = [
                executor.submit(proxy_delay, api, name, test_url, timeout_ms, 1000)
                for test_url in test_urls
            ]
            round_delays: list[int] = []
            for future in futures:
                delay = future.result()
                if delay is None:
                    return None
                round_delays.append(delay)
            if round_delays and all(delay == 1000 for delay in round_delays):
                return None
            delays.extend(round_delays)
        if index + 1 < samples:
            time.sleep(0.2)
    average = round(sum(delays) / len(delays))
    return average, max(delays), tuple(delays)


def direct_network_online(args: argparse.Namespace) -> bool:
    req = urllib.request.Request(args.direct_check_url, headers={"User-Agent": "clash-auto-switch/1.0"})
    handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=args.direct_check_timeout) as resp:
            resp.read(128)
            return 200 <= resp.status < 500
    except Exception as exc:
        log(f"direct network offline: {exc}")
        return False


def load_proxies(api: Api) -> dict[str, Any]:
    data = api.get("/proxies")
    proxies = data.get("proxies")
    if not isinstance(proxies, dict):
        raise RuntimeError("unexpected /proxies response")
    return proxies


def load_provider_members(api: Api) -> dict[str, set[str]]:
    try:
        data = api.get("/providers/proxies")
    except Exception as exc:
        log(f"provider list unavailable: {exc}")
        return {}
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}

    members: dict[str, set[str]] = {}
    for provider_name, provider in providers.items():
        if not isinstance(provider_name, str) or not isinstance(provider, dict):
            continue
        proxy_items = provider.get("proxies") or []
        names: set[str] = set()
        for item in proxy_items:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
            elif isinstance(item, str):
                names.add(item)
        members[provider_name] = names
    return members


def choose_group(proxies: dict[str, Any], preferred: str | None) -> str:
    if preferred and preferred in proxies:
        return preferred
    if preferred:
        raise RuntimeError(f"group not found: {preferred}")

    for name in ("Proxy", "GLOBAL", "🚀 节点选择", "节点选择", "国外流量"):
        item = proxies.get(name)
        if isinstance(item, dict) and isinstance(item.get("all"), list):
            return name

    selectors = [
        name
        for name, item in proxies.items()
        if isinstance(item, dict) and item.get("type") in {"Selector", "URLTest", "Fallback", "LoadBalance"}
        and isinstance(item.get("all"), list)
    ]
    if not selectors:
        raise RuntimeError("no selector-like proxy group found")
    return selectors[0]


def is_excluded(name: str, keywords: list[str]) -> bool:
    lowered = name.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def provider_for_node(name: str, provider_members: dict[str, set[str]], priority: list[str]) -> str:
    for provider in priority:
        if name in provider_members.get(provider, set()):
            return provider
    lowered = name.lower()
    for provider in priority:
        if provider.lower() in lowered:
            return provider
    return "其他"


def candidate_from_name(
    name: str,
    proxies: dict[str, Any],
    provider_members: dict[str, set[str]],
    provider_priority: list[str],
    exclude_keywords: list[str],
) -> Candidate | None:
    if not isinstance(name, str) or name in SPECIAL_NAMES:
        return None
    if is_excluded(name, exclude_keywords):
        log(f"excluded: {name}")
        return None
    item = proxies.get(name)
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("all"), list):
        return None
    return Candidate(name=name, provider=provider_for_node(name, provider_members, provider_priority))


def collect_candidates(
    group_name: str,
    group: dict[str, Any],
    proxies: dict[str, Any],
    provider_members: dict[str, set[str]],
    provider_priority: list[str],
    exclude_keywords: list[str],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for name in group.get("all") or []:
        direct = candidate_from_name(name, proxies, provider_members, provider_priority, exclude_keywords)
        if direct:
            if direct.name not in seen:
                candidates.append(direct)
                seen.add(direct.name)
            continue

        nested_group = proxies.get(name)
        if not isinstance(name, str) or not isinstance(nested_group, dict) or not isinstance(nested_group.get("all"), list):
            continue

        provider = name if name in provider_priority else provider_for_node(name, provider_members, provider_priority)
        for nested_name in nested_group.get("all") or []:
            nested = candidate_from_name(nested_name, proxies, provider_members, provider_priority, exclude_keywords)
            if not nested:
                continue
            nested = Candidate(name=nested.name, provider=provider if provider != "其他" else nested.provider, parent_group=name)
            if nested.name not in seen:
                candidates.append(nested)
                seen.add(nested.name)
    if not candidates:
        log(f"no usable candidates in group: {group_name}")
    return candidates


def provider_rank(provider: str, priority: list[str]) -> int:
    try:
        return priority.index(provider)
    except ValueError:
        return len(priority)


def candidate_sort_key(candidate: Candidate) -> tuple[int, int, int, str]:
    name = candidate.name
    line_rank = 0 if "专线" in name else 1
    region_rank = 0 if "新加坡" in name or "🇸🇬" in name else 1
    media_rank = 0 if "流媒体" in name else 1
    return line_rank, region_rank, media_rank, name


def refresh_providers(api: Api) -> None:
    try:
        data = api.get("/providers/proxies")
    except Exception as exc:
        log(f"provider list unavailable: {exc}")
        return
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return
    for name in providers:
        try:
            api.put(f"/providers/proxies/{q(name)}")
            log(f"provider refreshed: {name}")
        except Exception as exc:
            log(f"provider refresh failed: {name}: {exc}")


def mark_cache_invalidated(args: argparse.Namespace) -> None:
    # 改动5 · 节点性能短缓存：订阅刚刚完成刷新后，旧缓存强制失效，下次扫描重新测试。
    setattr(args, "_score_cache_invalidated_at", time.monotonic())


def cached_score(args: argparse.Namespace, candidate: Candidate) -> Score | None:
    # 改动5 · 节点性能短缓存：5 分钟内且 <=300ms 的候选节点结果可复用，跳过重复四站测试。
    cache = getattr(args, "_score_cache", {})
    entry = cache.get(candidate.name)
    if not entry:
        return None
    tested_at, average, maximum = entry
    now = time.monotonic()
    if tested_at < getattr(args, "_score_cache_invalidated_at", 0.0):
        return None
    if now - tested_at > SCORE_CACHE_TTL:
        return None
    if average > args.hard_threshold:
        return None
    log(f"cache hit: {candidate.provider} / {candidate.name}, avg {average}ms, max {maximum}ms")
    return Score(candidate=candidate, average=average, maximum=maximum, samples=(average, maximum))


def remember_score(args: argparse.Namespace, name: str, average: int, maximum: int) -> None:
    # 改动5 · 节点性能短缓存：扫描得到的四站平均延迟和最大延迟写入短缓存。
    cache = getattr(args, "_score_cache", None)
    if cache is None:
        cache = {}
        setattr(args, "_score_cache", cache)
    cache[name] = (time.monotonic(), average, maximum)


def scan_best_candidate(
    api: Api,
    args: argparse.Namespace,
    candidates: list[Candidate],
    now: str,
    allowed_providers: list[str],
) -> Score | None:
    for provider in allowed_providers:
        provider_candidates = sorted(
            [candidate for candidate in candidates if candidate.provider == provider],
            key=candidate_sort_key,
        )
        if not provider_candidates:
            continue
        log(f"checking provider: {provider}")
        provider_scores: list[Score] = []
        for candidate in provider_candidates:
            if candidate.name == now:
                continue
            cached = cached_score(args, candidate)
            if cached:
                provider_scores.append(cached)
                if args.fast_recovery and cached.average <= args.threshold and cached.maximum <= args.ideal_max_delay:
                    log("fast recovery: cached ideal node found")
                    return cached
                continue
            # 改动3 · 候选节点预筛层：完整四站测试前，先用 Google 1.5s 快速预筛。
            prefilter_delay = proxy_delay(api, candidate.name, args.prefilter_url, args.prefilter_timeout_ms)
            if prefilter_delay is None:
                log(f"prefilter failed: {candidate.provider} / {candidate.name}")
                continue
            score = proxy_score(api, candidate.name, args.test_url, args.timeout_ms, args.stable_samples)
            if score is None:
                log(f"unstable: {candidate.provider} / {candidate.name}")
                continue
            average, maximum, samples = score
            item = Score(candidate=candidate, average=average, maximum=maximum, samples=samples)
            remember_score(args, candidate.name, average, maximum)
            provider_scores.append(item)
            log(f"candidate: {candidate.provider} / {candidate.name}, avg {average}ms, max {maximum}ms, samples {samples}")
            if args.fast_recovery and average <= args.threshold and maximum <= args.ideal_max_delay:
                log("fast recovery: first ideal node found")
                return item

        provider_scores.sort(key=lambda item: (item.average, item.maximum, item.candidate.name))
        provider_ideal = [
            item
            for item in provider_scores
            if item.average <= args.threshold and item.maximum <= args.ideal_max_delay
        ]
        if provider_ideal:
            return provider_ideal[0]

        provider_safe = [
            item
            for item in provider_scores
            if item.average <= args.hard_threshold and item.maximum <= args.safe_max_delay
        ]
        if provider_safe:
            log(f"fallback: no <= {args.threshold}ms node in {provider}; using <= {args.hard_threshold}ms safe node")
            return provider_safe[0]
        if args.switch_to_best_even_if_slow and provider_scores:
            return provider_scores[0]
    return None


def apply_switch(api: Api, group_name: str, best: Score) -> None:
    if best.candidate.parent_group:
        api.put(f"/proxies/{q(best.candidate.parent_group)}", {"name": best.candidate.name})
        api.put(f"/proxies/{q(group_name)}", {"name": best.candidate.parent_group})
        log(
            f"switched: {best.candidate.parent_group} -> {best.candidate.name}, "
            f"{group_name} -> {best.candidate.parent_group}, avg {best.average}ms"
        )
    else:
        api.put(f"/proxies/{q(group_name)}", {"name": best.candidate.name})
        log(f"switched: {group_name} -> {best.candidate.name}, avg {best.average}ms")


def regression_to_provider_a(api: Api, args: argparse.Namespace) -> None:
    # 改动6 · 订阅刷新后主动回归ProviderA：刷新完成后只扫描ProviderA，找到平均 <=200ms 且最大 <=300ms 即切回。
    try:
        proxies = load_proxies(api)
        group_name = choose_group(proxies, args.group)
        group = proxies[group_name]
        provider_members = load_provider_members(api)
        candidates = collect_candidates(
            group_name,
            group,
            proxies,
            provider_members,
            args.provider_priority,
            args.exclude_keyword,
        )
        provider_a_candidates = sorted(
            [candidate for candidate in candidates if candidate.provider == "ProviderA"],
            key=candidate_sort_key,
        )
        if not provider_a_candidates:
            log("provider_a regression: no candidates")
            return
        for candidate in provider_a_candidates:
            score = proxy_score(api, candidate.name, args.test_url, args.timeout_ms, args.stable_samples)
            if score is None:
                log(f"provider_a regression unstable: {candidate.name}")
                continue
            average, maximum, samples = score
            remember_score(args, candidate.name, average, maximum)
            log(f"provider_a regression candidate: {candidate.name}, avg {average}ms, max {maximum}ms, samples {samples}")
            if average <= args.threshold and maximum <= args.ideal_max_delay:
                apply_switch(api, group_name, Score(candidate=candidate, average=average, maximum=maximum, samples=samples))
                return
        log("provider_a regression: no ideal node found")
    except Exception as exc:
        log(f"provider_a regression failed: {exc}")


def maybe_regress_after_lower_provider_stability(api: Api, args: argparse.Namespace, current_provider: str) -> None:
    if current_provider not in {"ProviderB", "ProviderC"}:
        setattr(args, "_lower_provider_stable_since", None)
        return
    now = time.monotonic()
    stable_since = getattr(args, "_lower_provider_stable_since", None)
    if stable_since is None:
        setattr(args, "_lower_provider_stable_since", now)
        return
    if now - stable_since >= args.lower_provider_regress_after:
        log("lower provider stable for 10 minutes; probing provider_a regression")
        regression_to_provider_a(api, args)
        setattr(args, "_lower_provider_stable_since", now)


def refresh_providers_and_regress(api: Api, args: argparse.Namespace) -> None:
    # 改动6 · 订阅刷新后主动回归ProviderA：复用既有刷新动作，不新增计时器。
    refresh_providers(api)
    mark_cache_invalidated(args)
    regression_to_provider_a(api, args)


def maybe_auto_refresh(api: Api, args: argparse.Namespace) -> bool:
    if not args.auto_refresh_on_failure:
        return False
    now = time.monotonic()
    last_refresh = getattr(args, "_last_auto_refresh", 0.0)
    if now - last_refresh < args.refresh_cooldown:
        wait = round(args.refresh_cooldown - (now - last_refresh))
        log(f"auto refresh skipped: cooldown {wait}s")
        return False
    setattr(args, "_last_auto_refresh", now)
    log("auto refresh: no working node found; refreshing proxy providers")
    refresh_providers_and_regress(api, args)
    return True


def maybe_periodic_refresh(api: Api, args: argparse.Namespace) -> None:
    if not getattr(args, "_direct_online", True):
        log("periodic refresh skipped: direct network offline")
        return
    if args.provider_refresh_interval <= 0:
        return
    now = time.monotonic()
    last_refresh = getattr(args, "_last_periodic_refresh", 0.0)
    if last_refresh and now - last_refresh < args.provider_refresh_interval:
        return
    setattr(args, "_last_periodic_refresh", now)
    log(f"periodic refresh: refreshing proxy providers every {args.provider_refresh_interval}s")
    refresh_providers_and_regress(api, args)


def next_interval(args: argparse.Namespace, result: CycleResult | None) -> int:
    # 改动1 · 自适应轮询间隔：按平均延迟判断；切换后前 3 轮使用 3s；<=200ms 使用 15s；200-300ms 使用 5s。
    if result and result.switched:
        setattr(args, "_post_switch_rounds", 3)
    post_switch_rounds = getattr(args, "_post_switch_rounds", 0)
    if post_switch_rounds > 0:
        setattr(args, "_post_switch_rounds", post_switch_rounds - 1)
        return 3
    if result and result.delay is not None:
        if result.delay <= args.threshold:
            return 15
        if args.threshold < result.delay <= args.hard_threshold:
            return 5
    return max(1, args.interval)


def switch_if_needed(api: Api, args: argparse.Namespace) -> CycleResult:
    proxies = load_proxies(api)
    group_name = choose_group(proxies, args.group)
    group = proxies[group_name]
    provider_members = load_provider_members(api)
    now = group.get("now")
    if not isinstance(now, str) or not now:
        raise RuntimeError(f"group has no current node: {group_name}")

    current_score = proxy_score(api, now, args.test_url, args.timeout_ms, 1)
    current_delay = current_score[0] if current_score else None
    current_maximum = current_score[1] if current_score else None
    current_provider = provider_for_node(now, provider_members, args.provider_priority)
    # 改动2 · 分级触发扫描阈值：失败或最大延迟 >500ms 立即扫描；最大延迟 300-500ms 连续 2 次才扫描；<=300ms 重置计数。
    trigger_scan = args.force_scan
    if current_delay is None or current_maximum is None:
        trigger_scan = True
    elif current_maximum > args.immediate_scan_threshold:
        setattr(args, "_degraded_count", 0)
        trigger_scan = True
    elif current_maximum > args.hard_threshold:
        degraded_count = getattr(args, "_degraded_count", 0) + 1
        setattr(args, "_degraded_count", degraded_count)
        trigger_scan = trigger_scan or degraded_count >= args.degraded_scan_count
        if not trigger_scan:
            log(
                f"degraded: {group_name} -> {now}, avg {current_delay}ms, max {current_maximum}ms, "
                f"count {degraded_count}/{args.degraded_scan_count}"
            )
            return CycleResult(delay=current_maximum)
    else:
        setattr(args, "_degraded_count", 0)

    if not trigger_scan:
        log(f"ok: {group_name} -> {now}, avg {current_delay}ms, max {current_maximum}ms")
        maybe_regress_after_lower_provider_stability(api, args, current_provider)
        return CycleResult(delay=current_maximum)

    reason = "timeout/error" if current_delay is None else f"avg {current_delay}ms, max {current_maximum}ms"
    if args.force_scan:
        log(f"force scan: {group_name} -> {now}, {reason}; scanning candidates")
    else:
        log(f"slow: {group_name} -> {now}, {reason}; scanning candidates")

    if args.refresh_before_scan:
        refresh_providers_and_regress(api, args)
        proxies = load_proxies(api)
        group = proxies[group_name]
        provider_members = load_provider_members(api)

    candidates = collect_candidates(
        group_name,
        group,
        proxies,
        provider_members,
        args.provider_priority,
        args.exclude_keyword,
    )

    allowed_providers = args.provider_priority + [OTHER_PROVIDER]

    best = scan_best_candidate(api, args, candidates, now, allowed_providers)
    if best is None and maybe_auto_refresh(api, args):
        proxies = load_proxies(api)
        group = proxies[group_name]
        provider_members = load_provider_members(api)
        candidates = collect_candidates(
            group_name,
            group,
            proxies,
            provider_members,
            args.provider_priority,
            args.exclude_keyword,
        )
        best = scan_best_candidate(api, args, candidates, now, allowed_providers)

    if best is None:
        log("no measurable candidate found")
        return CycleResult(delay=current_maximum)

    if best.average > args.hard_threshold and not args.switch_to_best_even_if_slow:
        log(f"best is still above hard threshold: {best.candidate.name}, {best.average}ms; keeping current")
        return CycleResult(delay=current_maximum)

    apply_switch(api, group_name, best)
    # 改动2 · 分级触发扫描阈值：每次切换节点后重置连续计数器。
    setattr(args, "_degraded_count", 0)
    return CycleResult(delay=best.average, switched=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-switch Clash/Mihomo node by delay threshold.")
    parser.add_argument("--api", default=os.getenv("CLASH_API", "http://127.0.0.1:9097"))
    parser.add_argument("--socket", default=os.getenv("CLASH_SOCKET") or None)
    parser.add_argument("--secret", default=os.getenv("CLASH_SECRET") or None)
    parser.add_argument("--group", default=os.getenv("CLASH_GROUP") or None)
    parser.add_argument("--threshold", type=int, default=int(os.getenv("CLASH_THRESHOLD", "200")))
    parser.add_argument("--hard-threshold", type=int, default=int(os.getenv("CLASH_HARD_THRESHOLD", "300")))
    parser.add_argument("--immediate-scan-threshold", type=int, default=int(os.getenv("CLASH_IMMEDIATE_SCAN_THRESHOLD", "500")))
    parser.add_argument("--degraded-scan-count", type=int, default=int(os.getenv("CLASH_DEGRADED_SCAN_COUNT", "2")))
    parser.add_argument("--interval", type=int, default=int(os.getenv("CLASH_INTERVAL", "3")))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("CLASH_TIMEOUT_MS", "800")))
    parser.add_argument("--direct-check-url", default=os.getenv("CLASH_DIRECT_CHECK_URL", DEFAULT_DIRECT_CHECK_URL))
    parser.add_argument(
        "--direct-check-timeout",
        type=float,
        default=float(os.getenv("CLASH_DIRECT_CHECK_TIMEOUT", "2")),
    )
    parser.add_argument("--stable-samples", type=int, default=int(os.getenv("CLASH_STABLE_SAMPLES", "3")))
    parser.add_argument("--ideal-max-delay", type=int, default=int(os.getenv("CLASH_IDEAL_MAX_DELAY", "300")))
    parser.add_argument("--safe-max-delay", type=int, default=int(os.getenv("CLASH_SAFE_MAX_DELAY", "400")))
    parser.add_argument("--lower-provider-regress-after", type=int, default=int(os.getenv("CLASH_LOWER_PROVIDER_REGRESS_AFTER", "600")))
    parser.add_argument("--prefilter-url", default=os.getenv("CLASH_PREFILTER_URL", "https://www.google.com/generate_204"))
    parser.add_argument("--prefilter-timeout-ms", type=int, default=int(os.getenv("CLASH_PREFILTER_TIMEOUT_MS", "1500")))
    parser.add_argument(
        "--test-url",
        action="append",
        default=list(DEFAULT_TEST_URLS),
        help="site used for delay testing; can be used multiple times",
    )
    parser.add_argument(
        "--url",
        dest="legacy_url",
        default=os.getenv("CLASH_TEST_URL") or None,
        help="deprecated alias; if set, replaces the default test URL list with one URL",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force-scan", action="store_true", help="scan and switch even when the current node is healthy")
    parser.add_argument(
        "--exclude-keyword",
        action="append",
        default=list(DEFAULT_EXCLUDE_KEYWORDS),
        help="skip nodes whose names contain this keyword; can be used multiple times",
    )
    parser.add_argument(
        "--provider-priority",
        action="append",
        default=list(DEFAULT_PROVIDER_PRIORITY),
        help="subscription/provider priority; default: ProviderA, ProviderB, ProviderC",
    )
    parser.add_argument("--refresh-before-scan", action="store_true", help="refresh proxy providers before scanning")
    parser.add_argument(
        "--auto-refresh-on-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="refresh proxy providers automatically when no working node is found",
    )
    parser.add_argument("--refresh-cooldown", type=int, default=int(os.getenv("CLASH_REFRESH_COOLDOWN", "60")))
    parser.add_argument(
        "--provider-refresh-interval",
        type=int,
        default=int(os.getenv("CLASH_PROVIDER_REFRESH_INTERVAL", "1800")),
        help="refresh proxy providers periodically; 0 disables periodic refresh",
    )
    parser.add_argument(
        "--fast-recovery",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="switch as soon as the first acceptable node is found instead of scanning the whole provider",
    )
    parser.add_argument(
        "--switch-to-best-even-if-slow",
        action="store_true",
        help="switch to the fastest candidate even when all candidates are above threshold",
    )
    args = parser.parse_args()
    if args.legacy_url:
        args.test_url = [args.legacy_url]
    return args


def main() -> int:
    args = parse_args()
    if not args.socket and os.path.exists("/tmp/verge/verge-mihomo.sock"):
        args.socket = "/tmp/verge/verge-mihomo.sock"
    api = Api(args.api, args.secret, max(2.0, args.timeout_ms / 1000 + 1), args.socket)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopping:
        result: CycleResult | None = None
        try:
            online = direct_network_online(args)
            was_online = getattr(args, "_direct_online", True)
            setattr(args, "_direct_online", online)
            if not online:
                setattr(args, "_needs_refresh_after_reconnect", True)
                if was_online:
                    log("direct network went offline; waiting for hotspot/network reconnect")
            else:
                if not was_online:
                    log("direct network restored; refreshing providers before recovery scan")
                if getattr(args, "_needs_refresh_after_reconnect", False):
                    refresh_providers_and_regress(api, args)
                    setattr(args, "_needs_refresh_after_reconnect", False)
                    setattr(args, "_last_auto_refresh", time.monotonic())
                    setattr(args, "_last_periodic_refresh", time.monotonic())
            maybe_periodic_refresh(api, args)
            if online:
                result = switch_if_needed(api, args)
        except Exception as exc:
            log(f"error: {exc}")
        if args.once:
            break
        wait_seconds = next_interval(args, result)
        for _ in range(wait_seconds):
            if stopping:
                break
            time.sleep(1)
    log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
