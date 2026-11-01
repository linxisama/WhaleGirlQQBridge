# 新方向任务书：Agent 平台（v1）

配套文档：docs/新方向-Agent平台-框架.md（用户定调与四项裁决）。

## 1. 目标态一句话
控制台自带的主体 agent 能**调工具、改工作区文件、按人设记忆**；外部本地 agent 以**通用后端**方式接入并各有独立工作区；插件只负责被正确导入与调用。

## 2. 不可破契约（K1–K7）
- **K1 工具注册键**：一切工具经 src/tool_loop.py 的 ToolRegistry 注册，键为运行时全名（沿用 C1）；主体 agent 不得绕过注册表直调插件。
- **K2 文件写边界**：agent 的写操作默认只能落 `<data>/agent_workspace/<agentId>/ `；越界必须持一次性 grant 令牌，且每次写入写审计行（jsonl）。
- **K3 记忆 scope**：写入必须带 scope={channel:'qq'|'console'|'local', persona:<name>}；召回允许跨 scope，同 scope 权重高于跨 scope（权重为常量，写死可测）。
- **K4 后端抽象**：本地 agent 只经 AgentBackend 接口（connect/send/events/workspace_dir/close）；新增后端只加适配器。
- **K5 删 UI 不动安全网**：删方案预览与逐步确认，但 _undo 与危险动作一次性确认必须保留且有用例。
- **K6 插件兼容性链路冻结**：宿主根物化、registry 权威、probe 门禁、依赖加载本轮不改，只跑验收。
- **K7 默认关闭**：主体 agent 的工具环由配置开关控制，默认 false；打开才加载工具。

## 3. 工作包

### W1 删方案 UI（无依赖，可先做）
- 目标：控制台 agent 只保留对话窗口；"输出方案/逐步确认"整条数据流下线。
- 写域：src/console_agent.py、qq_gui.py、tests/test_agent_*、tests/test_console_agent*。
- 要点：parse_plan/validate_plan/describe_plan 与前端卡片删除；_undo/_undo_one 与危险动作一次性确认保留；旧会话里的 plan 字段要能容忍（不能因缺字段崩）。
- 验收：grep 证明 UI 入口 0 命中；撤销与确认用例绿；全量回归不红。

### W2 主体 agent 工具环（依赖 W3 的 workspace 工具）
- 目标：ConsoleAgent 能多轮调工具（内置 + 插件 258 + skill 77 + 宿主自带）。
- 写域：src/console_agent.py、新建 src/agent_tools.py、tests。
- 要点：复用 tool_loop.ToolRegistry/run_calls 与审计；开关默认关；轮数与超时沿用 ai.tool_loop_* 口径；工具结果要回到对话上下文。
- 验收：真机一次"读我的记事本 → 追加一行 → 回读"闭环；插件工具（如 forward-reader__expand）与 skill 工具（wiki-query__wiki_query）各真调一次成功。

### W3 工作区与文件权限（W1 可并行）
- 目标：agent 有记事本与文件读写，边界可证。
- 写域：新建 src/agent_workspace.py（+tests）、src/console_agent.py 只接入。
- 要点：目录约定 `<data>/agent_workspace/<agentId>/`（notes.md/notes/、persona/、drafts/、transcripts/）；API：resolve/read/write/append/list/grant/audit；越界读写一律返回结构化拒绝并记审计。
- 验收：边界用例（.. 穿越、绝对路径、符号链接、超限大小）+ 真机写 notes.md 成功 + 越界被拒且审计有行。

### W4 人设与记忆 scope
- 目标：窗口内切人设，对话与记忆按人设归属，可交叉召回。
- 写域：src/unified_memory.py、src/agent_sessions.py、src/console_agent.py、tests。
- 要点：记忆 schema 加 scope；写入路径统一；召回按 scope 加权；旧数据无 scope 视为 channel='console', persona='default'。
- 验收：同一人设在 console 与 qq 两侧写入后，任一通道都能召回；不同人设召回不串味（用例）。

### W5 本地 agent 通用桥
- 目标：外部本地 agent（先 DSH，再任意 CLI）连上后各有一个工作区目录做记忆。
- 写域：新建 src/agent_backends.py（AgentBackend + DshBackend + CliBackend）、重写 src/local_agent_bridge.py、src/qq_app.py 端点、qq_gui.py 面板、tests。
- 要点：后端 profile（命令/参数/workdir/env/超时/协议）；连接生命周期与结构化错误；工作区复用 W3 的约定，键为 backendId；干跑（dry-run）验证命令可执行。
- 验收：DSH 后端真连一次（可复用 whale_bot MCP 的 HTTP 客户端或 dsh CLI），CLI 后端用假命令真跑一次；工作区目录生成且可读写；断连后不残留进程。

### W6 QQ 主人后台操作 + 面板收敛
- 目标：主人在 QQ 里发话即授权 agent 在后台操作；无关面板入口收敛。
- 写域：src/qq_app.py、qq_gui.py、tests。
- 要点：主人判定复用现有；危险动作一次性确认可在 QQ 完成（确认码）；写工作区与读文件对主人开放；非主人一律拒；GUI 只隐藏入口不删代码。
- 验收：主人在 QQ 触发写工作区成功、非主人被拒有用例；GUI 入口隐藏后主窗口仍可启动。

## 4. 顺序
W1 ∥ W3 → W2 → W4 → W5 → W6。每包上桌前先跑一次全量基线（当前 3514 例 OK）。

## 5. 全局验收
- 全量回归保持 OK（当前 3514 例 / skipped 66）。
- 每包必须给真机证据（不是 mock）。
- 每包附回滚备份到 .tmp/backup-<wp>/。

## 6. 明确不做
- 不重写任何插件或 skill 的业务实现；只保导入与调用。
- 不删除插件代码，面板只隐藏。
- 不动三份 host-one.mjs（sha256 33a7e0f291e113c4 / 16746 字节 / 315 行必须一致）。
- 不默认打开工具环。

## 7. 参考实现：原项目已有 src/local_agent/（不要另造权限模型）

`D:/qq机器人5.0/src/local_agent/` 是原项目（5.0 / 规划书 1.7）已经写好的本机 Agent 包，共 7 文件约 62KB：

| 文件 | 行数量级 | 职责（取自其 docstring） | 对本轮的意义 |
| --- | --- | --- | --- |
| `__init__.py` | 824B | 声明『独立权限域』：与机器人管理/宿主控制完全分开，不复用旧宿主口令；首版只做 L0/L1 | 直接就是 K2/K6 的边界定义 |
| `contracts.py` | 8.7KB | 可信信封（调用者/QQ 账号/原始消息 ID/签名由 Broker 生成，**绝不从模型响应读取**）、动作包 `{job_id,device_id,action,args,expires_at,policy_version,nonce}`、错误码；本机共享密钥 + HMAC-SHA256 | W3 的授权令牌与审计字段照这个抄 |
| `policy.py` | 6.3KB | 动作白名单 + 资源白名单 + 短期授权 + 审计；审批令牌绑定『参数哈希+设备+主体+有效期+策略版本』，改参重审；`consume()` 防重放；解绑/急停清空 | K2 的唯一闸门，W3 直接复用 |
| `runtime.py` | 8.2KB | 独立进程状态机 + 心跳 10s（丢 3 次标离线）+ 任务取消 + 急停（下一动作前生效，不撤销已完成）+ 15s 动作预算 + 重启不重放 | W5 后端生命周期可复用其状态机语义 |
| `broker.py` | 18KB | 配对、能力查询、动作提案与回执、防重放（nonce 落库 TTL 10 分钟）；优先命名管道，回环只监听 127.0.0.1 且**回环不等于鉴权** | W5/W6 的连接层 |
| `windows_executor.py` | 11.7KB | L0 在线/能力查询；L1 打开白名单应用、设音量、暂停媒体；动作前核对窗口焦点，锁屏/UAC 返不可执行；目录授权解析最终路径拒越界/UNC/junction/symlink；**绝不执行来自 QQ 的任意命令字符串**；默认 APP_WHITELIST 为空 | W3『记事本/文件写』必须走它的路径校验规则 |
| `console.py` | 8KB | 本地人机确认（设备、待批动作、批准/拒绝、急停、审计）；批准不会被聊天/模型路径自动触发 | 与 W1『保留危险动作一次性确认』是同一件事 |

### 7.1 因此对本任务书的三处修订
- **W3 改为『复用 + 扩展』**：不新造权限模型；`src/agent_workspace.py` 只提供工作区目录与读写，**放行判定一律走 `local_agent.policy.ActionPolicy`**，审计写同一条 `agent_audit`。
- **W6 改为『接既有信封』**：QQ 主人私聊 → 生成可信信封（`contracts`）→ 策略闸 → executor；群聊/引用/转发一律不作为授权证明（原包已定死这条）。
- **W5 的 CLI/DSH 后端**：外部 agent 的『工作区目录』与 `local_agent` 的 L0/L1 动作域分开——外部 agent 只能读写自己的工作区，**不能**获得 `windows_executor` 的白名单动作；要动电脑仍须过 ActionPolicy 审批。

### 7.2 明天第一件事（按用户『参考原项目的这个 agent 并修改』）
1. 通读 `src/local_agent/` 七个模块（重点 `policy.py` 的 `authorize/consume` 与 `contracts.py` 的信封字段）。
2. 跑现有用例：`grep -rl local_agent tests/` 找到已覆盖范围，先跑一遍确认绿。
3. 再决定 W3 是『在 policy 上加工件区写动作』还是『工区写走独立轻闸 + 危险动作过 policy』——写进报告再动手。
