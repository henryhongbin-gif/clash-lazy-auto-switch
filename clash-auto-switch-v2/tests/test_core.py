"""Minimal test cases for clash-auto-switch."""

from __future__ import annotations

from clash_auto_switch.region import Region, get_region, is_excluded, region_rank
from clash_auto_switch.healthcheck import Candidate
from clash_auto_switch.switcher import candidate_sort_key
from clash_auto_switch.config import Config


# ---------------------------------------------------------------------------
# 1. Region identification
# ---------------------------------------------------------------------------

def test_region_singapore() -> None:
    assert get_region("新加坡-01") == Region.SINGAPORE
    assert get_region("SG-优化") == Region.SINGAPORE
    assert get_region("🇸🇬 狮城专线") == Region.SINGAPORE
    assert get_region("Singapore-3x") == Region.SINGAPORE


def test_region_japan() -> None:
    assert get_region("日本-东京") == Region.JAPAN
    assert get_region("JP-02") == Region.JAPAN
    assert get_region("🇯🇵 Tokyo") == Region.JAPAN


def test_region_hongkong() -> None:
    assert get_region("香港-01") == Region.HONGKONG
    assert get_region("HK-03") == Region.HONGKONG


def test_region_taiwan() -> None:
    assert get_region("台湾-台北") == Region.TAIWAN
    assert get_region("TW-01") == Region.TAIWAN


def test_region_usa() -> None:
    assert get_region("美国-洛杉矶") == Region.USA
    assert get_region("US-01") == Region.USA
    assert get_region("🇺🇸 New York") == Region.USA


def test_region_other() -> None:
    assert get_region("random-node") == Region.OTHER
    assert get_region("") == Region.OTHER


def test_get_region_empty_string_not_always_true() -> None:
    """Verify that an empty string does NOT falsely match any region."""
    assert get_region("") == Region.OTHER


# ---------------------------------------------------------------------------
# 2. Exclusion
# ---------------------------------------------------------------------------

def test_is_excluded() -> None:
    keywords = ["剩余流量", "套餐到期", "距离下次重置"]
    assert is_excluded("节点A-剩余流量:98%", keywords) is True
    assert is_excluded("套餐到期-警告", keywords) is True
    assert is_excluded("新加坡-01", keywords) is False


def test_is_excluded_empty_keyword_not_always_match() -> None:
    """An empty keyword should never cause an exclusion match."""
    keywords: list[str] = [""]
    result = is_excluded("新加坡-01", keywords)
    assert result is False, f"empty keyword must not match: got {result}"


# ---------------------------------------------------------------------------
# 3. Region rank (for sorting)
# ---------------------------------------------------------------------------

def test_region_rank_preferred_first() -> None:
    rank_sg = region_rank("新加坡-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_jp = region_rank("日本-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_hk = region_rank("香港-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_tw = region_rank("台湾-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_us = region_rank("美国-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_other = region_rank("random", "新加坡", ["日本", "香港", "台湾", "美国"])

    assert rank_sg < rank_jp < rank_hk < rank_tw < rank_us < rank_other
    assert rank_sg == 0
    assert rank_jp == 1
    assert rank_hk == 2
    assert rank_tw == 3
    assert rank_us == 4
    assert rank_other == 5


# ---------------------------------------------------------------------------
# 4. Candidate sort key
# ---------------------------------------------------------------------------

def test_sort_key_preferred_region_first() -> None:
    """Candidates in preferred region should sort before backups."""
    config = Config()
    config.preferred_region = "新加坡"
    config.backup_regions = ["日本", "香港", "台湾", "美国"]
    config.provider_priority = ["良心云"]

    sg = Candidate(name="新加坡-01", provider="良心云", region=Region.SINGAPORE)
    jp = Candidate(name="日本-01", provider="良心云", region=Region.JAPAN)
    hk = Candidate(name="香港-01", provider="良心云", region=Region.HONGKONG)

    key_sg = candidate_sort_key(sg, config)
    key_jp = candidate_sort_key(jp, config)
    key_hk = candidate_sort_key(hk, config)

    assert key_sg < key_jp
    assert key_sg < key_hk
    assert key_jp < key_hk


def test_sort_key_zhuanxian_first() -> None:
    """专线 nodes should sort before non-专线 within same region."""
    config = Config()
    config.preferred_region = "新加坡"
    config.backup_regions = ["日本"]
    config.provider_priority = ["良心云"]

    a = Candidate(name="专线-新加坡-01", provider="良心云", region=Region.SINGAPORE)
    b = Candidate(name="新加坡-01", provider="良心云", region=Region.SINGAPORE)

    assert candidate_sort_key(a, config) < candidate_sort_key(b, config)


def test_sort_key_no_empty_string_bug() -> None:
    """Ensure sort key does not have the empty-string-always-true bug."""
    config = Config()
    config.preferred_region = "新加坡"
    config.backup_regions = ["日本"]

    # If there were an empty-string check, all names would get the same
    # rank.  Verify that different regions actually produce different keys.
    sg = Candidate(name="新加坡-01", provider="良心云", region=Region.SINGAPORE)
    jp = Candidate(name="日本-01", provider="良心云", region=Region.JAPAN)
    other = Candidate(name="xx-01", provider="良心云", region=Region.OTHER)

    key_sg = candidate_sort_key(sg, config)
    key_jp = candidate_sort_key(jp, config)
    key_other = candidate_sort_key(other, config)

    assert key_sg != key_jp, "Singapore and Japan should NOT have the same sort key"
    assert key_jp != key_other, "Japan and Other should NOT have the same sort key"


# ---------------------------------------------------------------------------
# 5. Preferred region priority (conceptual)
# ---------------------------------------------------------------------------

def test_preferred_region_singapore_rank_0() -> None:
    """Default preferred region is 新加坡 and must rank 0."""
    assert region_rank("🇸🇬 新加坡-A", "新加坡", ["日本", "香港", "台湾", "美国"]) == 0
    assert region_rank("sgp-node-1", "新加坡", ["日本", "香港", "台湾", "美国"]) == 0


# ---------------------------------------------------------------------------
# 6. Backup region fallback
# ---------------------------------------------------------------------------

def test_backup_fallback_order() -> None:
    """When preferred is absent, backups get ranks 1-4 in order."""
    rank_jp = region_rank("日本-01", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_hk = region_rank("🇭🇰 香港", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_tw = region_rank("台湾-台北", "新加坡", ["日本", "香港", "台湾", "美国"])
    rank_us = region_rank("美国-01", "新加坡", ["日本", "香港", "台湾", "美国"])

    assert rank_jp == 1
    assert rank_hk == 2
    assert rank_tw == 3
    assert rank_us == 4


# ---------------------------------------------------------------------------
# 7. Cross-region cooldown (logic test)
# ---------------------------------------------------------------------------

def test_cooldown_logic() -> None:
    """Verify that cooldown constants are set correctly."""
    config = Config()
    # defaults
    assert config.min_region_hold_seconds == 1800  # 30 min
    assert config.switch_cooldown_seconds == 600   # 10 min


# ---------------------------------------------------------------------------
# 8. Dry-run does not actually switch (logic test)
# ---------------------------------------------------------------------------

def test_dry_run_config() -> None:
    """Verify dry_run default and env-var behavior."""
    config = Config()
    assert config.dry_run is False

    import os
    os.environ["DRY_RUN"] = "true"
    config2 = Config()
    assert config2.dry_run is True
    del os.environ["DRY_RUN"]


# ---------------------------------------------------------------------------
# 9. Empty string safety in region detection
# ---------------------------------------------------------------------------

def test_empty_string_not_in_indicators() -> None:
    """No indicator keyword may be an empty string."""
    from clash_auto_switch.region import _INDICATORS
    for region_keywords in _INDICATORS.values():
        for kw in region_keywords:
            assert kw != "", f"empty keyword found in {region_keywords}"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    tests = [
        fn
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  PASS  {test_fn.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test_fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
