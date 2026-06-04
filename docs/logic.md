# Runtime Logic

1. Check direct network connectivity without using Clash proxy.
2. If the base network is offline, wait and do not refresh providers.
3. If the network recovers, refresh providers and invalidate node cache.
4. After provider refresh, scan only `ProviderA`; switch back if an ideal node is found.
5. Test the current node against Google, YouTube, ChatGPT, and Claude in parallel.
6. A single site timeout means no response within `800ms`; it is counted as `1000ms`. All four sites timing out is a test failure.
7. Trigger a scan immediately on failure or max latency `> 500ms`.
8. If max latency is `300-500ms`, trigger a scan only after two consecutive degraded rounds.
9. Scan candidates by provider priority: `ProviderA > ProviderB > ProviderC > Other`.
10. Before full four-site testing, run a fast Google prefilter.
11. Cache candidate average and max latency for 5 minutes.
12. Ignore cache if it is expired, above safe threshold, or provider data was refreshed.
13. Prefer ideal nodes: average `<= 200ms`, max `<= 300ms`.
14. Fall back to safe nodes: average `<= 300ms`, max `<= 400ms`.
15. After a switch, reset degraded counters.
16. Use adaptive polling: 15s after ideal result, 5s after safe result, 3s after switch or degraded conditions.

This repository intentionally keeps the logic documentation text-only so no local screenshots, private node names, or account-specific details are published.
