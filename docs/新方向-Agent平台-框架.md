# 新方向：Agent 平台（用户 2026-10-25 定调）

## 一、用户原话要点（不可改写）
1. 「关于 agent 的方面输出方案的那个窗口删掉」——控制台里「输出方案 / 逐步确认」的那块 UI 不要了。
2. 「单独留有 agent 的这个窗口，而 agent 的窗口也可以删掉。但我们最好不删掉，因为 agent 是主体的一部分」——agent 窗口保留，它是产品主体。
3. 「这个控制台自带的 agent 要有控制电脑的权限」。
4. 「也可以直接调用与本地的 agent 相连接……bot 调用的 API 的 agent 窗口是独立的，而本地的 agent 连接后会在本地新开一个文件夹（工作区）来帮助 bot 的 agent 进行更好的记忆管理」。
5. 「bot 的 agent 的工作不仅仅是统筹全局，而是要有人设的转变以及对记事本的修改权限（也就是对本地电脑的修改权限）」。
6. 「在 bot 的 agent 界面聊天的时候 agent 也可以以不同的人设进行聊天，然后把相关的记忆同步到记忆库中，从而更好的塑造记忆库和学习」。
7. 「QQ 这边认定唯一的一个主人，在和主人聊天的时候这个机器人可以从后台对电脑进行一系列的操作」。
8. 「其他功能都可以简化掉，我们做的更像是一个带有成熟系统的 agent 平台，技能全是通过插件导入的。我们做的不是功能的实现，而是对插件的更好的兼容性，我们只需要去确保插件能够正常导入和使用就可以了」。

## 二、由此确定的产品定义
**这不是「QQ 机器人 + 一堆功能」，而是一个 agent 平台**：
- 主体 = 控制台自带的 agent（有人设、有记事本/文件写权限、能调电脑）。
- 能力来源 = **插件导入**（512 扩展 / 376 skill）。我们的工作是**兼容性与生命周期**，不是替插件实现业务。
- QQ = 一个输入通道，**只认一个主人**；主人在 QQ 里说话，等价于授权 agent 在后台操作这台电脑。
- 本地 agent（外部）可接入，落在一个**独立工作区目录**里做记忆管理。

## 三、三层结构（解读）
```
控制台 GUI
 ├─ agent 对话窗口（主体，保留；删掉「输出方案/逐步确认」那块）
 ├─ 插件/技能市场（安装、启用、层级、阻塞原因）← 兼容性主战场
 └─ 简化：其余面板入口收敛
主体 agent（控制台自带）
 ├─ 统筹：把用户意图拆成动作 / 调插件工具
 ├─ 人设：窗口内可切换，人设参与对话与记忆归属
 ├─ 记事本 / 文件：对本地电脑有写权限（边界见下）
 └─ 记忆：写入统一记忆库（unified_memory），供学习与召回
本地 agent 桥（外部进程）
 ├─ 独立 agent 窗口/API，与主体 agent 解耦
 ├─ 连接后在本地新建工作区目录（记忆、草稿、人设文件都落这里）
 └─ 协议：还没定（见第六节 Q1）
QQ 通道
 └─ 唯一主人 → 后台电脑操作（受权限边界与审计约束）
```

## 四、要做的（按依赖顺序）
1. **删方案 UI**：`src/console_agent.py` 的 `parse_plan` / `validate_plan` / `describe_plan`（:144/:418/:544）与对应前端卡片下线； 保留撤销（`:718 _undo`）与危险动作一次性确认（否则改电脑没有回滚）。
2. **主体 agent 接工具**：把 `src/tool_loop.py` 的 `ToolRegistry` + `respond_with_tools` 接进 `ConsoleAgent`（现在 grep `tool_loop` 命中 0），工具来源 = 内置 + 插件工具（`qqagent_bridge.list_tools`）+ skill 工具（`skill_tools.register_skills`）；开关默认关。
3. **电脑操作权限**：新增工作区概念 `<data>/agent_workspace/<agentId>/`（记事本、人设文件、草稿），写操作默认限定在此；越界需主人显式授权并写审计。
4. **人设参与对话**：窗口内切人设 → 记忆按人设打 scope 写进 `src/unified_memory.py`；召回时按人设加权。
5. **本地 agent 桥**：协议 + 工作区目录约定 + 连接管理（面板/API），与主体 agent 解耦。
6. **插件兼容性收口**：512 扩展 / 376 skill 的导入、启用、层级、阻塞原因、工具 schema、依赖加载——这才是 KPI，不再新增业务功能。

## 五、要删 / 要简化
- 删：agent 的「输出方案/逐步确认」UI 与相关数据流（保留撤销）。
- 简化：与 agent 平台无关的 GUI 面板入口（**先只隐藏，不删代码**，可逆）。
- 不动：已验证绿的全量 3514 例测试基线、发布白名单、C1–C21 契约。

## 六、已定四项（用户 2026-10-25 拍板，不再讨论）

| 问题 | 裁决 | 落地含义 |
| --- | --- | --- |
| Q1 本地 agent 接谁 | **B 通用桥** | 定义 AgentBackend 接口，DSH 与任意命令行 agent 都是适配器；新增后端不改调用方 |
| Q2 电脑权限边界 | **A 工作区限定** | 写操作默认落 <data>/agent_workspace/<id>/；越界需主人显式授权 + 写审计 |
| Q3 方案窗口删到哪 | **A 删确认留撤销** | 下线 parse_plan/validate_plan/describe_plan 与前端卡片；_undo 与危险动作一次性确认保留 |
| Q4 记忆归属 | **A 同库分 scope** | 记忆条目带 scope={channel, persona}；召回允许跨 scope，同 scope 加权 |

- **Q1 本地 agent 接谁**：只接 DeepSeek Harness（已有 whale_bot MCP + profile 注册经验）／抽象成通用桥同时支持 DSH 与任意命令行 agent／只定协议不实现后端。
- **Q2 电脑权限边界**：默认限定 agent 工作区、越界要显式授权／直接整机读写／只读 + 写要确认。
- **Q3 方案窗口删到哪**：删方案预览与逐步确认、保留撤销与一次性确认／全删只留日志／保留但默认折叠。
- **Q4 记忆归属**：bot agent 的窗口聊天记忆与 QQ 主人聊天共用同一库同一 scope／分两个 scope、允许交叉召回／完全隔离。

## 七、对现有资产的映射（明天动手时照着改）
| 现有 | 新方向里的位置 |
| --- | --- |
| `src/console_agent.py`（1295 行，plan/action 型） | 主体 agent 的骨架；**保留**，去掉 plan 输出、接上工具 |
| `src/tool_loop.py`（ToolRegistry/respond_with_tools） | 主体 agent 的工具底座（现在只服务 QQ 回复链路） |
| `src/unified_memory.py` + `memory_api.py` | 记忆库；人设 scope 与学习都往这里写 |
| `src/agent_scheduler.py` 的 `ACTIONS`（7 种周期任务） | 周期任务保留，但不再作为 agent 的能力白名单 |
| `src/agent_sessions.py` / `agent_learning.py` | 会话与学习队列，保留 |
| `src/local_agent/`（contracts/broker/policy/runtime/windows_executor/console，7 文件 62KB） | **原项目已有的本机 Agent 权限域**：可信信封 + 动作白名单 + 审批令牌（参数哈希/设备/主体/有效期/策略版本）+ 防重放 + 审计 + L0/L1 执行器。W3/W6 直接复用，不另造 |
| `tools/whale_bot_mcp.py` + `src/bot_control_api.py` | DSH 侧的 14 个工具；本地 agent 桥可复用这套 HTTP 客户端 |
| `src/qqagent_host.py` / `src/extension_host.py` / `src/plugins/qqagent_bridge.py` | 插件兼容性主链，**本次不动**，只做验收 |
