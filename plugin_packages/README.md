# 可安装插件源码

`voice_console/` 是「语音控制台 1.0.2」的可维护源码，不自动加载到宿主。用户发行包在 `D:\聊天机器人\插件市场`；补充生成/QQ交付分离查询、合成取消及Qwen音色/语气描述。以1.0.2为推荐版本，保留之前的产物供对照。

构建使用 `python -m tools.build_release --plugin-source plugin_packages/voice_console --destination <新的插件目录> --plugin-archive <新的ZIP路径>`。工具只收录插件入口、plugin.json、README、LICENSE和哈希清单，拒绝覆盖已有目标。

此插件要求含音频网关的QQBridge5.1.2及以上版本；不会把Qt核心模块复制到旧宿主中。

## 手动添加的稳定插件包（`pack-*`）

`pack-repeat-guard/`、`pack-burst-merge/`、`pack-sleep-quiet/` 是三个**已验证稳定**、自包含的小插件包：
分别从内置插件 `src/plugins/anti_repeat.py`、`burst_merge.py`、`sleep_mode.py` 独立而来（**内置文件未改**，
两者可并存）。它们不会被宿主自动加载，必须由用户手动放入自定义插件目录。

| 包 id | 版本 | 用途 | 依赖 |
| --- | --- | --- | --- |
| `pack-repeat-guard` | 1.0.0 | 同一群里同一句话超过阈值就静默丢弃（`on_block`） | 无外部依赖；宿主 ≥ 5.2.0 |
| `pack-burst-merge` | 1.0.0 | 同一人连发时加 `delay`，几条消息合成一次调用（`on_pre_ai`） | 无外部依赖；宿主 ≥ 5.2.0 |
| `pack-sleep-quiet` | 1.0.0 | 睡眠时段内不回复，被 @ 时可选放行（`on_block`） | 无外部依赖；宿主 ≥ 5.2.0 |

### 手动放入 / 安装
1. 自定义插件目录的绝对路径由 `GET /api/plugins/custom` 的 `dir` 字段给出（安装版默认是 `%LOCALAPPDATA%\WhaleGirlQQ\data\qq-only\custom_plugins`，源码运行时是仓库的 `data\qq-only\custom_plugins`，不要写死）。
2. 把**整个包目录**（内含 `plugin.json`、`README.md`、入口 `.py`）原样复制进去；目录名必须等于 `plugin.json` 的 `id`。
3. 重启后台或触发插件重载即可：管理器扫描该目录时自动挂载，**不需要**写 `custom_plugins.json`、也不调 `register_custom`。
4. 也可以在控制台「插件 → 本地插件包」里选中包后点「安装到本地插件目录」，可勾选「安装后自动启用」；已安装的包会显示状态，重复安装会提示，需要时勾选覆盖。
5. 也可以压成 ZIP 走「本地安装」（`POST /api/plugins/install-local`）：ZIP 里只能有一个包根（根目录的 `plugin.json`，或唯一顶层目录里的 `plugin.json`）。

### 启用 / 禁用 / 移除
- 三个包都是 `enabled_by_default: false`：挂上后**默认不生效**，要在插件设置里勾「启用」才工作。
- 禁用：取消插件设置里的 `enabled`（或停用该插件）；不需要删文件，设置保留。
- 移除：先停用，再删除该包目录；控制台插件列表点删除也会连同包目录一起移除。若同目录下存在同名裸插件 `<id>.py`，它会按旧通道优先加载，包目录会被 `file_conflict` 拒载——两个通道只留一个。

### 坏插件隔离
- 清单不合格的包在下一次扫描时**只拒载自己**：其它插件与后台主体照常启动，原因进插件页的加载问题与 `plugin_data/plugin_audit.jsonl` 的 `load-refused` 记录。
- 入口在 import 期抛异常同样只记一条「加载失败（已跳过，不影响其它插件）」，不会带崩后台。
- 管理器侧拒载码见 `src/plugins/manager.py` 的 `_validate_mod_manifest`：`missing_id` / `id_mismatch` / `bad_api_version` / `api_version_unsupported` / `missing_entry` / `entry_escape` / `entry_missing` / `bad_permissions` / `bad_json` / `bad_manifest` / `builtin_conflict` / `file_conflict` / `dependency-missing` / `dependency-cycle`。
- 只读预检（不安装、不 import）：`python -m src.plugin_packs plugin_packages` 输出 JSON，逐包给 `ok`、`issues`（`error` 拒载、`warning` 仅提示）与 `counts`。包目录扫描还支持 ZIP 探测。

### 包清单字段（`plugin.json`）
| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | `^[a-z0-9][a-z0-9_-]{0,63}$`，**必须等于目录名**，且不能与内置插件 id 相同 |
| `name` | 是 | 显示名 |
| `version` | 是 | semver，例 `1.0.0` |
| `entry` | 是 | 包内相对 `.py`（可写 `文件.py:函数名`）；不许绝对路径或 `..` |
| `permissions` | 是 | 字符串数组。目录 mod 合法取值：`read_message`/`send_message`/`store_data`/`network`/`manage_settings`/`register_tool`；其它取值只作声明并给 `unknown-permission` 警告 |
| `description` | 是 | 一句话说明（插件页展示） |
| `host` | 否 | `WhaleGirlQQ` / `WhaleGirlQQBridge`；不认识的值给警告 |
| `min_host_version` | 否 | 宿主版本下限；低于当前 `version.txt` 判 `host-too-old` |
| `api_version` | 否 | 目录 mod 必须是 `1`；缺省给 `missing-api-version` 警告（挂载会失败） |
| `depends` | 否 | 其它 mod id 数组；缺依赖或成环会被拒载 |
| `enabled_by_default` | 否 | `false` 时挂上后默认停用 |

未知字段（如 `license`、`category`、`source`、`not_implemented`）一律忽略，用于兼容既有包。
