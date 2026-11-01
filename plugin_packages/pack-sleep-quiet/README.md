# 睡眠时段静默（pack-sleep-quiet 1.0.0）

## 来源
从内置插件 `src/plugins/sleep_mode.py`（「睡眠时段」）独立出来的自包含目录 mod。按北京时间
判断是否落在 `start_time`–`end_time` 窗口内（支持跨零点），命中就丢弃消息；`allow_at` 打开时
被 @ 的消息照常放行。**内置文件未改**。

## 能力
- 钩子：`on_block(message, runtime)`，返回 `True` 表示这条静默丢弃。
- 只读 `message` 字典与自己的设置，不发送消息、不落盘、不联网。
- 需要权限：`read_message`。

## 依赖
- 宿主：WhaleGirlQQ ≥ 5.2.0。
- 外部依赖：无（只用标准库 `datetime`）。

## 配置
| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 总开关 |
| `start_time` | text | `00:00` | 窗口开始，`HH:MM` |
| `end_time` | text | `07:00` | 窗口结束，`HH:MM`；小于开始时间表示跨零点 |
| `allow_at` | bool | `true` | 窗口内被 @ 时是否仍然回复 |

## 示例配置
同目录的 `config.example.json` 展示了可用的设置键和默认值，仅供对照；实际配置以控制台插件页保存的值为准。

## 安装 / 停用 / 移除
见上级目录 `README.md`。目录名必须是 `pack-sleep-quiet`。

## 已知限制
- 时区固定为 UTC+8（北京时间），没有配置项。
- 时间字符串写错时按 `00:00` 处理，不会拒绝保存（与内置版一致）。
- 与内置 `sleep_mode` 同开时两个窗口都生效，二选一启用。
