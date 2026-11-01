# 连发合并（pack-burst-merge 1.0.0）

## 来源
从内置插件 `src/plugins/burst_merge.py`（「连发合并」）独立出来的自包含目录 mod。同一发送者
在合并窗口内再发一条，就给本次 AI 调用加一个 `delay`，等对方说完再一起回。**内置文件未改**。

## 能力
- 钩子：`on_pre_ai(message, conversation, runtime)`，只返回 `{"delay": 秒}` 或 `None`。
- 不改对话历史、不发消息、不读设置以外的任何东西。
- 需要权限：`read_message`。

## 依赖
- 宿主：WhaleGirlQQ ≥ 5.2.0。
- 外部依赖：无（只用标准库 `time`）。

## 配置
| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 总开关 |
| `cap_ms` | int | `8000` | 合并窗口（毫秒），钳在 500–30000 |
| `quiet_ms` | int | `600` | 记录"对方静默多久算说完"的参考值，宿主侧决定实际等待 |

## 示例配置
同目录的 `config.example.json` 展示了可用的设置键和默认值，仅供对照；实际配置以控制台插件页保存的值为准。

## 安装 / 停用 / 移除
见上级目录 `README.md`。目录名必须是 `pack-burst-merge`。

## 已知限制
- 只对群聊生效（私聊不需要等对方"发完"）。
- 与内置 `burst_merge` 同开会让等待叠加，二选一启用。
- 已经带 `_burst_handled` 标记的消息不再等待，避免二次延迟。
