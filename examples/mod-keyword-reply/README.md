# mod-keyword-reply · 目录 mod 样例

一个可以直接拷贝安装的目录 mod：消息包含关键词时直接回预设文案，不经过 AI。
行为与内置插件 `src/plugins/keyword_reply.py` 对齐（同一套规则格式、同样的命中文案）。

## 目录结构

```
mod-keyword-reply/
├── plugin.json        # 清单：id 必须等于目录名（manager.py:463），entry 指向入口文件
└── keyword_reply.py   # 入口：PluginBase 子类，自己声明 META / config_schema
```

## 四步

1. **装**：整个目录拷到 `<data_dir>/custom_plugins/mod-keyword-reply/`，`manager.reload()`。
   管理器扫到 `plugin.json` 就自动挂载，不需要写进 `custom_plugins.json`。
2. **跑**：在插件设置里打开「启用」并填规则（`关键词=回复` 每行一条），匹配的消息直接回这句。
3. **禁**：把「启用」关掉，匹配的消息不再回复（插件仍在表里）。
4. **摘**：删掉整个目录再 `reload()`，插件从表里消失，其它插件不受影响。

## 规则格式

与内置插件同一套编解码（`src/keyword_rules.py`）：

- 旧版逐行 `关键词=回复`
- 版本化记录：`{"format":"keyword_reply/v1","rules":[{"keyword":"...","reply":"..."}]}`

注意：`from ..keyword_rules import ...` 之所以能跑，是因为经理把加载模块的
`__package__` 指向 `src.plugins`；仓库外的第三方 mod 没有这个包上下文，
需要把那段编解码逻辑抄进自己目录（或等 ModRuntime 暴露规则编解码）。
