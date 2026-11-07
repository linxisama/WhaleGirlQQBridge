"""GitHub Releases 自动更新：检查、下载安装包、生成退出后执行的批处理。

只使用 GitHub 官方 Releases API 和 GitHub 托管的资产地址；不执行仓库里的任意
脚本，也不在这里 import 任何第三方代码。真正的安装交给用户已经装过的
Inno Setup 安装包（静默参数），由独立 cmd 进程在控制台退出后执行。

令牌只从环境变量读取（GITHUB_TOKEN / GH_TOKEN），代码里不硬编码任何令牌；
发布方提供的 sha256 优先使用 GitHub asset 的 digest 字段，其次使用同名
.sha256 资产；两者都没有时 check_update 会给出 needs_confirmation 和人类可读
理由，download_update 必须拿到显式确认才继续——不假装校验过，也不默认放行。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com/repos/{repo}/releases/latest"
HTML_LATEST = "https://github.com/{repo}/releases/latest"
HTML_ASSETS = "https://github.com/{repo}/releases/expanded_assets/{tag}"
USER_AGENT = "WhaleGirlQQBridge-Updater"
MAX_UPDATE_BYTES = 300 * 1024 * 1024
MAX_RELEASES_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_NOTES_CHARS = 200000
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
VERSION_PART_RE = re.compile(r"\d+")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_TEXT_RE = re.compile(r"[0-9a-fA-F]{64}")
CHECKSUM_SUFFIXES = (".sha256", ".sha256sum")
VERIFY_DIGEST = "github-digest"
VERIFY_CHECKSUM_ASSET = "sha256-asset"
VERIFY_UNKNOWN = "unknown"
# 没有任何发布方哈希时的统一说法：既是 check_update 的 confirmation_reason，也是下载拒绝的理由。
UNVERIFIED_REASON = ("这个 Release 没有发布 sha256（安装包 digest 和 .sha256 校验资产都没有），"
                     "安装包无法校验完整性；请到 Releases 页面手动下载并核对，"
                     "确认无误后再选择继续未校验安装")
# 预发布后缀的权重：数字越大越旧，于是 alpha < beta < rc < 同号正式版。
PRE_RELEASE_RANKS = (("alpha", 3), ("beta", 2), ("rc", 1), ("pre", 1))
VERSION_HEAD_RE = re.compile(r"[^\-+~]*")
ASSET_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
)


def update_script_dir() -> Path:
    """更新脚本和更新日志所在的临时目录。"""
    return Path(tempfile.gettempdir()) / "WhaleGirlQQBridge-updates"


def update_log_path(pid) -> Path:
    return update_script_dir() / ("update-%d.log" % _safe_int(pid))


def _safe_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


DEFAULT_REPO = "linxisama/WhaleGirlQQBridge"


def normalize_repo(value) -> str:
    """把 owner/repo、GitHub 仓库链接、Release 链接或 git 远程地址收敛成 owner/repo。"""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(
        r"^(?:(?:https?|ssh)://)?(?:git@)?(?:www\.)?github\.com[:/]([^/?#\s]+)/([^/?#\s]+)",
        text, re.I)
    if match:
        text = match.group(1) + "/" + match.group(2)
    text = text.strip().strip("/")
    if text.lower().endswith(".git"):
        text = text[:-4].strip("/")
    if not REPO_RE.fullmatch(text):
        raise ValueError("更新仓库格式应为 owner/repo，例如 your-name/WhaleGirlQQBridge")
    return text


def _pre_release_rank(text: str) -> int:
    lowered = str(text or "").strip().lower()
    for word, rank in PRE_RELEASE_RANKS:
        if lowered.startswith(word):
            return rank
    return 2


def version_key(value) -> tuple:
    """版本号 -> 纯数字元组（最多四段），只取正式号那一段。

    元组形状被既有测试钉住（tests/test_app_updater.py:29 要求
    "v5.2.83-beta" -> (5, 2, 83)），所以预发布后缀不在这里体现，交给
    _version_sort_key 排序。只从正式号取数是为了不让 -rc1 里的 1 冒充第 4 段。
    """
    text = str(value or "").strip().lstrip("vV")
    head = VERSION_HEAD_RE.match(text).group(0)
    parts = tuple(int(part) for part in VERSION_PART_RE.findall(head)[:4])
    if not parts:
        # 版本号不在开头（tag 带前缀）时退回宽松取数，别把老仓库直接判成"没有更新"。
        return tuple(int(part) for part in VERSION_PART_RE.findall(text)[:4])
    return parts


def _version_sort_key(value) -> tuple:
    """排序用的版本键：正式版补 0，预发布补负数，于是 alpha < beta < rc < 同号正式版。

    必须补位：Python 里短元组更小，(5,2,112) 会排到 (5,2,112,-3) 前面。
    """
    parts = version_key(value)
    if not parts:
        return ()
    text = str(value or "").strip().lstrip("vV")
    tail = text[len(VERSION_HEAD_RE.match(text).group(0)):].lstrip("-+~ ._").strip()
    return parts + (0 if not tail else -_pre_release_rank(tail),)


def is_newer(latest, current) -> bool:
    left, right = _version_sort_key(latest), _version_sort_key(current)
    if not left:
        return False
    if not right:
        return True
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) > right + (0,) * (size - len(right))


def _safe_asset_url(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return ""
    if host not in ASSET_HOSTS and not host.endswith(".githubusercontent.com"):
        return ""
    return raw


def _release_assets(release) -> list:
    """把 Release 的 assets 归一化成 [{name, size, url, digest}]，只保留 GitHub https 地址。"""
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        return []
    out = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        url = _safe_asset_url(asset.get("browser_download_url") or asset.get("url"))
        if not url:
            continue
        out.append({
            "name": str(asset.get("name") or ""),
            "size": _safe_int(asset.get("size")),
            "url": url,
            "digest": str(asset.get("digest") or ""),
        })
    return out


def pick_asset(release: dict) -> dict | None:
    """从 Release 资产里挑安装包：优先 Setup exe，其次任意非卸载 exe。"""
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        return None
    candidates = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lowered = name.lower()
        if not lowered.endswith(".exe") or "uninstall" in lowered:
            continue
        if not _safe_asset_url(asset.get("browser_download_url") or asset.get("url")):
            continue
        candidates.append(asset)
    if not candidates:
        return None

    def rank(asset):
        name = str(asset.get("name") or "").lower()
        if "setup" in name and "whalegirl" in name:
            return 0
        if "setup" in name:
            return 1
        return 2

    candidates.sort(key=rank)
    return candidates[0]


def _verify_method(info) -> str:
    """不发网络请求判断校验方式：digest 优先，其次同名 .sha256 资产，都没有就是 unknown。

    只判断"用哪种方式"，不判断"是否通过"——通过与否要等下载时算出来。
    """
    asset = info.get("asset") if isinstance(info, dict) else None
    if not isinstance(asset, dict):
        return VERIFY_UNKNOWN
    if str(asset.get("digest") or "").strip():
        return VERIFY_DIGEST
    if find_checksum_asset(info, str(asset.get("name") or "")):
        return VERIFY_CHECKSUM_ASSET
    return VERIFY_UNKNOWN


def update_facts(info, download=None) -> dict:
    """把检查/下载结果收敛成写更新日志用的五要素；拿不到的项留空，写日志时显示"未知"。"""
    info = info if isinstance(info, dict) else {}
    asset = info.get("asset") if isinstance(info.get("asset"), dict) else {}
    download = download if isinstance(download, dict) else {}
    verified = bool(download.get("verified"))
    return {
        "repo": str(info.get("repo") or download.get("repo") or ""),
        "current": str(info.get("current") or ""),
        "latest": str(info.get("latest") or info.get("tag") or download.get("latest") or ""),
        "asset": str(asset.get("name") or download.get("name") or ""),
        "assetBytes": _safe_int(asset.get("size") or download.get("bytes")),
        "verifyMethod": str(download.get("verifyStatus")
                            or info.get("verify_method") or VERIFY_UNKNOWN),
        "verifyResult": ("校验通过" if verified else "未校验" if download else "未下载"),
        "sha256": str(download.get("sha256") or ""),
        "path": str(download.get("path") or ""),
    }


# 安装包路径 -> 五要素。按路径存，上一轮下载的记录不会冒充这一轮。
_last_update: dict = {}
_last_update_lock = threading.Lock()


def remember_update(info, download=None) -> dict:
    """记住某次下载的五要素，build_update_script 写日志时按安装包路径取用。"""
    facts = update_facts(info, download)
    key = facts.get("path")
    if key:
        with _last_update_lock:
            _last_update[key] = facts
            while len(_last_update) > 8:
                _last_update.pop(next(iter(_last_update)))
    return facts


def _log_facts(setup_path, info=None) -> dict:
    """优先用调用方显式给的 info；否则用这个安装包上次下载时记下的五要素。"""
    if isinstance(info, dict) and info:
        return update_facts(info)
    with _last_update_lock:
        return dict(_last_update.get(str(setup_path)) or {})


def _write_update_log(log_path, facts) -> None:
    """先把"查了什么、选了什么、怎么校验"写进日志；安装结果之后由 cmd 追加。"""
    facts = facts if isinstance(facts, dict) else {}

    def show(key, suffix=""):
        value = facts.get(key)
        return ("未知" if value in (None, "") else str(value)) + suffix

    size = _safe_int(facts.get("assetBytes"))
    lines = [
        "[update] 检查仓库=" + show("repo"),
        "[update] 当前版本=" + show("current"),
        "[update] 发现新版本=" + show("latest"),
        "[update] 选中资产=" + show("asset", "（%d 字节）" % size if size else ""),
        "[update] 校验方式=%s 结果=%s" % (show("verifyMethod"), show("verifyResult")),
    ]
    if facts.get("sha256"):
        lines.append("[update] 安装包 sha256=" + str(facts["sha256"]))
    # 固定 UTF-8：cmd 之后追加的都是 ASCII，追加进 UTF-8 文件不会坏；
    # 用 mbcs 反而会在非中文 Windows 上写不出去。
    # newline 显式给 CRLF：文本模式默认会把 LF 再转一次，写成 CRCRLF。
    with log_path.open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _release_info(data: dict, repo: str, current: str) -> dict:
    """把 GitHub Release 的 JSON 归一化成控制台用的检查结果。"""
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    notes = str(data.get("body") or "")
    truncated = len(notes) > MAX_NOTES_CHARS
    if truncated:
        notes = notes[:MAX_NOTES_CHARS]
    assets = _release_assets(data)
    asset = pick_asset(data)
    asset_info = None if asset is None else {
        "name": str(asset.get("name") or ""),
        "size": _safe_int(asset.get("size")),
        "url": _safe_asset_url(asset.get("browser_download_url") or asset.get("url")),
        "digest": str(asset.get("digest") or ""),
    }
    method = _verify_method({"asset": asset_info, "assets": assets})
    # 没有发布方哈希就必须由用户点头：这个字段一路传到控制台和下载门闩。
    needs_confirm = bool(asset_info) and method == VERIFY_UNKNOWN
    html_url = (str(data.get("html_url") or "").strip()
                or "https://github.com/%s/releases/tag/%s" % (repo, quote(tag, safe="")))
    return {
        "ok": True,
        "repo": repo,
        "repoUrl": "https://github.com/" + repo,
        "current": str(current or ""),
        "latest": tag,
        "tag": tag,
        "hasUpdate": is_newer(tag, current),
        "releaseUrl": html_url,
        "htmlUrl": html_url,
        "releaseName": str(data.get("name") or ""),
        "publishedAt": str(data.get("published_at") or ""),
        "notes": notes,
        "notesTruncated": truncated,
        "source": "api",
        "assets": assets,
        "asset": asset_info,
        "verify_method": method,
        "needs_confirmation": needs_confirm,
        "confirmation_reason": UNVERIFIED_REASON if needs_confirm else "",
    }


def _html_release(client, repo: str, current: str) -> dict:
    """API 被限流时，从公开 Releases 页面读最新 tag 和资产链接。"""
    latest_url = HTML_LATEST.format(repo=repo)
    response = client.get(latest_url, follow_redirects=False)
    if response.status_code == 404:
        raise ValueError("GitHub 仓库不存在：" + repo)
    location = str(response.headers.get("location") or "").strip()
    if location.startswith("/"):
        location = "https://github.com" + location
    marker = "/releases/tag/"
    if marker not in location:
        raise ValueError("这个仓库还没有发布 Release")
    tag = unquote(location.split(marker, 1)[1].split("?", 1)[0].strip("/"))
    if not tag:
        raise ValueError("GitHub 页面没有给出 Release 版本号")
    assets_url = HTML_ASSETS.format(repo=repo, tag=quote(tag, safe=""))
    page = client.get(assets_url, follow_redirects=True)
    if page.status_code == 404:
        raise ValueError("Release 页面不存在：" + tag)
    page.raise_for_status()
    if _safe_int(page.headers.get("content-length")) > MAX_RELEASES_BYTES:
        raise ValueError("GitHub Release 页面异常大，已拒绝")
    raw_assets = []
    for href in re.findall(r'href="([^"]+)"', page.text or ""):
        url = "https://github.com" + href if href.startswith("/") else href
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "github.com":
            continue
        path = parts.path or ""
        if "/releases/download/" not in path:
            continue
        name = unquote(path.rsplit("/", 1)[-1])
        if not name or name in (".", ".."):
            continue
        if not _safe_asset_url(url):
            continue
        raw_assets.append({"name": name, "browser_download_url": url, "size": 0})
    info = _release_info({"assets": raw_assets, "tag_name": tag, "body": "",
                          "published_at": "",
                          "html_url": "https://github.com/%s/releases/tag/%s"
                                      % (repo, quote(tag, safe=""))}, repo, current)
    info["source"] = "release-page"
    return info


def check_update(repo, current: str, timeout: float = 20.0) -> dict:
    """读取 GitHub 最新 Release；只读，不下载。

    repo 可以是 owner/repo，也可以是完整 GitHub 仓库链接或 Release 链接。
    """
    repo = normalize_repo(repo) or DEFAULT_REPO
    if not repo:
        raise ValueError("还没有配置 GitHub 更新仓库")
    url = API_BASE.format(repo=repo)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers=headers) as client:
            response = client.get(url)
            if response.status_code == 404:
                raise ValueError("GitHub 仓库或 Release 不存在：" + repo
                                 + "；请检查设置里的更新仓库，或到 Releases 页面确认已发版")
            if response.status_code in (403, 429):
                try:
                    return _html_release(client, repo, current)
                except Exception as fallback_error:
                    raise ValueError(
                        "GitHub 接口访问受限（HTTP %d，通常是未登录限流：每小时 60 次）。"
                        "也可以设置 GITHUB_TOKEN 后重启再试；备用 Release 页面也没读到更新信息：%s"
                        % (response.status_code, fallback_error)
                    ) from fallback_error
            response.raise_for_status()
            if _safe_int(response.headers.get("content-length")) > MAX_RELEASES_BYTES:
                raise ValueError("GitHub Release 信息异常大（超过 2 MB），已拒绝；"
                                 "请到 Releases 页面查看更新说明")
            try:
                data = response.json()
            except ValueError as exc:
                # 响应不是合法 JSON（限流页、代理拦截页）时，别把解析错误原样丢给用户。
                raise ValueError("GitHub 返回的 Release 信息不是合法 JSON（可能被代理或限流页拦截）："
                                 "请检查网络后重试，或到 Releases 页面手动查看") from exc
    except httpx.TimeoutException as exc:
        raise ValueError("连接 GitHub 超时（%.0f 秒）：请检查网络或代理后重试；"
                         "也可以到 Releases 页面手动下载" % float(timeout)) from exc
    except httpx.HTTPError as exc:
        raise ValueError("连接 GitHub 失败：%s。请检查网络或代理后重试，"
                         "也可以到 Releases 页面手动下载" % (exc or exc.__class__.__name__)) from exc
    except ValueError:
        raise
    except Exception as exc:
        # 响应不是合法 JSON 时 httpx 会抛 json 解析错误；这里不能让它裸奔成 500。
        raise ValueError("GitHub 返回的 Release 信息读不出来（%s）：请稍后重试，"
                         "或到 Releases 页面手动查看" % (exc or exc.__class__.__name__)) from exc
    if not isinstance(data, dict):
        raise ValueError("GitHub 返回的 Release 信息格式不正确")
    return _release_info(data, repo, current)


class UpdateProgress:
    """线程安全的下载进度记录。

    两种用法：
      * 直接当 download_update 的 progress 回调传（对象本身可调用）；
      * 后台线程下载时留在主线程，用 snapshot() 轮询进度。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"
        self._done = 0
        self._total = 0
        self._message = ""
        self._path = ""
        self._sha256 = ""
        self._verified = None
        self._verify_status = VERIFY_UNKNOWN
        self._started_at = 0.0
        self._updated_at = time.time()

    def __call__(self, done, total=None) -> None:
        self.update(done, total)

    def update(self, done, total=None) -> None:
        now = time.time()
        with self._lock:
            if self._state in ("idle", "done", "error"):
                self._state = "downloading"
                self._started_at = self._started_at or now
            self._done = max(0, _safe_int(done))
            if total is not None:
                self._total = max(0, _safe_int(total))
            if self._total and self._done > self._total:
                self._total = self._done
            self._updated_at = now

    def mark(self, state: str, message: str = "") -> None:
        now = time.time()
        with self._lock:
            self._state = str(state or self._state)
            if message:
                self._message = str(message)
            if self._state == "downloading" and not self._started_at:
                self._started_at = now
            self._updated_at = now

    def fail(self, message) -> None:
        self.mark("error", str(message or "下载失败"))

    def finish(self, path, sha256, verified, verify_status=None) -> None:
        now = time.time()
        with self._lock:
            self._state = "done"
            self._path = str(path or "")
            self._sha256 = str(sha256 or "")
            self._verified = bool(verified)
            if verify_status:
                self._verify_status = str(verify_status)
            self._message = "安装包已就绪"
            self._updated_at = now

    def snapshot(self) -> dict:
        with self._lock:
            done, total = self._done, self._total
            percent = None if total <= 0 else round(min(100.0, done * 100.0 / total), 1)
            elapsed = max(0.0, self._updated_at - self._started_at) if self._started_at else 0.0
            speed = round(done / elapsed, 1) if elapsed > 0 else 0.0
            return {
                "state": self._state,
                "done": done,
                "total": total,
                "percent": percent,
                "bytesPerSecond": speed,
                "message": self._message,
                "path": self._path,
                "sha256": self._sha256,
                "verified": self._verified,
                "verifyStatus": self._verify_status,
                "startedAt": self._started_at,
                "updatedAt": self._updated_at,
            }


def _notify(progress, done, total) -> None:
    """调用外部进度回调；回调本身出错不能打断下载。"""
    if progress is None:
        return
    callback = progress if callable(progress) else getattr(progress, "update", None)
    if not callable(callback):
        return
    try:
        callback(_safe_int(done), _safe_int(total))
    except Exception:
        log.debug("更新进度回调失败（已忽略）", exc_info=True)


def _progress_state(progress, state: str, message: str = "") -> None:
    mark = getattr(progress, "mark", None)
    if callable(mark):
        try:
            mark(state, message)
        except Exception:
            log.debug("更新进度状态写入失败（已忽略）", exc_info=True)


def _progress_finish(progress, path, sha256, verified, verify_status) -> None:
    finish = getattr(progress, "finish", None)
    if callable(finish):
        try:
            finish(path, sha256, verified, verify_status)
        except TypeError:
            try:
                finish(path, sha256, verified)
            except Exception:
                log.debug("更新进度收尾失败（已忽略）", exc_info=True)
        except Exception:
            log.debug("更新进度收尾失败（已忽略）", exc_info=True)


def sha256_from_digest(value) -> str:
    """解析 GitHub asset 的 digest 字段（sha256:<64 hex>）。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if ":" in text:
        algorithm, _, rest = text.partition(":")
        if algorithm.strip() != "sha256":
            raise ValueError("安装包 digest 使用的不是 sha256，已拒绝：" + str(value)[:80])
        text = rest.strip()
    if not SHA256_RE.fullmatch(text):
        raise ValueError("安装包 digest 格式不正确，已拒绝：" + str(value)[:80])
    return text


def parse_sha256_text(text) -> str:
    """从 .sha256 文件内容里取第一段 64 位十六进制摘要。"""
    match = SHA256_TEXT_RE.search(str(text or ""))
    return match.group(0).lower() if match else ""


def find_checksum_asset(info, asset_name: str) -> dict | None:
    """在 Release 资产里找 <安装包名>.sha256 / .sha256sum。"""
    wanted = {str(asset_name or "").lower() + suffix for suffix in CHECKSUM_SUFFIXES}
    assets = info.get("assets") if isinstance(info, dict) else None
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if name.lower() not in wanted:
            continue
        url = _safe_asset_url(asset.get("url") or asset.get("browser_download_url"))
        if not url:
            continue
        return {"name": name, "url": url, "size": _safe_int(asset.get("size"))}
    return None


def _fetch_checksum(client, url: str) -> str:
    response = client.get(url, follow_redirects=True)
    if response.status_code == 404:
        raise ValueError("Release 里声明的 sha256 校验文件不存在：" + url)
    response.raise_for_status()
    if _safe_int(response.headers.get("content-length")) > MAX_CHECKSUM_BYTES:
        raise ValueError("sha256 校验文件异常大，已拒绝")
    text = str(getattr(response, "text", "") or "")
    if len(text) > MAX_CHECKSUM_BYTES:
        raise ValueError("sha256 校验文件异常大，已拒绝")
    return text


def _published_sha256(client, info, asset_name: str, asset_digest) -> tuple:
    """返回 (期望的 sha256 十六进制, 校验来源)；没有发布哈希时返回 ("", unknown)。"""
    digest = sha256_from_digest(asset_digest)
    if digest:
        return digest, VERIFY_DIGEST
    checksum = find_checksum_asset(info, asset_name)
    if checksum:
        parsed = parse_sha256_text(_fetch_checksum(client, checksum["url"]))
        if not parsed:
            raise ValueError("发布方提供了 %s，但里面没有可用的 sha256，已放弃更新"
                             % checksum["name"])
        return parsed, VERIFY_CHECKSUM_ASSET
    return "", VERIFY_UNKNOWN


def _safe_filename(value: str, fallback: str = "update.exe") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return name or fallback


def download_update(info: dict, dest_dir=None, timeout: float = 600.0, progress=None,
                    allow_unverified: bool = False) -> dict:
    """下载 Release 里的安装包；边下边算 sha256，不把整个文件读进内存。

    progress 可选，形如 progress(done_bytes, total_bytes)；可以直接传
    UpdateProgress 实例，边下边用 snapshot() 轮询。

    allow_unverified 是"未校验也继续"的唯一开关。当 info 来自 check_update 且
    该 Release 没有任何发布方哈希（needs_confirmation=True）时，拿不到哈希就
    直接抛 ValueError，调用方必须拿到用户确认再显式传 True——不默认放行。
    手工拼出来的 info（老调用方、传输层测试）不带这个字段，行为保持不变。
    """
    asset = info.get("asset") if isinstance(info, dict) else None
    if not isinstance(asset, dict):
        raise ValueError("这个版本没有可下载的安装包（Release 里需要 Setup .exe）")
    url = _safe_asset_url(asset.get("url") or asset.get("browser_download_url"))
    if not url:
        raise ValueError("安装包地址不是 GitHub 的 https 地址，已拒绝下载")
    raw_name = str(asset.get("name") or "")
    if raw_name and not raw_name.lower().endswith(".exe"):
        raise ValueError("安装包文件名不是 .exe，已拒绝：" + raw_name[:120])
    # 只有 check_update 产出的 info 才带这个字段；它说明"发布方没有哈希，要用户点头"。
    needs_confirm = info.get("needs_confirmation") is True
    name = _safe_filename(raw_name, "WhaleGirlQQBridge-Setup.exe")
    root = Path(dest_dir) if dest_dir else update_script_dir()
    root.mkdir(parents=True, exist_ok=True)
    tag_name = _safe_filename(info.get("tag") or info.get("latest"), "latest")
    target = root / (tag_name + "-" + name)
    part = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    total = 0
    verified = False
    expected_sha = ""
    verify_status = VERIFY_UNKNOWN
    _progress_state(progress, "downloading", "开始下载 " + name)
    _notify(progress, 0, 0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            expected_sha, verify_status = _published_sha256(client, info, raw_name,
                                                            asset.get("digest"))
            if not expected_sha and not allow_unverified and needs_confirm:
                # 不默认放行：check_update 已经说明这个版本没有哈希，停在下载前。
                raise ValueError(UNVERIFIED_REASON)
            with client.stream("GET", url) as response:
                response.raise_for_status()
                host = (urlsplit(str(response.url)).hostname or "").lower()
                if host not in ASSET_HOSTS and not host.endswith(".githubusercontent.com"):
                    raise ValueError("安装包下载被重定向到非 GitHub 地址，已拒绝")
                expected = _safe_int(response.headers.get("content-length"))
                if expected > MAX_UPDATE_BYTES:
                    raise ValueError("安装包超过 300 MB，已拒绝")
                with part.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_UPDATE_BYTES:
                            raise ValueError("安装包超过 300 MB，已拒绝")
                        digest.update(chunk)
                        handle.write(chunk)
                        _notify(progress, total, expected)
                    handle.flush()
                    os.fsync(handle.fileno())
        if not total:
            raise ValueError("下载到的安装包是空的")
        actual = digest.hexdigest()
        _notify(progress, total, total)
        if expected_sha:
            _progress_state(progress, "verifying", "校验安装包 sha256")
            if expected_sha != actual:
                part.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise ValueError("安装包 sha256 校验失败（发布 %s，下载 %s），已删除并放弃更新"
                                 % (expected_sha, actual))
            verified = True
        else:
            verify_status = VERIFY_UNKNOWN
        os.replace(part, target)
    except Exception as exc:
        part.unlink(missing_ok=True)
        _progress_state(progress, "error", str(exc) or exc.__class__.__name__)
        if isinstance(exc, ValueError):
            raise
        if isinstance(exc, httpx.TimeoutException):
            raise ValueError("下载安装包超时（%.0f 秒）：请检查网络或代理后重试，"
                             "也可以到 Releases 页面手动下载" % float(timeout)) from exc
        if isinstance(exc, httpx.HTTPError):
            raise ValueError("下载安装包失败：%s。请检查网络或代理后重试，"
                             "也可以到 Releases 页面手动下载"
                             % (exc or exc.__class__.__name__)) from exc
        raise
    result = {
        "path": str(target),
        "bytes": total,
        "sha256": actual,
        "name": name,
        "downloadedAt": time.time(),
        "verified": bool(verified),
        "verifyStatus": verify_status if verified else VERIFY_UNKNOWN,
        "expectedSha256": expected_sha,
    }
    remember_update(info, result)
    _progress_state(progress, "done", "安装包已就绪")
    _progress_finish(progress, result["path"], result["sha256"],
                     result["verified"], result["verifyStatus"])
    return result


def app_executable() -> str:
    """冻结运行时返回安装目录里的 exe；源码运行返回空串（不能一键覆盖源码）。"""
    if not getattr(sys, "frozen", False):
        return ""
    exe = Path(sys.executable)
    return str(exe) if exe.is_file() and exe.suffix.lower() == ".exe" else ""


BAD_BATCH_CHARS = ('"', "\r", "\n", "\x00")


def _batch_quote(value: str) -> str:
    """把路径安全地放进 set "VAR=..." 里：转义 % ，拒绝无法安全引号转义的字符。"""
    text = str(value)
    for char in BAD_BATCH_CHARS:
        if char in text:
            raise ValueError("路径里含有不能放进批处理的字符，已拒绝：%r" % text)
    return text.replace("%", "%%")


def build_update_script(setup_path, app_exe, pid: int, install_dir=None, info=None) -> Path:
    """生成一个等待控制台退出、记录日志、静默安装、再拉起的 cmd 脚本。

    info 可选：显式给出检查结果，日志里就能写清检查了哪个仓库、当前/新版本、
    选中哪个资产；不给时用最近一次 download_update 记下的五要素。
    """
    setup = Path(setup_path)
    if not setup.is_file():
        raise ValueError("安装包文件不存在：" + str(setup))
    app = Path(app_exe)
    if not str(app_exe or "").strip() or app.suffix.lower() != ".exe":
        raise ValueError("不是安装版程序，无法一键更新")
    target = Path(install_dir) if install_dir else app.parent
    directory = update_script_dir()
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / ("apply-%d.cmd" % _safe_int(pid))
    log_file = update_log_path(pid)
    # 检查要素先落盘，安装结果之后由 cmd 追加到同一个文件。
    try:
        _write_update_log(log_file, _log_facts(setup, info))
    except OSError as exc:
        log.warning("写更新日志失败（继续安装）：%s", exc)
    # 只记录当前 exe 和安装目录，不复制整个目录。
    lines = [
        "@echo off",
        "setlocal",
        'set "SETUP=' + _batch_quote(str(setup)) + '"',
        'set "APP=' + _batch_quote(str(app)) + '"',
        'set "DIR=' + _batch_quote(str(target)) + '"',
        'set "PID=' + str(_safe_int(pid)) + '"',
        'set "LOG=' + _batch_quote(str(log_file)) + '"',
        'for %%F in ("%APP%") do set "APPNAME=%%~nxF"',
        '>>"%LOG%" echo [update] start %DATE% %TIME%',
        '>>"%LOG%" echo [update] setup="%SETUP%"',
        '>>"%LOG%" echo [update] app="%APP%"',
        '>>"%LOG%" echo [update] dir="%DIR%"',
        '>>"%LOG%" echo [update] pid=%PID%',
        'set "TRIES=0"',
        ":wait",
        "set /a TRIES+=1",
        # 卡死的旧实例不能让安装永远等下去：约 10 分钟后记日志继续装。
        "if %TRIES% GTR 300 goto waittimeout",
        # PID 可能被系统复用；命中 PID 后还要确认映像名，避免把别的进程当成控制台。
        'tasklist /FI "PID eq %PID%" /NH 2>nul | findstr /I /C:"%APPNAME%" >nul',
        "if not errorlevel 1 (",
        "  ping -n 3 127.0.0.1 >nul",
        "  goto wait",
        ")",
        'tasklist /FI "IMAGENAME eq %APPNAME%" /NH 2>nul | findstr /I /C:"%APPNAME%" >nul',
        "if not errorlevel 1 (",
        "  ping -n 3 127.0.0.1 >nul",
        "  goto wait",
        ")",
        "goto install",
        ":waittimeout",
        '>>"%LOG%" echo [WARN] waited too long (about 10 min), installing anyway',
        ":install",
        '>>"%LOG%" echo [update] console exited, starting silent install',
        'start "" /wait "%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="%DIR%"',
        'set "RC=%ERRORLEVEL%"',
        'if "%RC%"=="0" goto installed',
        '>>"%LOG%" echo [ERROR] installer failed exitcode=%RC% (0=ok 3010=reboot required)',
        '>>"%LOG%" echo [ERROR] setup="%SETUP%" dir="%DIR%"',
        # 日志要能让用户自己往下走：装不上就指到 Releases 页面手动装。
        # 这里必须是纯 ASCII：cmd 按控制台代码页写字节，中文会在部分系统上变乱码。
        '>>"%LOG%" echo [ERROR] install not finished: download Setup.exe from the Releases'
        ' page and run it; if it still fails, close every WhaleGirlQQBridge process first',
        "goto relaunch",
        ":installed",
        '>>"%LOG%" echo [update] installer finished exitcode=0',
        ":relaunch",
        '>>"%LOG%" echo [update] restarting console app="%APP%"',
        'start "" "%APP%"',
        'del "%~f0"',
    ]
    text = "\r\n".join(lines) + "\r\n"
    try:
        script.write_text(text, encoding="mbcs")
    except (LookupError, UnicodeEncodeError):
        script.write_text(text, encoding="utf-8")
    return script


def launch_update_script(script_path) -> None:
    """脱离当前进程启动更新脚本；控制台随后自己退出。"""
    script = Path(script_path)
    if not script.is_file():
        raise ValueError("更新脚本不存在：" + str(script))
    flags = 0
    if os.name == "nt":
        # 不要用 DETACHED_PROCESS：没有控制台时 tasklist 不产出任何输出，
        # 等待循环会空转到超时才安装。隐藏控制台既能后台运行，也能让 tasklist 正常输出。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd.exe", "/c", str(script)], cwd=str(script.parent),
                     creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
