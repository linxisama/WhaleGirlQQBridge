# 插件内核 API v1（权威规格，内核与文档都照此实现）

> 这份文件是 DSeasier v3.0 契约在**我们仓库**里的落地规格。它已经定死，改动必须带版本号（api_version +1）。
> 位置：`D:/qq机器人5.0/docs/插件内核-API-v1.md`。实现：`D:/qq机器人5.0/plugin_core/`。

## 0. 硬约束（来自契约，逐条可机检）

1. 挂载方式 = **目录扫描 + manifest**：`<plugins_dir>/<id>/plugin.json` + 入口文件；目录名必须等于 `id`。
2. 加载完成即 **freeze**：运行期增删插件被拒绝并留审计（`frozen`）。
3. 只有**三个挂载点**：入站（inbound）/ 生成（generate）/ 出站（outbound）。没有任意 hook。
4. 插件返回**动作列表**，由主体校验权限后执行；插件不得自己发消息。
5. 权限五项中文原词，标识符固定：
   | 展示词 | 标识符 | 管什么 |
   |---|---|---|
   | 读消息 | `read_message` | 收到入站事件、读会话内容 |
   | 发消息 | `send_message` | 产出 main/attach/extra、改写出站 |
   | 存数据 | `store` | 读写本插件命名空间 KV |
   | 联网 | `network` | 发起对外请求（只能经 `ctx.fetch`） |
   | 管理权限 | `admin` | 读审计、改他人插件状态 |
   子标记映射：`config_write` → `store`（只能写自己的配置）；`exec` → `admin`；`read_env` → `admin`。
6. 权限校验发生在**执行动作前**；未声明而尝试 → 拒绝 + 审计（插件 id、动作、时间、会话），不抛给用户，进程不崩。
7. `api_version` 是 int，加载时严格比对 `API_VERSION = 1`，不等即拒载并写清差的版本。
8. 不做热重载，只支持整批重载（`Host.reload_all()`）。
9. 插件之间不互相 import；通信只走 `emit` 动作 → `ctx.on(event)`。
10. 上下文只读且已归一化：**不允许任何字段直接来自上游原始报文**（无群号原样主键、无 onebot 字段名）。

## 1. 包结构（实现侧）

```
plugin_core/
  __init__.py        导出 Host / API_VERSION / 版本号
  api.py             API_VERSION、PLUGIN_API_VERSION、常量、异常
  manifest.py        manifest 读取与校验（错误码枚举）
  loader.py          目录扫描、entry 解析、freeze、拒载审计
  sdk.py             PluginAPI（给插件用）、Context（只读）、Draft、Action 构造器
  arbiter.py         竞标-仲裁-合成
  permissions.py     五项权限 + 子标记映射 + 动作前校验
  actions.py         动作执行（顺序、序列化、单个失败不中断）
  outbound.py        出站管道（串行、priority 升序、取消即短路、改写结果不回灌）
  store.py           命名空间 KV + data_policy（retain / purge）
  events.py          十个生命周期时刻
  audit.py           JSONL 审计（含仲裁记录）
  host.py            Host：把上面拼起来，空载七步
  mock.py            mock 传输层 + mock 模型
  cli.py             `python -m plugin_core <子命令>`
  selftest.py        T1–T10
  examples/          hello / persona / filter / real
```

## 2. manifest v1（`plugin.json`）

必填：`id` `name` `version` `api_version` `author{name,contact}` `description` `entry` `permissions`
`capabilities` `side` `data_policy` `upstream{protocol,kernel,range}` `native`
可选：`permission_flags` `storage` `config_schema`

- `id`：kebab-case `^[a-z0-9][a-z0-9-]{0,63}$`，发布后不变，必须等于目录名。
- `version`：semver `^\d+\.\d+\.\d+`。
- `api_version`：int，必须 `== API_VERSION`。
- `description`：≤ 30 字。
- `entry`：`"<相对路径>.py:<函数名>"`，函数签名 `def setup(api: PluginAPI) -> None`；路径必须在插件目录内（逃逸即拒载）。
- `permissions`：上面五个标识符的子集，可为 `[]`。
- `capabilities`：数组，每项
  `{ "stage": "inbound"|"generate"|"outbound", "produces": "main"|"attach"|"extra"|"silent"|"rewrite"|"split"|"cancel", "priority": int, "exclusive": bool?, "requires_main": bool? }`
  同一插件可有多项；`exclusive` 只对 main 有意义；`requires_main` 只对 attach 有意义。
- `side`：`local` / `group` / `both`。
- `data_policy`：`retain`（卸载保留）/ `purge`（卸载即清）。
- `upstream`：`{"protocol": "...", "kernel": "...", "range": ">=3.2 <4"}`；`range` 不许空、不许写"最新版"。
- `native`：bool。

拒载码（`load-refused` 审计里的 `code`，稳定不变）：
`bad-json` `bad-manifest` `missing-field` `bad-id` `id-mismatch` `bad-api-version` `api-version-mismatch`
`entry-missing` `entry-escape` `bad-entry` `setup-error` `duplicate-id` `bad-permission` `bad-capability`
`bad-data-policy` `bad-side` `bad-upstream`

## 3. 插件侧 SDK（`plugin_core.sdk`）

```python
from plugin_core.sdk import PluginAPI, Context, Action

def setup(api: PluginAPI) -> None:
    api.declare_inbound(on_message)      # 入站观察 / 竞标
    api.declare_outbound(on_outbound)    # 出站改写；返回 None 表示取消
    api.on('event-name', handler)        # 事件订阅（unload 自动清理）
    api.declare_command('签到', handler)  # 显式指令
    api.declare_timer(60, handler)       # 周期触发（unload 自动停）
```

`PluginAPI` 方法（内部只登记，不做副作用）：
- `declare_inbound(fn)`：`fn(ctx: Context) -> Bid | None | list[Bid]`
- `declare_outbound(fn)`：`fn(ctx: Context, draft: Draft) -> Draft | None`
- `on(event: str, fn)`：`fn(ctx: Context, payload: dict) -> list[Action] | None`
- `declare_command(name: str, fn)`、`declare_timer(seconds: float, fn)`
- `can(permission: str) -> bool`
- 只读属性：`plugin_id`、`manifest`（dict 拷贝）、`log`、`cfg`（本插件配置 dict，只读视图）、`store`

`Bid`（`api.bid("main", 80, generate=fn, reason="关键词命中")`）：
`{"produces": ..., "priority": int, "generate": callable|None, "reason": str}`
- `produces` ∈ `main` / `attach` / `extra` / `silent`
- `silent` 不需要 `generate`；其余需要。
- `generate(ctx) -> list[Action]`（可以返回 `[]`，等于这一轮不产出）

`Context`（只读；`__setattr__` 抛 `AttributeError`）：
```
ctx.session   {id, kind: 'group'|'private', name}
ctx.sender    {id, name, role: 'owner'|'admin'|'member', is_bot: bool}
ctx.message   {id, text, segments: [{type: 'text'|'image'|'voice'|'file'|'face', ...}],
               mentions: [ids], reply_to: str|None, ts: float}
ctx.plugin    {'id': ..., 'version': ...}
ctx.host      {'version': ..., 'api_version': 1}
ctx.cfg       本插件配置（dict 只读拷贝）
ctx.store     本插件 KV：get(key, default) / set(key, value) / delete(key) / keys()
ctx.can(p)    -> bool
ctx.log       debug/info/warn/error（自动带插件 id）
ctx.model     chat(messages: list[dict], *, max_chars: int|None = None) -> str
              # 只给 messages 与输出约束；不暴露 provider / endpoint / temperature
ctx.fetch(url, *, method='GET', body=None, timeout=10.0) -> dict
              # 需要 network 权限；未声明 → PermissionDenied（内核拦，插件不自己发）
```

`Draft`（出站）：`{"conversation": str, "text": str, "media": list, "plugin": str}`；出站函数返回新 Draft 或 `None`（取消）。

`Action`（`plugin_core.sdk.Action`）：
```
Action.send_text(conversation, text)      # send_message
Action.send_media(conversation, media)    # send_message
Action.store_set(key, value)              # store
Action.store_del(key)                     # store
Action.schedule(delay, event, payload)    # 无
Action.emit(event, payload)               # 无
Action.noop()                             # 无
```
每个动作可 `as_dict()` 序列化；审计里原样记录。

## 4. 仲裁（`arbiter.py`）—— 全文心脏

输入：一条归一化消息 + 已 freeze 的插件表。
流程：`入站观察 → 竞标 → 仲裁 → 生成 → 合成 → 出站加工 → 发送`

1. 竞标阶段**不调模型**：逐个插件调 `declare_inbound` 的 fn（异常隔离，记 `hook-failed`），收集 `Bid`。
   未声明 `read_message` 的插件**不进入**竞标（审计 `skipped: no-read-permission`）。
   未命中触发条件 → 返回 `None` → 记 `dropped: no-trigger`。
2. **silent 与 main 同场按 priority 比较**（规则 1）：若 silent 的最高 priority ≥ main 的最高 priority，
   则 silent 胜出 → 本轮所有 main 作废，记录胜出 silent 的插件与 `reason`。
3. main 按 priority 降序；相同 priority 按 **manifest 声明序**（即 `plugin.json` 里 capabilities 的顺序，再按 id 字典序兜底），
   不按加载顺序。胜者出 1 个；其余记 `dropped: priority-low`（含胜者 priority 便于解释）。
4. **attach 只在 main 胜出之后执行**：按 priority 降序串行生成；attach 失败不影响 main 发送（记 `attach-failed`）。
   `requires_main: true` 且无 main 胜出 → 记 `dropped: requires-main`。
5. **extra 独立发送**：各自按 priority 降序生成并发送，仍要过出站管道。
6. 没有 main 也没有 extra → **什么都不发**，这一轮仍然写一条仲裁记录（`sent: []`），沉默合法。
7. 只有胜者调 `generate`；main 只生成一次。

仲裁记录（每条消息一条，JSONL，`event='arbitration'`）：
```json
{"event":"arbitration","message_id":"m-1","session":"g:100","bids":[{"plugin":"a","produces":"main","priority":80,"reason":"..."}],
 "silent_winner":null,"winner":{"plugin":"a","produces":"main"},
 "dropped":[{"plugin":"b","produces":"main","priority":20,"reason":"priority-low"}],
 "generated":["a"],"attached":[],"extra":[],"sent":[{"conversation":"...","text":"..."}],"duration_ms":3}
```
必须能回答三个问题：几家竞标（id/类型/优先级）、谁胜出、落败原因（`priority-low` / `no-trigger` / `requires-main` / `silent-over-main`）。

## 5. 出站管道（`outbound.py`）

- 串行；`priority` **小的先执行**（D8）。
- 返回 `None` 即取消本轮发送并短路（后续中间件不再执行），审计 `outbound-cancelled`。
- 改写结果**不再进管道**（防回灌）：一次消息只走一遍。
- 中间件必须有 `send_message` 权限，否则跳过并审计 `outbound-denied`。

## 6. 生命周期十个时刻（`events.py`）

`preLaunch` → `load` → `enable` → （`message` / `timer` / `command` / `event`）→ `disable` → `unload`；`error` 在插件抛异常后回调。
- `load` 阶段**不许有对外副作用**；`enable` 才能注册命令/订阅事件。
- `unload` 必须清理四样：定时器、事件订阅、连接、线程（内核用 `plugin_core.events.Lifecycle` 记账，未清理的记 `leak-warning`）。
- `preLaunch` 在连上协议端之前跑完（配置迁移放这里）。

## 7. 存储与 data_policy（`store.py`）

- 落盘 `<data_dir>/plugin_kv/<plugin_id>.json`，键不跨插件可见。
- `data_policy=purge`：`Host.uninstall(plugin_id)` 删 KV + 配置；`retain`：保留并在重装时原样读回。
- 配置：`<data_dir>/plugin_config/<plugin_id>.json`（`config_write` 子标记才能写；默认只读）。

## 8. Host（`host.py`）

```python
Host(plugins_dir, data_dir, transport=None, model=None, *, host_version='...')
host.start()            # preLaunch → load → enable → freeze（返回 freeze 清单）
host.handle(message)    # 归一化 → 入站 → 仲裁 → 生成 → 合成 → 出站 → 发送（返回仲裁记录 dict）
host.reload_all()       # 整批重载（这是唯一的重载方式）
host.freeze_snapshot()  # 不可变清单（tuple of frozen dict）
host.add_plugin(dir)    # freeze 之后调用 → 拒绝 + 审计 'frozen'
host.uninstall(plugin_id)
host.selftest_*         # 见下
```

**空载七步（T1）用 `mock.MockTransport` + `mock.MockModel` 验证**，`plugins_dir` 为空目录：
1 连上（mock 记 `connected`）2 归一化（日志打出归一化字段，无上游报文）3 组 prompt（打印实际 prompt）
4 调模型（MockModel 可换）5 回复发送（mock 收到 outbound）6 会话落盘 `<data>/session.json`，重建 Host 后续上
7 默认人格（host 内置一句话系统提示，无插件也回话）。

## 9. CLI（`cli.py`）

- `python -m plugin_core demo`：**50 行原型**。mock 消息 + 两个插件（A main 80 / B main 20），打印：
  ```
  竞标：[A(main,80), B(main,20)]
  胜出：A
  落败：B（优先级低）
  ```
- `python -m plugin_core selftest --all`：跑 T1–T10，逐条 `PASS/FAIL` + 证据文件写到 `<cwd>/selftest-out/`。
- `python -m plugin_core selftest --only T8`：单条。
- `python -m plugin_core run --plugins <dir> --data <dir> --message "文本"`：跑一条消息并打印仲裁记录（出站打到 stdout）。

**零依赖**：全部标准库，可直接用系统 python 3.10+ 跑，不 import 本仓库 `src/` 任何模块。

## 10. T1–T10 映射（selftest 必须逐条覆盖）

| # | 场景 | 关键断言 |
|---|---|---|
| T1 | plugins 目录为空 | 7 步全过；重启后记得；无插件也能回话 |
| T2 | 装 hello | 一条 mock 消息 → hello 回固定文本，耗时 < 3 s |
| T3 | hello 去掉 `send_message` | 动作被拒（`denied`），审计 1 条，进程不崩，无消息发出 |
| T4 | hello 的 inbound 抛异常 | 主体继续；其它插件正常；审计 `hook-failed` |
| T5 | 静态检查 | `examples/` 源码 grep `onebot|napcat|snowluma|astrbot` = 0，且无对应 import |
| T6 | 删掉任一插件目录 | 主体仍启动，freeze 清单少一个 |
| T7 | hello 的 `api_version` 改 999 | 拒载，日志说明 `api_version=999 != 1`，其余插件照常 |
| T8 | persona(80) + hello(20) | 只有 persona 回；仲裁记录写明胜败与原因 |
| T9 | filter priority 10 命中词表 | 出站文本被改写/取消；改写结果不再进管道（计数 1 次） |
| T10 | 卸载 persona | `purge` 清 KV；`retain` 保留；重装不串味 |

## 11. 四个参考插件（`plugin_core/examples/`）

| 目录 | 要点 |
|---|---|
| `hello` | 最小路径：`read_message` + `send_message`；竞标 main，返回固定文本 |
| `persona` | main priority 80 + `exclusive: true`；用 `ctx.model.chat` + 系统提示；**可弃标**（话题接不住时返回 `None`，实现里给可配置关键词） |
| `filter` | 出站 `rewrite` priority 10；命中词表改写或取消；验证短路与防回灌 |
| `real` | 从现有代码真抽：`关键词回复`（对应 `src/plugins/keyword_reply.py`），main priority 90，规则来自 `ctx.cfg` |

每个目录内必须有：`plugin.json`、入口 `.py`、`README.md`（怎么装 / 怎么触发 / 要什么权限）、一个能复现的最小脚本或测试。

## 12. 三个指标（MIGRATION.md 要填）

| 指标 | 改造前 | 改造后（本内核） | 门槛 |
|---|---|---|---|
| 适配层行数 | `src/qq_transports.py` **1339** 行 | `plugin_core/mock.py`（mock 适配层）+ 未来真适配层，实测填实际行数 | ≤ 2000 |
| 上游假设位置数 | 422 处 / 59 文件（`handoff/selfcheck/assume_audit.py` 可复跑） | **1**：仅 `plugin_core/adapters/<protocol>.py` 一处 | 1 |
| 空载能否收发 | 未验（T1 未跑） | 能（T1 通过） | 能 |

## 13. 诚实边界（写进 OPEN-QUESTIONS，不许掩饰）

- 同进程 Python 插件**不是安全沙箱**：`network` / `exec` 只能靠契约 + 动作层校验，插件若直接 `import requests` 内核拦不住；交付里要写明这是协作式限权，并提供 `selftest` 静态扫描作为可机检的兜底。
- 真 QQ 链路（官方协议 / qqbridge）本批不接；T1–T10 全部用 mock，真链路迁移写进 `MIGRATION.md` S4。
