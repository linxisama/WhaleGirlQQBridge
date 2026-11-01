# plugin_core —— 独立插件内核参考实现（API v1）

纯标准库、零依赖、可独立运行。本包不 import 母仓任何 `src/` 模块，也不联网。

## 快速开始

```powershell
# 在仓库根跑
python -m plugin_core.cli demo                       # 竞标/胜出/落败 三行
python -m plugin_core.cli selftest --all             # T1..T10 逐项 PASS/FAIL
python -m plugin_core.cli selftest --only T8         # 单跑一项
python -m plugin_core.cli run --plugins plugin_core/examples --data .tmp/run-data --message "在吗"
python -m unittest discover -s plugin_core/tests -t .
```

## 目录

| 路径 | 作用 |
| --- | --- |
| `api.py` | 常量、异常、Bid、拒载码 |
| `manifest.py` | manifest 归一化与校验 |
| `loader.py` | 目录扫描、入口解析、加载与拒载 |
| `events.py` | 生命周期时刻与资源登记 |
| `permissions.py` | 动作 → 权限映射与校验闸门 |
| `sdk.py` | PluginAPI / Context / Draft / Action |
| `arbiter.py` | 竞标 → 仲裁 → 生成 → 仲裁记录 |
| `outbound.py` | 出站管道（改写 / 短路 / 防回灌） |
| `actions.py` | 动作执行器（逐个动作过权限） |
| `store.py` | KV 与配置落盘、卸载清理 |
| `audit.py` | 审计 JSONL 与内存日志 |
| `host.py` | Host：start / handle / reload_all / freeze_snapshot / add_plugin / uninstall |
| `mock.py` | mock 传输层、mock 模型、mock 消息源 |
| `cli.py` / `__main__.py` | 命令行入口 |
| `selftest.py` | T1–T10 自检与证据落盘 |
| `examples/` | hello / persona / filter / real 四个参考插件 |
| `tests/` | 标准库 unittest |

## 公开面

```python
from plugin_core import (
    API_VERSION, Host, PluginContext, Bid, bid, Action, Draft,
    load_plugins, normalize_message, MockTransport, MockModel, MockSource,
)
```

`load_plugins(plugins_dir, data_dir)` 只扫描并加载，不启动主体；返回 (已加载, 拒载清单)。

## 规则摘要

- 加载完立刻 freeze：运行期 `add_plugin` 抛 `FreezeViolation` 并记审计；只支持 `reload_all()` 整批重载。
- 竞标阶段不调模型；没声明 `read_message` 的插件不进入竞标。
- main 按 priority 降序，同优先级按 manifest 声明序（plugin.json 里 capabilities 的下标，不是目录名/加载序），再按 id 字典序兜底（契约 handoff/PLUGIN-CONTRACT.md:139、handoff/DECISIONS.md D7）。
- silent 与 main 同场：silent 最高 priority ≥ main 最高 priority 时 silent 胜，本轮所有 main 作废。
- attach 只在 main 胜出后按 priority 串行生成，单个失败记 `attach-failed` 不影响 main。
- extra 独立发送，但照样过出站管道；无 main 无 extra 就什么都不发，仍写一条仲裁记录（`sent: []`）。
- 出站串行、priority 小的先跑；返回 None 即取消并短路；改写结果不再进管道（防回灌）。
- 动作执行前校验权限，未声明 → 拒 + 审计 `denied`，不抛给用户、进程不崩。

## 数据落盘

```
<data_dir>/plugin_kv/<plugin_id>.json      插件 KV（键不跨插件可见）
<data_dir>/plugin_config/<plugin_id>.json  插件配置（需 store + config_write）
<data_dir>/audit/<YYYYMMDD>.jsonl          审计
<data_dir>/session.json                    会话（重启后接着聊）
```

## 诚实边界

这是同进程 Python 插件，不是安全沙箱：`network` / `admin` 只能靠契约加动作层校验协作式限权，
插件若直接 `import requests` 内核拦不住。内核侧的兜底是 `selftest --only T5` 的静态扫描。
