# filter 示例

出站管道中间件，`{stage: outbound, produces: rewrite, priority: 10}`，优先级最小所以最先执行。

- 权限：`send_message` 是出站中间件的硬门槛，缺了主体直接跳过并记 `outbound-denied`
- 取消：命中 `cancel_words` 返回 `None` → 审计 `outbound-cancelled`，后续中间件不再执行
- 改写：命中 `rewrite_map` 返回新 `Draft`；改写结果不会再次进入管道（防回灌，selftest T9 用 `passes` 计数验证）

复现：

    python examples/filter/demo.py
