# WhaleGirlQQBridge / 鲸鱼娘 QQ 账号版

Windows 上的 QQ 机器人控制台：官方 QQ 机器人接入、人设与记忆、插件与插件市场、DSH（DeepSeek Harness）联动，以及带 SHA256 校验的安装版自动更新。

> 本项目只负责本机控制台和 QQ 官方机器人接入，不包含 QQ 客户端外挂、自动抢红包、批量登录等能力。

## 功能

- **QQ 官方机器人**：AppID/AppSecret 配置、私聊/群聊、沙箱与正式环境、发送门禁与限流。
- **控制台**：对话、智能中心、QQ 接入、记忆、机器人管理、日志。
- **人设与记忆**：多人设、当前人设、手动/确认/候选记忆、日记。
- **插件**：插件市场、本地插件包、独立目录、启停/移除、异常隔离、文件上传字段。
- **DSH 联动**：自动发现本机 DSH profile，登记 whale_memory / whale_bot / whale_tools 三组 MCP，新会话即可调用；连接体检会说明缺什么、是否需要重启。
- **自动更新**：GitHub Releases 检查、完整更新说明、下载进度、SHA256 校验、退出后静默安装、失败重试。
- **无开发环境运行**：安装包自带 Python 运行时和 runtime/qqagent-host 能力桥；插件宿主所需的 Node 运行时也随包提供。

## 安装

从 [Releases](https://github.com/linxisama/WhaleGirlQQBridge/releases) 下载最新 WhaleGirlQQBridge-Setup-x.y.z.exe，双击安装。默认安装到 %LOCALAPPDATA%\Programs\WhaleGirlQQBridge，不需要管理员权限，也不需要安装 Python 或 Node。

升级可直接覆盖安装；卸载默认保留 %LOCALAPPDATA%\WhaleGirlQQ 里的用户数据。

## 从源码运行

要求：Windows 10/11 x64、Python 3.12、Node.js 20+（能力桥的插件宿主需要）。

```powershell
python -m venv .venv-qq
.venv-qq\Scripts\python.exe -m pip install -r requirements.qq.txt -r requirements.gui.txt
.venv-qq\Scripts\python.exe qq_main.py
```

控制台默认端口 27499。源码模式不写入安装目录，数据仍在 %LOCALAPPDATA%\WhaleGirlQQ。

## 配置

首次启动会生成 config.qq.yaml。至少填写：

```yaml
qq:
  app_id: "你的 AppID"
  client_secret: "你的 AppSecret"
ai:
  api_url: "https://api.deepseek.com"
  api_key: "你的模型 API Key"
  model: "deepseek-chat"
```

不要把填好密钥的 config.qq.yaml 提交到仓库。仓库里的 config.example.yaml 所有密钥字段都是空的。

## DSH 接入

安装版不含 Python 源码，需要在「外部后端 → DSH 联动 → 源码仓库根」填含 tools\ 和 src\ 的源码目录（例如 D:\qq机器人5.0），也可以设 WHALE_QQ_REPO。填一次会记住。点「连接体检」可看三组 MCP 是否登记齐全、是否需要重启 DSH。

## 插件

- 内置包在 plugin_packages/<id>/，含 plugin.json、README.md、入口 .py。
- 控制台「插件 → 本地插件包」可手动安装；不会自动安装。
- 用户插件复制到 %LOCALAPPDATA%\WhaleGirlQQ\data\custom_plugins\<id>\，停用不删文件。
- 插件配置的文件字段会把文件复制到 data/plugin_uploads，不会自动上传公网。

## 测试

```powershell
.venv-qq\Scripts\python.exe -m unittest discover -s tests -t tests -p "test_*.py"
```

## 构建

见 [BUILDING.md](BUILDING.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。
