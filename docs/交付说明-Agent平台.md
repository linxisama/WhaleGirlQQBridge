# 本轮交付说明 · Agent 平台（W1–W13）

本文件对应用户新方向：**把控制台做成一个能自己干活的 agent 平台**，删掉「先出方案再确认」的旧交互，
给主体 agent 加上工具权限、记事本（本地文件）权限、记忆分通道，并把本地 agent 接进来。
任务书见 `docs/新方向-任务书-Agent平台.md`，框架说明见 `docs/新方向-Agent平台-框架.md`。

## 一、一句话结论

控制台的 agent 从「只会说话、给你出方案」变成「能自己调工具、能写自己的记事本、记忆按通道隔离」；
本地 agent（DeepSeek Harness、任意命令行 agent）能从「外部后端」页加进来并各开一个自己的工作区；
QQ 侧只有唯一主人能在私聊里让机器人操作电脑，且必须回一次性 6 位确认码。所有写盘动作都有审计，
默认全部关闭（`ai.agent_tools: false`），关着时的行为与改造前逐字一致。
另外补上了「夜间固化」（把近几天日记与记忆聚类写成 `soul` 层记忆）与「自画像」（重写 `我是谁.md` 并同步成一条 `soul` 记忆），两者都是定时任务里的动作、默认关闭。
W8–W13 把主线压在「导入 + 兼容 + agent」上：技能侧放开 T2 与 `node:child_process`（可注册 57 包 / 153 工具）、宿主给技能注入 `fetch`、宿主子进程有了独占 cwd、
外部后端能一键识别本机 DSH、插件/技能导入统一到一个识别内核与统一 schema、弹层不再黑屏、**新增日志页（报错可见 / 可复制 / 可导出）**。

## 二、各工作包各自落了什么

| 包 | 做了什么 | 主要落点 |
| --- | --- | --- |
| W1 删方案 UI | 下线「方案预览 / 逐步确认」，改成对话流 + 「允许这次操作」「撤销这次修改」；危险动作（service / open_path / open_url / type_text / hotkey / screenshot）仍然停下来要一次确认 | `src/console_agent.py`、`src/gui/assistant.py` |
| W2 工具环 | agent 可以自己一轮轮调工具（工作区读写 / 插件工具 / skill 工具），复用既有 `src/tool_loop.py` 注册表与审计；默认关闭 | `src/agent_tools.py`、`src/console_agent.py:568` |
| W3 工作区 | 每个 agent 一个工作区目录，写操作默认只能在界内；越界要一次性授权令牌，越界写审计失败就**拒写** | `src/agent_workspace.py` |
| W4 记忆 scope | 记忆条目带 `{channel, persona}`，同通道同人设加权召回；跨通道可召回但群聊永不读到控制台记忆 | `src/unified_memory.py`、`src/qq_app.py:772` |
| W5 本地 agent 桥 | 后端抽象（DSH / 通用 CLI 两种适配器）+ 端点 + 「外部后端」页；每个后端各开工作区子目录 | `src/agent_backends.py`、`src/agent_backend_api.py`、`src/gui/agent_panel.py` |
| W6 QQ 主人 + 收敛 | 主人私聊命令走「记事本 / 列工作区 / 读文件」，危险动作回一次性确认码；侧栏收敛到 7 项 +「更多面板」；设置页新增「Agent 工具环」三键 | `src/agent_qq_ops.py`、`qq_gui.py`、`src/gui/agent_settings.py` |
| W7 夜间固化 + 自画像 | 每晚把近 3 天日记 / 记忆聚类固化成 `soul` 层记忆；把「我是谁」重写成 `<data>/我是谁.md` 并同步成 `soul` 记忆；两者都是定时任务里的动作，默认关闭 | `src/agent_dream.py`、`src/agent_portrait.py`、`src/agent_scheduler.py` |
| W8 技能兼容 | `DEFAULT_TIERS` 加 T2、`node:child_process` 直接放行并记账、宿主视为已提供 `fetch`：可注册 27 包/77 工具 → **57 包/153 工具** | `src/skill_tools.py`、`runtime/qqagent-host/host-one.mjs` |
| W9 后端可发现 | DSH 自动识别（`detect()` 永不抛，探 `--version`，headless 标推荐）+ 后端支持 `{prompt}` 占位参数；全局弹层样式（菜单/提示框/文件框不再黑屏），侧栏不再把面板藏进「更多面板」 | `src/agent_detect.py`、`src/agent_backend_api.py`、`src/gui/menu_style.py`、`qq_gui.py` |
| W10 运行期 CTX | 宿主给每次技能调用开独占 cwd（`<data>/qq-only/extension_cwd/<pid>-<随机>/`，成功/失败/超时都删），相对路径不再写到仓库根 | `src/extension_host.py` |
| W11 统一导入 | 新增识别内核：`detect()` / `inspect()` / `install()` 吃四类包（py 插件 / JS 扩展 / 技能 / 功能包），941 包识别 `unknown=0`；Python 安装器认 B1 方言（50 包预检 7→49 通过）；宿主加载垫片（入口回退、`enabledByDefault` 归一、CJS 中文诊断） | `src/pack_import.py`、`src/plugin_installer.py`、`runtime/qqagent-host/src/plugin-loader.js` |
| W12 外部后端页 | 无选中时按钮置灰 + tooltip、点击即有「正在探测…」中文进度、空状态三步引导、候选框命令列不截断且异步补版本、保存后选中新行、状态列绿灰红、表单显示「正在编辑：<id>」 | `src/gui/agent_panel.py` |
| W13 日志 | 结构化日志（错误优先 + 关键事件，不记聊天正文）：桌面新页 + 网页第 5 个 tab，可按级别/来源/关键词筛选，单条与全部复制、导出 `.json` | `src/app_log.py`、`src/log_api.py`、`src/gui/logs_page.py`、`src/web/static/qq.{html,js}` |

## 三、怎么验

~~~bash
cd "D:/qq机器人5.0"
PY="D:/聊天机器人/qq机器人/.venv-build/Scripts/python.exe"   # GUI 用例必须用这个 venv
export QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8

# 1) 终局全量回归（实测 2478–3204 秒 ≈ 41–54 分钟，GUI 用例占大头；4371 例）
"$PY" -m unittest discover -s tests -p "test_*.py"

# 2) 本轮的定向模块（约 3 分钟）
"$PY" -m unittest tests.test_agent_workspace tests.test_agent_workspace_audit \
  tests.test_agent_tools tests.test_agent_tools_loop tests.test_agent_tools_config_persist \
  tests.test_agent_no_plan tests.test_memory_scope tests.test_memory_scope_wiring \
  tests.test_agent_backends tests.test_agent_backends_cli tests.test_agent_backend_api \
  tests.test_agent_panel tests.test_agent_qq_ops tests.test_agent_qq_owner tests.test_gui_convergence \
  tests.test_agent_dream tests.test_agent_portrait tests.test_agent_growth_settings \
  tests.test_agent_growth_ui tests.test_scheduler_growth tests.test_agent_scheduler \
  tests.test_pack_import tests.test_pack_import_install tests.test_plugin_installer_dialects \
  tests.test_host_loader_shims tests.test_host_fetch_ctx tests.test_host_dependencies \
  tests.test_extension_host_cwd tests.test_agent_detect tests.test_agent_panel tests.test_agent_panel_ux \
  tests.test_gui_popup_style tests.test_app_log tests.test_log_api tests.test_logs_page tests.test_web_logs_tab

# 3) 构建产物 + 冒烟
"$PY" .tmp/accept/build_and_check_5_0.py
"$PY" .tmp/accept/smoke_release_5_0.py .tmp/release-final
~~~

界面上手验：控制台 → 设置 → 「Agent 工具环」勾上（重启控制台后生效）→ 对话里让它「把今天的结论记到记事本」→
看 `<data>/agent_workspace/<agentId>/` 里出现文件；「外部后端」页加一条 DSH → 探针 → 启动。

## 四、配置与文件落在哪

| 东西 | 位置 | 说明 |
| --- | --- | --- |
| `ai.agent_tools` | `config.qq.yaml` | 默认 `false`；界面在 设置 → Agent 工具环 |
| `ai.agent_tools_max_rounds` | 同上 | 1..5，默认 3 |
| `ai.agent_tools_timeout` | 同上 | 1..60 秒，默认 20 |
| agent 工作区 | `<data>/agent_workspace/<agentId>/` | 界面「外部后端」页可看 `workspace()` |
| 工作区写审计 | `<data>/qq-only/agent_workspace_audit.jsonl` | `ts/agent/op/path/ok/code/principal/param_hash`，不写正文，8MB 轮转 |
| QQ 主人操作审计 | `<data>/qq-only/qq_ops_audit.jsonl` | `code-issued/code-used/code-replay/code-invalid/code-expired/denied` |
| 后端清单 | `<data>/agent_backends.json` | `{"version":1,"backends":[{id,kind,profile}]}` |
| `ai.dream_enabled` | `config.qq.yaml` | 默认 `false`；夜间固化总开关（设置页「Agent 成长」） |
| `ai.dream_lookback_days` | 同上 | 1..30，默认 3：回看几天素材 |
| `ai.dream_max_clusters` | 同上 | 1..9，默认 3：每晚最多固化几簇 |
| `ai.dream_rate` | 同上 | 0..1，默认 0.35：概率门，1 表示每跑必固化 |
| `ai.portrait_enabled` | 同上 | 默认 `false`；自画像重写开关 |
| 自画像正文 | `<data>/我是谁.md` | UTF-8 无 BOM，超 4000 字截断 |
| 自画像历史 | `<data>/我是谁-history/<YYYYMMDD-HHMMSS>.md` | 每次覆盖前快照旧版，同秒追加 `-2` 不覆盖 |
| 固化去重账本 | `<data>/qq-only/dream-seen.json` | `{"version":1,"keys":[]}`，最多 4000 条 |
| 日志落盘 | `<data>/qq-only/logs/app-<日期>.jsonl` | 每行一条 JSON；10MB 滚动、留 3 份；界面只看最近 1000 条 |
| 技能调用 cwd | `<data>/qq-only/extension_cwd/<pid>-<随机>/` | 每次调用独占、结束即删（SIGKILL 强杀时可能残留） |
| 自画像默认背景 | `assets/backgrounds/default.jpg` | 2560×1440，首次启动复制到 `<data>/backgrounds/`；已有自定义背景不覆盖 |

**界面上单独落盘的键不会被后端覆盖写回抹掉**（现为 8 个）：后端保存人设 / 违禁词时会把磁盘上这三个键先合并回来
（`src/qq_app.py` 的 `GUI_OWNED_AI_KEYS` + `_preserve_gui_owned_ai`）；反向也安全，界面每次保存前都重读磁盘。

## 五、HTTP 端点（仅回环 + 同源，双层鉴权）

`GET /api/agent/backends`（列表 + live + workspace）、`POST /api/agent/backends`（增改 profile）、
`DELETE /api/agent/backends/{id}`、`POST /api/agent/backends/{id}/probe`、`/start`（202 + 后台线程）、
`/stop`、`GET /api/agent/backends/{id}/status`、`GET /api/agent/backends/{id}/workspace?path=`、
`POST /api/agent/backends/shutdown`（进程收尾时用）、`GET /api/agent/backends/detect?probe=0|1`（扫描本机 DSH，`probe=1` 才起进程取版本）。

日志三条：`GET /api/logs?level=&source=&query=&limit=&since=`、`GET /api/logs/export`（附件 `app-log-<YYYYMMDD-HHMMSS>.json`）、
`POST /api/logs/client`（前端把浏览器侧报错也记进来）。三条全走 `_require_local`，非回环一律 403 `auth_denied`。

## 六、QQ 侧只有主人能用

私聊 + 已绑定主人两个条件都满足才进 `OwnerOps`；群里发这些词一律拒绝；
危险动作要先 `request()` 领 6 位码（120 秒有效、一次性、错误码不消耗令牌），主人私聊回码才执行。
群聊里拿不到码、控制台记忆也永远不出现在群聊提示词里。

## 七、夜间固化 + 自画像（W7 追加）

两项都挂在既有「定时任务」里，不另外装 Windows 计划任务：`ACTIONS` 里分别是 `dream_consolidate`（夜间固化记忆）与 `self_portrait`（重写自画像）。
GUI 新建任务的类型下拉与 `src/console_agent.py:53` 的提示词都从 `src/agent_scheduler.py:27 action_choices()` 这个唯一真源取，不再各写一份。

- **夜间固化**（`src/agent_dream.py`）：素材 = `<data>/日记.md` 近 N 天（先按空行分段，超 300 字的段再按 `。！？；…` 切，单句本身超长就硬切）+ 记忆里近 N 天的非 `soul` 条目。
  聚簇 = 相似度 ≥ 0.3 或存在 ≥ 4 字连续公共片段；每晚上限 `max_clusters` 簇；`rate` 概率门；写完记 `<data>/qq-only/dream-seen.json` 去重（同一天同一句不会被反复固化）。
  `importance = min(100, 40 + 15 × 提及次数)`，只写 `layer='soul'`，既有 fact/lesson/project 行只读不写。
- **自画像**（`src/agent_portrait.py`）：素材 = 现有「我是谁」正文 + `soul`/`user` 层记忆摘要 + 日记尾巴；模型重写后原子写盘（tmp + `os.replace`）、旧版先快照、正文 < 80 字判 `too_short` 不覆盖；
  正文同时 upsert 成一条 `source='portrait'`、`importance=95` 的 `soul` 记忆（恒只有 1 条）。定时任务的成功文案会区分「首次生成」与「旧版已快照」。

开关关着的默认行为：跑这两项只会得到「夜间固化未启用」「自画像开关关着」的文案，不写文件、不动记忆。

手动验（不用等定时）：控制台 → 助手 →「定时任务」→ 类型选「夜间固化记忆」或「重写自画像」→ 添加 → 选中 →「立即运行」。

## 八、日志（W13：报错看得到、能复制、能导出）

- **落点**：`src/app_log.py`（`class AppLog`、`MirrorHandler`、`install_logging_handler`）。条目形状固定：
  `{"id":"<sha1前16>","ts":"<iso>","level":"error","source":"qq","message":"≤500","detail":"≤8000","code":"","extra":{}}`，
  `level` ∈ `debug|info|warning|error`，`source` ∈ `qq|console|agent|extension|skill|dsh|web|gui|backend|import`。**只记错误与关键事件，不记聊天正文。**
- **写盘**：`<data>/qq-only/logs/app-<日期>.jsonl`，单文件 10MB 滚动、保留 3 份；内存环形缓冲留最近 1000 条给界面。
- **桌面页**：`src/gui/logs_page.py`（侧栏「管理」组第 7 项「日志」）。级别/来源下拉 + 关键词框 + 表格 + 详情面板；按钮「复制这条」「复制全部」「导出 .json」「清空列表」；5 秒自动刷新，请求走 `PanelRequests` 工作线程（忙时排队，不卡界面）。
- **网页**：`src/web/static/qq.html` 第 5 个 tab。`qq.js:42 panels` 数组必须同步加 `'logs'`（tab 按下标映射，漏一个就全错位）。
- **导出**：`GET /api/logs/export` 返回 `{"version":1,"exported_at":…,"filters":{…},"count":N,"entries":[…]}`，UTF-8 无 BOM，文件名带服务器时间戳。
- **已挂钩的关键事件**：后台启动/停止（`src/qq_app.py:3119/:3121`）、运行时失败（`:699`）、模块加载错误（`:1599`）、导入成功（`src/import_api.py:267`）。
- 手动验：桌面侧栏「日志」→ 下拉切「错误」应只剩错误；「导出 .json」应下载到 `app-log-*.json`；网页控制台第 5 个 tab 同款。

## 九、已知边界（诚实清单）

1. 工具环默认关闭；关着时与改造前完全一致（这是有意的，防止一升级就乱动文件）。
2. 确认码只有程序 API `request()` 能发起；要让模型自己触发截屏类动作，还得把 `agent_tools.py` 接上（下一轮）。
3. 确认码是 6 位明文、120 秒、无速率限制；越权尝试全部记审计但不阻断（不做封锁是为了不把主人关在门外）。
4. 工作区越界令牌只活在进程内存，重启即失效；跨进程不共享（跨进程要落盘 approvals，等下一轮评估）。
5. 后端 `stop()` 用 `taskkill /T /F` 连树杀，SIGKILL 场景下可能残留；面板 live 状态是进程内的，没有文件锁。
6. 界内写审计仍保持「审计失败不阻塞写」的老语义；只有越界写是硬失败（拒写）。
7. `src/memory_api.py:42` 调 `reflect()` 未传 scope，落默认 `console`——语义正确，本轮不改。
8. 迟到的 GUI 回调加固：`qq_gui.py` 的 `_apply_backdrop` 那个 100ms 单次定时器改成自己取 `winId()` 并吞掉 `RuntimeError`，窗口先销毁时不再让 PyQt 槽抛错 abort 进程。
9. 全量回归的跨模块定时器污染修了两处：`tests/test_gui_convergence.py` 建窗口改走 `_dispose` + `_flush_deletes`（停 `_timer`/`pc_tab.timer`/`workbench_tab._status_timer`/`hang_watchdog._timer`/`_hang_notice_timer` 再 deleteLater 并派发 DeferredDelete）；`tests/test_desktop_reliability.py` 的慢桩保持时间 30s→300s（主线程被别的模块阻塞 30s 以上会让被测任务自然结束），并新增 1 例云母迟到回调回归。
10. `soul` 层记忆（含 dream 与 portrait 条目）在**群聊**里读不到：`context_for_prompt(private=False)` 一律跳过 `soul`/`user` 层。控制台与 QQ 主人私聊可见，群聊只剩 `rules`；这是 W4 定的隐私规则，本轮未改。
11. `agent_dream` 写 `channel=''` 会被 `_norm_channel` 归一成 `'console'`、未知 `source` 会被统一改写成 `'desktop'`，所以 dream / portrait 条目的身份只能靠 `tags`（`['dream']` / `['portrait']`）认，别按 `source` 过滤。
12. `agent_portrait.refresh()` 只吃同步 `generate`（返回 awaitable 会得到 `generate_async_unsupported`），所以调度器是「先 `await engine.chat()` 拿到正文，再把正文当 `generate` 的结果喂给 refresh」；`write_portrait()` 的 `source` 默认 `'dream'` 且 `refresh` 没传，自画像条目同样只靠 tags 认。
13. 定时任务在 `run_due` 里串行 `await`，自画像最长 90 秒，会拖住同一轮其它任务；`agent_schedules.json` 落在 `config_path` 的父目录，与 `data_root` 无关（多账号场景是既有问题）。
14. 日志只收错误与关键事件，**不记聊天正文**（用户裁决）；想追某次对话要自己加 `extra`，否则只有摘要。
15. 本机 FastAPI 0.143 的 `app.get` 不自动补 HEAD，导出路由必须显式写 `methods=['GET','HEAD']`，否则 HEAD 返 405（有些下载器会先发 HEAD）。
16. `src/pack_import.install()` 装完不立即执行 py 插件（`load.code='post-install-check'`），JS 扩展的 `enable` 只记 `enable-ignored`；统一端点与统一对话框是 W11-4b 的事。
17. 宿主仍不支持 CommonJS 入口真正运行（`type=module` 的根下会报 `cjs-unsupported` 并给中文改法）；`enabledByDefault:false` 的包占了相当大的比例，装了会被明确告知「已安装但未生效」，默认值不改。

## 十、回滚

W1–W6 的改动都留了备份：`.tmp/backup-w1/`、`.tmp/backup-w2/`、`.tmp/backup-w4(b)/`、`.tmp/backup-w5b/`、`.tmp/backup-w6/`。
单点回滚最省事的是把 `ai.agent_tools` 关掉（等于回到改造前的对话路径），必要时再用备份覆盖对应文件。

W7 没有单独备份：新增的 `src/agent_dream.py`、`src/agent_portrait.py`、`tests/test_agent_dream.py`、`tests/test_agent_portrait.py`、`tests/test_agent_growth_settings.py`、`tests/test_agent_growth_ui.py`、`tests/test_scheduler_growth.py` 直接删掉即可；
对既有文件的改动只有四处小改——`src/agent_scheduler.py`（`ACTIONS` 两项 + `action_choices()` + `run_now` 两个分支）、`src/gui/assistant.py`（类型下拉改走 `action_choices()` + 一行提示文案）、`src/console_agent.py:53`（一行提示词）、`src/qq_app.py:104-106`（`GUI_OWNED_AI_KEYS` 8 键）。
只想关功能不动代码：`ai.dream_enabled` 与 `ai.portrait_enabled` 都设 `false`。

W8–W13 同样没有单独备份，回滚按文件来：
- W8：`src/skill_tools.py` 的 `DEFAULT_TIERS` 去掉 `'T2-web-fetch'`、把 `node:child_process` 移回 `NODE_BUILTIN_GATED` 即回到 27 包/77 工具。
- W9：删 `src/agent_detect.py`、`src/gui/menu_style.py`，`qq_gui.py` 去掉 `app.setStyleSheet(POPUP_STYLESHEET)` 即回到旧弹层（会重新出现黑底菜单）。
- W10：`src/extension_host.py` 里 `call_cwd` 相关四处不传 `cwd` 即回到旧行为（技能相对路径会再落到仓库根）。
- W11：删 `src/pack_import.py`；`src/plugin_installer.py` 与 `runtime/qqagent-host/src/plugin-loader.js` 从 `.tmp/backup-w11*/` 覆盖（无备份时用 git 里的上一版）。
- W13：删 `src/app_log.py`、`src/log_api.py`、`src/gui/logs_page.py`，并在 `src/qq_app.py` 去掉 `log_api.register` 那一行、`qq_gui.py` 去掉日志 tab；网页侧删 `qq.html` 的 `#tab-logs` 段与 `qq.js:42` 里的 `'logs'`（两处必须同时删，否则 tab 错位）。
