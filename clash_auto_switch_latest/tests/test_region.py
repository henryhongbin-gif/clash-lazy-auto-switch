from clash_auto_switch.config import Config
from clash_auto_switch.region import Region, get_region
from clash_auto_switch.switcher import collect_candidates


def test_region_detects_hong_kong_variants():
    assert get_region("香港 IPLC 01") == Region.HONGKONG
    assert get_region("HK-01") == Region.HONGKONG
    assert get_region("Hong Kong Premium") == Region.HONGKONG


def test_default_backup_regions_do_not_include_hong_kong():
    config = Config()
    assert "香港" not in config.backup_regions


def test_collect_candidates_excludes_hong_kong_even_without_keywords():
    config = Config()
    config.exclude_keywords = []
    proxies = {
        "GLOBAL": {"type": "Selector", "all": ["SG-01", "HK-01"]},
        "SG-01": {"type": "Shadowsocks"},
        "HK-01": {"type": "Shadowsocks"},
    }

    candidates = collect_candidates("GLOBAL", proxies["GLOBAL"], proxies, {}, config)

    assert [candidate.name for candidate in candidates] == ["SG-01"]

