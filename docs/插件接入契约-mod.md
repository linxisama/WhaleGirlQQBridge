# 插件接入契约：目录 mod

本文是「目录 mod」这一接入方式的契约。读完你应该能：把一个插件做成 `<custom_plugins_dir>/<id>/` 目录、
写 `plugin.json`、被主体自动挂载而不改主体任何代码，并知道运行时你**能拿到什么、拿不到什么**。

旧方式（`<custom_plugins_dir>/<id>.py` 单个文件 + `register_custom`）**完全不变**，本文只讲新方式。
内置 63 个插件的契约也一点没变：它们拿到的仍然是那个活的 `QQRuntime`。

---

## 1. 目录长什么样

    <data_dir>/custom_plugins/
      my_mod/
        plugin.json      # 清单，必需
        entry.py         # 入口，文件名由 plugin.json 的 entry 指定
        data/...         # 你自己的文件，随意

规则：

- **带 `plugin.json` 的子目录就是一个 mod**，启动/重载时自动挂载，不需要 `register_custom`，
  也不需要在 `custom_plugins.json` 里登记。
- 没有 `plugin.json` 的子目录（解压残留、随手放的文件夹）会被**静默忽略**，不算错误。
- 入口脚本里的相对导入写法与旧的裸 `.py` 插件完全一致：`from . import PluginBase, PluginMeta`。

## 2. plugin.json 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 插件标识，**必须与目录名完全一致**，否则拒载 |
| `api_version` | 是 | 整数。当前只支持 `1`，其它值拒载（升级会换号，不会悄悄兼容） |
| `entry` | 是 | 相对本 mod 目录的入口文件名，**不许 `..`、不许绝对路径**，越界拒载 |
| `name` | 否 | 展示名（字符串） |
| `description` | 否 | 说明（字符串） |
| `version` | 否 | 版本（字符串，只记录进审计） |
| `permissions` | 否 | 字符串列表，取值见下节；含未知取值即拒载 |
| `enabled_by_default` | 否 | 布尔。`false` 表示挂载后默认关闭（用户可在插件设置里开） |

最小可用清单：

    {
      "id": "my_mod",
      "api_version": 1,
      "entry": "entry.py"
    }

`permissions` 的合法取值：`read_message`、`send_message`、`store_data`、`network`、`manage_settings`。
**当前这些权限只做声明与查询**（`runtime.has_permission("network")`），主体不会因此自动放行或拦截
某个具体动作；真正起作用的是下面「运行时门面」里那份很短的可访问名单。

## 3. 拒载与自愈（都不带崩主体）

清单不合格 → 该 mod **拒载**，写一条**加载问题**（运行记录里能看见，`take_load_problems()` 取得到）
和一条审计，其它插件照常加载、主体照常启动。判定顺序：

| code | 含义 |
| --- | --- |
| `bad_json` | `plugin.json` 不是合法 JSON |
| `bad_manifest` | 顶层不是对象 |
| `missing_id` | 缺少 `id` |
| `id_mismatch` | `id` 与目录名不一致 |
| `bad_api_version` / `api_version_unsupported` | `api_version` 不是整数 / 不是 1 |
| `missing_entry` | 缺少 `entry` |
| `entry_escape` | `entry` 越出 mod 目录 |
| `entry_missing` | `entry` 指向的文件不存在 |
| `bad_permissions` / `bad_field` | 权限或可选字段类型/取值不合法 |
| `builtin_conflict` | 与内置插件同名（内置版本优先） |
| `file_conflict` | 存在同名裸插件 `<id>.py`（旧通道优先，目录 mod 不挂载） |

运行期自愈：

- 钩子**超时**（默认 2 秒，模块常量 `MOD_HOOK_TIMEOUT`）或**抛异常**都算一次失败。
- **连续 3 次**失败（`MOD_FAILURE_LIMIT`）→ 自动禁用：把你的 `enabled` 写 `false` 并落盘，
  写一条审计和一条加载问题。其它插件完全不受影响。
- **成功一次即清零**计数，所以偶发失败不会攒成误杀。
- 超时/异常只影响**这一次**钩子调用：主体跳过你，继续跑别的插件、继续回消息。

## 4. 运行时门面 ModRuntime：你能拿到什么

mod 的钩子第二参数**不是**活的 `QQRuntime`，而是一个只读门面 `ModRuntime`。

**可访问（白名单，就这些）**

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `mod_id` | `str` | 你的插件 id |
| `api_version` | `int` | 当前 mod API 版本（=1） |
| `settings` | `dict` | 当前设置的一份**拷贝**，改它不会写回 |
| `get(key, default=None)` | 方法 | 读设置，不存在返回默认值 |
| `has_permission(name)` | 方法 | 清单里声明过的权限才返回 `True` |
| `log(message)` / `note(message)` | 方法 | 写进运行记录 |
| `now()` | 方法 | 当前时间戳（秒，`float`） |

**一律拒绝（`AttributeError`，「ModRuntime 不向 mod 提供 …」）**

`bot`、`plugins`、`config`、`db`、`engine`、`send`、`_plugins`、`call_ai` …… 以及**任何**不在上表里的属性。
给门面属性赋值同样抛 `AttributeError`（`ModRuntime 是只读门面`）。

需要保存自己数据的，用 `PluginBase` 自带的能力（这部分契约没变）：

    self.load_state("mine.json", {})     # 读 <state_dir>/<你的 id>/mine.json
    self.save_state("mine.json", {...})  # 写

想发消息/调用模型？当前 mod **拿不到**这些能力（见第 8 节），请把需求走内置插件或后续 API。

## 5. on_ready 通道对 mod 关闭

`on_pre_ai` / `on_pre_ai_async` 的返回值里可以放 `on_ready`（让主体代你 `await` 一个回调）。
这是给内置插件留的口子，**mod 不能用**：返回里的 `on_ready` 会被剥掉，并写一条
`on-ready-stripped` 审计（不静默）。其余键（`delay` / `block` / `extra_system` /
`volatile_system` / `max_tokens`）照常生效。内置插件在该通道上的行为完全不变。

## 6. 审计日志

追加写入 `<data_dir>/qq-only/plugin_audit.jsonl`，每行一个 JSON 对象，字段固定：

    {"ts": "2026-10-27T10:53:49+08:00", "plugin": "slow_poker", "event": "hook-timeout",
     "hook": "on_ingest", "code": "timeout", "detail": "钩子超过 2 秒未返回（已跳过）"}

`event` 取值：`mounted`、`load-refused`、`hook-timeout`、`hook-failed`、`auto-disabled`、
`on-ready-stripped`。写盘失败只在日志里告警，绝不影响插件运行。

## 7. 一个完整的最小例子

`custom_plugins/hello_mod/plugin.json`

    {
      "id": "hello_mod",
      "api_version": 1,
      "entry": "entry.py",
      "name": "打个招呼",
      "version": "0.1.0",
      "permissions": ["read_message", "store_data"]
    }

`custom_plugins/hello_mod/entry.py`

    from . import PluginBase, PluginMeta


    class Hello(PluginBase):
        META = PluginMeta(id="hello_mod", name="打个招呼", category="utility",
                          description="示例 mod")

        def on_ingest(self, message, runtime):
            runtime.note("hello_mod 收到一条消息")
            seen = int(self.load_state("count.json", {"n": 0}).get("n", 0)) + 1
            self.save_state("count.json", {"n": seen})
            if seen == 1:
                runtime.log(f"第一条消息，mod_id={runtime.mod_id}")
            return None        # 返回 str 会直接回复并跳过 AI

放进目录、重启或重载插件即可生效 —— 不用改主体，不用登记。

不过「重载」只换钩子与插件实例，**已经建好的工具表不会跟着换**（见 §8 第 5 条）：重载后旧表里
那条 spec 仍绑着重载前的旧实例，调它拿到的是旧代码的结果，被摘掉的 mod 在这段时间里也照样能被
调到。QQ 侧会在下一次取表时（预热扫描或下一条消息，`qq_app.QQRuntime.tool_registry_fresh()`）
发现插件层修订号变了并重建，控制台侧每轮自己比号；等不到就重启程序。改完 mod 的工具体，请发
一条消息实测一次再收工。

## 8. 已知限制（诚实列出）

1. **纯 Python 内的隔离只能防误用，不是安全沙箱**。同一进程里 `sys.modules`、栈帧、
   文件系统都可达；`ModRuntime` 挡的是「顺手拿主体内部对象」这类误用，
   挡不住刻意绕过（例如直接 `import src.plugins.manager`）。要真隔离需要子进程/独立解释器，
   当前明确不做。
2. **同步钩子超时不会杀掉线程**。超时只是主体不再等它，那个守护线程会自己跑完；
   它若长期占用 CPU 或占着你的锁，后果仍由 mod 自己承担。
3. **`permissions` 目前只是声明**，没有与之绑定的自动放行/拦截动作。
4. mod 不能通过 `on_ready` 让主体代执行代码，也**没有**发消息/调用模型的通道。
5. **工具表不随 `reload()` 更新**。`PluginManager.reload()` 会重建所有插件实例并把
   `mod_revision` 无条件 +1（src/plugins/manager.py:484），但它**不清**已经建好的工具表：
   表缓存在 `runtime.tool_registry` 上（src/tool_loop.py:382），而 `register_plugin_tools`
   只往表里追加、从不注销（src/tool_loop.py:354）。失效靠修订号：QQ 侧下一次
   `qq_app.QQRuntime.tool_registry_fresh()`（预热扫描或下一条消息）、控制台侧下一轮取表时
   比号，号变了才重建。号还没比的窗口里，旧 spec 仍 `available=True`，点到的是重载前的旧
   实例，被摘掉的 mod 也照样能被调到。注意「停用/启用」**不** bump 号（活读 settings），所以
   旧表也拦得住：`spec.available` 仍是 `True`，调用返回 `code=disabled` —— 这是既有
   语义，不是 bug。
6. **`sync_mods` 的变更判据是 `(mtime_ns, size)`，被回填就看不出改动**
   （src/plugins/manager.py:81 `_file_stamp`、:1024 `sync_mods`）。`cp -p`、解压、
   `rsync --times`、部分编辑器保留时间戳时，源码改了但 mtime 与 size 一字不差 → 判成
   `unchanged`，热同步不会重载它，改动就是看不见。要认内容只能另外比哈希；临时办法是
   `touch` 一下或重启程序。
