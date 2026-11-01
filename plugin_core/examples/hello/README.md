# hello 示例

最小可用插件：任意消息都竞标 `main`，胜出后回一句固定文本。

- 权限：`read_message` + `send_message`（缺 `send_message` 时动作被拒并落审计，见 selftest T3）
- 能力：`{stage: inbound, produces: main, priority: 20}`
- 配置：`reply` 覆盖固定回复文本
- 存储：`hits` 计数落在 `<data>/plugin_kv/hello.json`

复现：

    python examples/hello/demo.py
