from clash_auto_switch.config import Config
from clash_auto_switch.switcher import SwitcherState, switch_if_needed


class FakeApi:
    def __init__(self, delays):
        self.delays = delays
        self.switched = []
        self.refreshed = 0

    def load_proxies(self):
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
