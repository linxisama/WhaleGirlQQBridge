"""首次使用（还没填 API Key）时的行为测试。

背景：全新解压的 config.yaml 里 api_url / api_key 都是空的，
``is_available`` 却恒为 True（它只表示"熔断没打开"），于是
- WebUI 会显示"可用"，用户以为已经配好了；
- 每条消息都会真的去请求一个空 URL，重试 + 退避 + 刷错误日志。
本测试锁住修复后的语义：configured 才是"真能调用"，未配置时直接走本地兜底。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ai_engine import AIEngine  # noqa: E402

FAILURES: list[str] = []
PASSED = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}", flush=True)
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name}  {extra}", flush=True)


def test_unconfigured() -> None:
    print("\n[1] 未配置（全新安装的默认状态）", flush=True)
    engine = AIEngine(api_url="", api_key="", model="")
    check("configured 为 False", engine.configured is False)
    check("available 仍为 True（历史语义不变）", engine.is_available is True)

    started = time.monotonic()
    reply = asyncio.run(
        engine.chat([{"role": "user", "content": "在吗"}], system_prompt="你是鲸鱼娘")
    )
    elapsed = time.monotonic() - started
    check("chat() 返回空串（交给本地兜底）", reply == "", repr(reply))
    check("chat() 不发起网络请求、立即返回", elapsed < 0.5, f"耗时 {elapsed:.2f}s")

    async def collect() -> list[str]:
        chunks: list[str] = []
        async for chunk in engine.chat_stream(
            [{"role": "user", "content": "在吗"}], system_prompt="你是鲸鱼娘"
        ):
            chunks.append(chunk)
        return chunks

    check("chat_stream() 不产出任何分片", asyncio.run(collect()) == [])


def test_partially_configured() -> None:
    print("\n[2] 只填了一半也算未配置", flush=True)
    only_url = AIEngine(api_url="https://example.test/v1/chat/completions", api_key="")
    check("只有地址、没 Key → configured False", only_url.configured is False)
    only_key = AIEngine(api_url="", api_key="sk-test-1234567890abcdefghij")
    check("只有 Key、没地址 → configured False", only_key.configured is False)


def test_configured() -> None:
    print("\n[3] 配置完整", flush=True)
    engine = AIEngine(
        api_url="https://example.test/v1/chat/completions",
        api_key="sk-test-1234567890abcdefghij",
        model="deepseek-chat",
    )
    check("configured 为 True", engine.configured is True)
    check("api_style 自动识别为 openai", engine.api_style == "openai", engine.api_style)


def test_reconfigure_enables() -> None:
    print("\n[4] WebUI 保存后热更新即可用", flush=True)
    engine = AIEngine(api_url="", api_key="", model="")
    check("初始未配置", engine.configured is False)
    asyncio.run(
        engine.reconfigure(
            api_url="https://api.deepseek.com/v1/chat/completions",
            api_key="sk-test-1234567890abcdefghij",
            model="deepseek-chat",
        )
    )
    check("热更新后 configured 为 True", engine.configured is True)
    check("地址补全为完整 chat/completions 路径",
          engine.api_url.endswith("/chat/completions"), engine.api_url)


def main() -> int:
    test_unconfigured()
    test_partially_configured()
    test_configured()
    test_reconfigure_enables()
    print("\n" + "=" * 56)
    if FAILURES:
        print(f"[FAIL] {len(FAILURES)} 项失败：{FAILURES}")
        return 1
    print(f"AI 配置守卫测试全部通过 ✅（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
