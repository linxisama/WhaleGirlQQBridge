# persona 示例

main 竞标 priority 80，并用 `ctx.model.chat` 真实调模型（mock 模型下返回确定性文本）。

- 权限：`read_message` + `send_message` + `store`
- 能力：`{stage: inbound, produces: main, priority: 80, exclusive: true}`
- 可弃标：消息里没有 `keywords` 里的任一触发词时返回 `None`，本轮不参与仲裁
- 数据策略：`purge`（卸载时清 KV 与配置，selftest T10 用）

复现：

    python examples/persona/demo.py
