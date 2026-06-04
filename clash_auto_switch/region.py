"""
Region detection from proxy node names.

Recognises Chinese / English / emoji-flag patterns for common regions.
"""

from __future__ import annotations

import re
from enum import Enum


class Region(Enum):
    SINGAPORE = "新加坡"
    JAPAN = "日本"
    HONGKONG = "香港"
    TAIWAN = "台湾"
    USA = "美国"
    OTHER = "其他"


# Keywords that must match as whole words (ASCII-only, short abbreviations).
# Using word-boundary regex to prevent "US" matching "Russia", "SG" matching
# "MSG", "TW" matching "network", etc.
_WORD_KEYWORDS: set[str] = {
    "SG", "SGP", "SIN",
    "JP", "JPN",
    "HK", "HKG",
    "TW", "TWN",
    "US", "USA",
}

_INDICATORS: dict[Region, list[str]] = {
    Region.SINGAPORE: [
        "新加坡", "狮城", "🇸🇬", "SG", "Singapore", "SGP", "singap", "SIN",
    ],
    Region.JAPAN: [
        "日本", "东京", "大阪", "🇯🇵", "JP", "Japan", "JPN",
        "tokyo", "Tokyo",
    ],
    Region.HONGKONG: [
        "香港", "🇭🇰", "HK", "Hong Kong", "HongKong", "hongkong", "HKG",
    ],
    Region.TAIWAN: [
        "台湾", "台北", "🇹🇼", "TW", "Taiwan", "TWN",
        "Taipei", "taipei",
    ],
    Region.USA: [
        "美国", "洛杉矶", "纽约", "硅谷", "🇺🇸", "US", "USA", "America",
        "United States",
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
            kw_upper = kw.upper()
            if kw_upper in _WORD_KEYWORDS:
                # Short ASCII abbreviation: case-sensitive + word boundary.
                # Providers always write these in uppercase (TW, SG, US …),
                # so case-sensitive matching prevents "network-tw" → Taiwan
                # while still catching "TW-IPLC-01" correctly.
                if re.search(r'\b' + re.escape(kw_upper) + r'\b', name):
                    _CACHE[name] = region
                    return region
            else:
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
