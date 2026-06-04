import time

from clash_auto_switch.config import Config
from clash_auto_switch.switcher import SwitcherState, switch_if_needed


class FakeApi:
    def __init__(self, delays, proxies=None, providers=None):
        self.delays = delays
        self.proxies = proxies
        self.providers = providers
        self.switched = []
        self.refreshed = 0

    def load_proxies(self):
        if self.proxies is not None:
            return self.proxies
        return {
            "GLOBAL": {
                "type": "Selector",
                "now": "Current SG",
                "all": ["SG bad", "JP good", "HK good"],
            },
            "Current SG": {"type": "Shadowsocks"},
            "SG bad": {"type": "Shadowsocks"},
            "JP good": {"type": "Shadowsocks"},
            "HK good": {"type": "Shadowsocks"},
        }

    def load_provider_members(self):
        if self.providers is not None:
            return self.providers
        return {"ProviderA": {"SG bad", "JP good", "HK good"}}

    def proxy_delay(self, name, _test_url, _timeout_ms):
        return self.delays.get(name)

    def refresh_providers(self):
        self.refreshed += 1

    def switch_proxy(self, group_name, node_name):
        self.switched.append((group_name, node_name))


def make_config():
    config = Config()
    config.group = "GLOBAL"
    config.force_scan = True
    config.dry_run = True
    config.stable_samples = 1
    config.switch_cooldown_seconds = 0
    config.min_region_hold_seconds = 0
    config.provider_refresh_interval = 0
    config.auto_refresh_on_failure = False
    config.test_urls = ["google", "youtube", "chatgpt", "claude"]
    config.region_sticky = True
    config.preferred_region = "新加坡"
    config.backup_regions = ["日本", "台湾", "美国"]
    return config


def test_region_sticky_falls_back_only_after_preferred_region_unhealthy():
    api = FakeApi({"Current SG": None, "SG bad": None, "JP good": 150, "HK good": 80})
    config = make_config()
    state = SwitcherState()

    delay, switched, target = switch_if_needed(api, config, state)

    assert delay == 150
    assert switched is False
    assert target == "JP good"
    assert api.switched == []


def test_hong_kong_is_not_used_even_when_fastest():
    api = FakeApi({"Current SG": None, "SG bad": None, "JP good": None, "HK good": 80})
    config = make_config()
    state = SwitcherState()

    delay, switched, target = switch_if_needed(api, config, state)

    assert delay is None
    assert switched is False
    assert target == ""
    assert api.switched == []


def test_lower_provider_regresses_to_primary_when_healthy():
    api = FakeApi(
        {"Backup SG": 120, "Primary SG": 150},
        proxies={
            "GLOBAL": {
                "type": "Selector",
                "now": "Backup SG",
                "all": ["Primary SG", "Backup SG"],
            },
            "Primary SG": {"type": "Shadowsocks"},
            "Backup SG": {"type": "Shadowsocks"},
        },
        providers={
            "Primary": {"Primary SG"},
            "Backup": {"Backup SG"},
        },
    )
    config = make_config()
    config.dry_run = False
    config.force_scan = False
    config.provider_priority = ["Primary", "Backup"]
    config.lower_provider_regress_after = 0.001
    state = SwitcherState(
        current_provider="Backup",
        lower_provider_since=max(0.000001, time.monotonic() - 0.01),
    )

    delay, switched, target = switch_if_needed(api, config, state)

    assert delay == 150
    assert switched is True
    assert target == "Primary SG"
    assert api.switched == [("GLOBAL", "Primary SG")]
    assert state.current_provider == "Primary"
    assert state.lower_provider_since == 0.0
