# Clash Auto Switch

Clash Verge / Mihomo 懒人自动切换节点脚本。重点是稳定运行：当前节点失败、延迟过高或热点网络恢复后，自动刷新与切换到健康节点。

## 特性

- 使用 Clash / Mihomo external controller API 或 Unix socket。
- 默认监控 `GLOBAL` 选择器组。
- 保留 `--dry-run`，可只打印决策不执行切换。
- 四站并发测速：Google、YouTube、ChatGPT、Claude。
- 默认排除香港节点和流量/套餐信息节点。
- 地区稳定策略：优先新加坡；新加坡无健康节点时，按日本、台湾、美国顺序兜底。
- 候选节点先做 Google 快速预筛，再做完整四站测试。
- 节点性能短缓存 5 分钟，减少重复测速。
- 基础网络离线时不反复刷新订阅；网络恢复后再刷新。

## 默认健康规则

- 理想节点：平均延迟 `<= 200ms` 且最大延迟 `<= 300ms`
- 安全节点：平均延迟 `<= 300ms` 且最大延迟 `<= 400ms`
- 单站超时：`800ms`
- 超时计入值：`1000ms`
- 最大延迟 `> 500ms` 或测试失败：立即扫描
- 最大延迟 `300-500ms`：连续 2 次后扫描

## 运行

复制示例配置：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```bash
CLASH_SECRET=YOUR_SECRET
CLASH_GROUP=GLOBAL
CLASH_PROVIDER_PRIORITY=你的首选订阅组,你的备用订阅组,你的最后兜底订阅组
```

只测试一轮，不真正切换：

```bash
python3 -m clash_auto_switch.main --once --force-scan --dry-run
```

正常运行：

```bash
python3 -m clash_auto_switch.main
```

## macOS LaunchAgent

参考：

```text
examples/com.example.clash-auto-switch.plist
```

先把项目放到稳定目录，再把 plist 里的 `YOUR_USER`、项目路径和环境变量改成自己的值。

## 隐私提醒

不要提交以下内容：

- 真实订阅链接
- 真实节点配置
- Clash / Mihomo 的完整 yaml 配置
- `.env`
- UUID、密码、server、private key
- 带真实节点名的运行日志
