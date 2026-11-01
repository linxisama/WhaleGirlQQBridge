# real 示例

从产品现有 `src/plugins/keyword_reply.py` 抽出来的关键词回复，main priority 90。

- 规则来自 `ctx.cfg["rules"]`，每行 `关键词=回复`，空行与坏行跳过
- 匹配逻辑与产品一致：`text.lower()` 子串匹配，从上到下第一条命中即返回
- `ctx.cfg["enabled"]` 为假时直接弃标
- 权限：`read_message` + `send_message`

复现：

    python examples/real/demo.py
