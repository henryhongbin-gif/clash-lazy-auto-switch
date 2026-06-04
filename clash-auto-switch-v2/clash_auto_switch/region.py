"""
Region detection from proxy node names.

Recognises Chinese / English / emoji-flag patterns for common regions.
"""

from __future__ import annotations

from enum import Enum


class Region(Enum):
    SINGAPORE = "新加坡"
    JAPAN = "日本"
    HONGKONG = "香港"
    TAIWAN = "台湾"
    USA = "美国"
    OTHER = "其他"


_INDICATORS: dict[Region, list[str]] = {
    Region.SINGAPORE: [
        "新加坡", "狮城", "🇸🇬", "SG", "Singapore", "singapore", "SGP", "sgp",
        "singap", "SIN", "sin",
    ],
    Region.JAPAN: [
        "日本", "东京", "大阪", "🇯🇵", "JP", "Japan", "japan", "JPN", "jpn",
        "tokyo", "Tokyo",
    ],
    Region.HONGKONG: [
        "香港", "🇭🇰", "HK", "Hong Kong", "HongKong", "hongkong", "HKG", "hkg",
    ],
    Region.TAIWAN: [
        "台湾", "台北", "🇹🇼", "TW", "Taiwan", "taiwan", "TWN", "twn",
        "Taipei", "taipei",
    ],
    Region.USA: [
        "美国", "洛杉矶", "纽约", "硅谷", "🇺🇸", "US", "USA", "America",
        "america", "United States", "united states",
    ],
}


_CACHE: dict[str, Region] = {}


def get_region(name: str) -> Region:
    """Return the Region for a proxy node name.

    The detection is case-insensitive and checks for both Chinese and
    English/emoji indicators.  Results are cached for performance.
    """
    if not isinstance(name, str):
        return Region.OTHER
    if name in _CACHE:
        return _CACHE[name]

    lowered = name.lower()
    for region, keywords in _INDICATORS.items():
        for kw in keywords:
            # Guard: an empty keyword would always match (Python quirk).
            if not kw:
                continue
            if kw.lower() in lowered:
                _CACHE[name] = region
                return region

    _CACHE[name] = Region.OTHER
    return Region.OTHER


def is_excluded(name: str, keywords: list[str]) -> bool:
    """Return True when *name* contains any of the given *keywords*."""
    if not isinstance(name, str):
        return True
    lowered = name.lower()
    for kw in keywords:
        if not kw:
            continue
        if kw.lower() in lowered:
            return True
    return False


def region_rank(name: str, preferred: str, backups: list[str]) -> int:
    """Return an integer rank for sorting (lower = better).

    preferred region  → 0
    1st backup        → 1
    2nd backup        → 2
    ...
    OTHER             → len(backups) + 1
    """
    region = get_region(name)
    region_label = region.value

    if region_label == preferred:
        return 0
    try:
        return backups.index(region_label) + 1
    except ValueError:
        return len(backups) + 1
