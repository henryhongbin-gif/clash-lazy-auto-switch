# Clash Auto Switch

A local watchdog script for Clash Verge Rev / Mihomo that automatically switches proxy nodes when connectivity degrades.

This repository is intentionally privacy-safe:

- No real proxy nodes
- No subscription URLs
- No UUIDs, passwords, server addresses, or ports
- No personal provider names

Provider names are represented as:

- `ProviderA`: preferred provider
- `ProviderB`: secondary provider
- `ProviderC`: fallback provider

## Features

- Controls Clash/Mihomo through the external controller API or Unix socket.
- Monitors the `GLOBAL` selector group by default.
- Tests Google, YouTube, ChatGPT, and Claude in parallel.
- Counts per-site timeout as a fixed penalty.
- Switches immediately when maximum latency is too high or a node fails.
- Uses provider priority: `ProviderA > ProviderB > ProviderC > Other`.
- Excludes Hong Kong nodes and metadata pseudo-nodes by default.
- Uses short-lived node performance cache.
- Refreshes providers periodically and after network recovery.
- Attempts to return to `ProviderA` after provider refresh.
- Avoids refreshing subscriptions while the base network is offline.

## Default Rules

- Ideal node: average latency `<= 200ms` and max latency `<= 300ms`
- Safe node: average latency `<= 300ms` and max latency `<= 400ms`
- Per-site timeout: `800ms`
- Timeout penalty: `1000ms`
- Four-site full timeout: node test failure
- Periodic provider refresh: every `1800s`

## Run Once

```bash
python3 src/clash_auto_switch.py \
  --socket /tmp/verge/verge-mihomo.sock \
  --secret YOUR_SECRET \
  --group GLOBAL \
  --once
```

## Run as LaunchAgent on macOS

Copy the script to a stable path:

```bash
mkdir -p "$HOME/Library/Application Support/clash-auto-switch"
cp src/clash_auto_switch.py "$HOME/Library/Application Support/clash-auto-switch/clash_auto_switch.py"
chmod +x "$HOME/Library/Application Support/clash-auto-switch/clash_auto_switch.py"
```

Edit `examples/com.example.clash-auto-switch.plist.example`, replace `YOUR_USER` and `YOUR_SECRET`, then load it as a LaunchAgent.

## Privacy Warning

Never commit:

- `clash-verge.yaml`
- Subscription URLs
- Proxy nodes
- UUIDs
- Passwords
- Real provider names if they identify your account
- Local logs containing node names or private provider details

See `.gitignore`.

