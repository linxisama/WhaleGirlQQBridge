"""插件安装生命周期的收尾：登记、重载、校验、回滚、可选启用。

以前这段逻辑散在 ``qq_app`` 的路由闭包里，单装/批量各写一份，覆盖失败时
一个删文件、一个留坏文件。抽出来后：

- **加载失败即回滚**：覆盖安装恢复旧文件，新装删除坏文件；
- 批量安装只重载一次，失败的条目不影响其他插件；
- 可选 ``enable``，加载成功后才自动开启；
- 路由只负责 HTTP 与参数校验。
"""
from __future__ import annotations

import shutil
import copy
from pathlib import Path


class PluginLoadError(RuntimeError):
    pass


def _rollback(item: dict) -> None:
    path = Path(str(item.get("path") or ""))
    backup = str(item.pop("_backup", "") or "")
    try:
        if backup:
            if not Path(backup).is_file():
                raise OSError('旧插件备份缺失，已保留当前文件供恢复')
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise PluginLoadError(f'插件文件恢复失败，请保留备份并重试：{exc}') from exc


def _cleanup_backup(item: dict) -> None:
    backup = str(item.pop("_backup", "") or "")
    if backup:
        try:
            Path(backup).unlink(missing_ok=True)
        except OSError:
            pass


def finish_install(manager, result: dict, enable: bool = False) -> dict:
    """单插件收尾。失败抛 :class:`PluginLoadError`，且磁盘已回滚。"""
    plugin_id = str(result.get("plugin_id") or "")
    state = _snapshot(manager, plugin_id)
    try:
        manager.register_custom(plugin_id)
        manager.reload()
        _verify_loaded(manager, result, enable)
    except Exception as exc:
        restore_error = None
        try:
            _restore(manager, result, state)
        except Exception as rollback_exc:
            restore_error = rollback_exc
        try:
            manager.reload()
        except Exception:
            pass
        result['loaded'] = False
        if restore_error is not None:
            raise PluginLoadError(
                f'插件 {plugin_id} 加载失败，且回滚未完成：{restore_error}；原始错误：{exc}'
            ) from exc
        raise PluginLoadError(f'插件 {plugin_id} 加载失败，已回滚到安装前状态。具体原因：{exc}') from exc
    _cleanup_backup(result)
    result["success"] = True
    result["message"] = (f"插件 {plugin_id} 已安装并已加载"
                         + ("，已自动启用" if enable else ""))
    return result


def _snapshot(manager, plugin_id):
    return {'registered': plugin_id in manager.custom_plugin_ids(),
            'settings': copy.deepcopy(manager._settings.get(plugin_id)),
            'had_settings': plugin_id in manager._settings}


def _restore(manager, item, state):
    _rollback(item)
    pid = item['plugin_id']
    if not state['registered']:
        manager.unregister_custom(pid)
    if state['had_settings']:
        manager._settings[pid] = state['settings']
    else:
        manager._settings.pop(pid, None)
    manager._save_settings()


def _verify_loaded(manager, item, enable):
    pid = item['plugin_id']
    if pid not in manager._plugins:
        problems = [str(p) for p in getattr(manager, '_load_problems', []) if pid in str(p)]
        detail = '；'.join(problems[-2:]) or '插件声明的 META.id 与安装标识不一致，或插件类无法加载'
        raise PluginLoadError(detail)
    if enable:
        # 不吞掉验证/保存异常；用户要立即使用时，不能把启用失败报成安装成功。
        manager.update_settings(pid, {'enabled': True})
    item['loaded'] = True
    item['enabled'] = bool(manager._plugins[pid].enabled)
    if enable and not item['enabled']:
        raise PluginLoadError('插件未能启用，请检查必填配置')


def finish_batch(manager, results: list, enable: bool = False) -> dict:
    """批量收尾：逐个登记、统一重载一次、失败条目回滚。"""
    installed = [item for item in results if item.get("success")]
    states = {item['plugin_id']: _snapshot(manager, item['plugin_id']) for item in installed}
    registration_errors = {}
    for item in installed:
        try:
            manager.register_custom(item["plugin_id"])
        except Exception as exc:
            registration_errors[item['plugin_id']] = str(exc)
    if installed:
        try:
            manager.reload()
        except Exception as exc:
            # reload 是全局动作；它失败时不能留下半登记、半写入的一批插件。
            for item in installed:
                try:
                    _restore(manager, item, states[item['plugin_id']])
                except Exception as restore_exc:
                    item['message'] = f'加载失败且回滚未完成：{restore_exc}'
                item['loaded'] = False
                item['success'] = False
                item.setdefault('message', f'加载失败，已回滚：{exc}')
            try:
                manager.reload()
            except Exception:
                pass
            failed = [item for item in results if not item.get("success")]
            return {"success": False, "results": results, "installed": [],
                    "failed": len(failed), "total": len(results),
                    "message": f'批量加载失败，已回滚：{exc}'}
    for item in installed:
        try:
            if item['plugin_id'] in registration_errors:
                raise PluginLoadError(registration_errors[item['plugin_id']])
            _verify_loaded(manager, item, enable)
        except Exception as exc:
            _restore(manager, item, states[item['plugin_id']])
            item['loaded'] = False
            item["success"] = False
            item["message"] = (f"插件 {item['plugin_id']} 加载失败，已回滚到安装前状态；"
                               f"具体原因：{exc}")
        else:
            _cleanup_backup(item)
            item['message'] = '已安装并已加载' + ('，已启用' if item.get('enabled') else '，未启用')
    if any(not item.get("loaded") for item in installed):
        try:
            manager.reload()
        except Exception:
            pass
    failed = [item for item in results if not item.get("success")]
    return {"success": not failed, "results": results,
            "installed": [item["plugin_id"] for item in installed if item.get("loaded")],
            "failed": len(failed), "total": len(results)}
