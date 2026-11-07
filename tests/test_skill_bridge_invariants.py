"""技能桥三处修复的在位守卫（2026-11-07）。

`src/skill_tools.py` 与 `src/skill_manifest.py` 当天被本仓库以外的执行者覆盖四次
（03:05 / 05:39 / 07:03 / 11:00），每次覆盖都精确删掉同一批修复，全量套件要跑
约 19 分钟才暴露。这个文件把三条不变量直接钉在源码文本上：秒级失败，指到具体行。

不变量（每一条都有独立理由，删之前先读理由）：
1. `_MEMBER` / `_MEMBER_BRACKET` 必须同时认 `ctx` 与 `api`。
   `setup(api)` 期的 `api.config()` / `api.fetch()` 是真实用法（`HOST_SETUP_API`
   就是这一批），只扫 ctx 会把它判成「没用」→ 漏判 → 错误放行。
2. `_skill_plan` 必须有 hostImports 闸。函数自己的 docstring 写着「四道闸」。
3. `classify()` 必须保留 `T4-host-source` 返回分支，但它是**故意不可达**的：
   条件只能用 `escape_above`（已在上一行判成 T6）。改成 `if host_imports: return T4`
   会让 373 个真实包里的 85 个从 T1/T2 翻成 T4，而 T4 是「不加载、不重试」。
"""
import io
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative):
    return io.open(ROOT / relative, encoding="utf-8").read()


class ScannerNamespaceTests(unittest.TestCase):
    """扫描器必须认 ctx 与 api 两个命名空间。"""

    def setUp(self):
        self.text = _read("src/skill_tools.py")

    def _pattern(self, name):
        match = re.search(r"^%s = re.compile\(r\"(.*)\"\)$" % name,
                          self.text, re.MULTILINE)
        self.assertIsNotNone(match, "%s 找不到，扫描器可能被重写了" % name)
        return match.group(1)

    def test_member_scans_both_namespaces(self):
        pattern = self._pattern("_MEMBER")
        self.assertIn("(?:ctx|api)", pattern,
                      "_MEMBER 只认 ctx：setup(api) 期的 api.config() 会被判成没用")

    def test_member_bracket_scans_both_namespaces(self):
        pattern = self._pattern("_MEMBER_BRACKET")
        self.assertIn("(?:ctx|api)", pattern,
                      "_MEMBER_BRACKET 只认 ctx：api[\"x\"] 这种写法扫不到")

    def test_destructure_still_scans_both_namespaces(self):
        # _DESTRUCT 一直是对的，列在这里防止有人「统一」成只认 ctx。
        pattern = self._pattern("_DESTRUCT")
        self.assertIn("(?:ctx|api)", pattern)

    def test_api_namespace_actually_scans(self):
        # 行为验证：不只比对文本，真的跑一遍扫描。
        from src import skill_tools
        members = set()
        for name in ("_MEMBER", "_MEMBER_BRACKET", "_DESTRUCT"):
            regex = getattr(skill_tools, name)
            for chunk in ("api.config()", "api[\"config\"]", "const { config } = api"):
                found = regex.findall(chunk)
                for item in found:
                    members.add(item.split(",")[0].strip())
        self.assertIn("config", members,
                      "三种写法都该扫出 config，扫不到说明 api 命名空间又丢了")


class HostImportsGateTests(unittest.TestCase):
    """清单声明了 hostImports 的包必须被拦下，理由要能读懂。"""

    def setUp(self):
        self.text = _read("src/skill_tools.py")

    def test_gate_exists_in_skill_plan(self):
        self.assertIn("declared_host", self.text,
                      "_skill_plan 的 hostImports 闸不见了（docstring 仍写着四道闸）")

    def test_gate_mentions_the_skip_reason(self):
        self.assertIn("宿主源码模块", self.text,
                      "跳过理由丢了：用户看到 skipped 要能知道为什么")

    def test_declared_host_imports_are_rejected(self):
        # 行为验证：真造一个声明 hostImports 的包，必须被挡。
        from src import skill_tools
        plan, reason = skill_tools._skill_plan(
            {"id": "probe", "tier": "T1-ready",
             "hostImports": ["../../src/tools.js"]},
            None, "probe-source", False)
        self.assertIsNone(plan, "声明了宿主源码依赖的包不该放行")
        self.assertIn("宿主源码模块", reason)


class HostSourceTierTests(unittest.TestCase):
    """T4 分支的存在性守卫，以及「2 层上溯不算 T4」的现口径。"""

    def setUp(self):
        self.text = _read("src/skill_manifest.py")

    def test_branch_exists(self):
        self.assertIn('host-source:%s', self.text,
                      "classify() 的 T4-host-source 返回分支不见了")

    def test_branch_stays_ahead_of_requires(self):
        # 顺序本身仍要守住：先认 hostImports、再判 requires，两个字段都记账。
        host = self.text.index("if host_imports:")
        req = self.text.index("if requires:")
        self.assertLess(host, req, "host_imports 的判定被挪到 requires 之后了")

    def test_tier_order_still_lists_t4(self):
        self.assertIn("T4-host-source", self.text,
                      "TIER_ORDER 里没有 T4-host-source，返回分支也就没意义了")

    def _classify_entry(self, name, entry):
        import json
        import tempfile
        from pathlib import Path
        from src import skill_manifest, skill_tools
        holder = tempfile.TemporaryDirectory(prefix="invariant-t4-")
        self.addCleanup(holder.cleanup)
        root = Path(holder.name) / name
        root.mkdir(parents=True)
        (root / "skill.json").write_text(
            json.dumps({"id": name, "name": name.upper(), "version": "1.0.0"}),
            encoding="utf-8")
        (root / "index.js").write_text(entry, encoding="utf-8")
        reader = skill_tools._DirReader(root)
        try:
            return skill_manifest.classify(reader)
        finally:
            skill_manifest._close(reader)

    def test_two_level_host_import_is_not_t4(self):
        # 2026-11-07 16:53 全量套件挂的 5 条，起因就是把 host_imports 直接当 T4 判据。
        # 实测（.tmp/t4_impact.py，373 个真实包）：那样会让 85 个包从 T1/T2 翻成 T4，
        # 而 BLOCKED_TIERS 对 T4 是「不加载、不重试」=> 85 个包当场全部不再注册。
        # 2 层上溯本来就能解析：<source>/skills-host-<pid>/0000-<id>/ => <source>/src。
        info = self._classify_entry(
            "hosty", "import {cfg} from '../../src/config.js';\n"
                     "ctx.registerTool({ name: 'hosty' });\n")
        self.assertEqual(info["tier"], "T1-ready",
                         "2 层上溯被判成 T4 会封掉 90 个正在注册的真实包")
        self.assertEqual(info["hostImports"], ["../../src/config.js"])

    def test_entry_escape_three_levels_is_t6(self):
        # 真正加载不了的（入口自己上溯 >=3 层）走 T6，这条判据不能跟着 T4 一起丢。
        info = self._classify_entry(
            "escaper", "const llm = require('../../../src/llm.js');\n"
                       "ctx.registerTool({ name: 'escaper' });\n")
        self.assertEqual(info["tier"], "T6-disabled")
        self.assertIn("escape-run-root", info["tierReason"])


class PolicyConsistencyTests(unittest.TestCase):
    """政策本身要自洽：GATED 与 ACCOUNTED 不能重叠。"""

    def test_accounted_and_gated_are_disjoint(self):
        from src import skill_tools
        overlap = set(skill_tools.NODE_BUILTIN_GATED) & set(skill_tools.NODE_BUILTIN_ACCOUNTED)
        self.assertEqual(overlap, set(),
                         "同一个内置模块不能既记账又阻断：%s" % sorted(overlap))

    def test_child_process_stays_accounted(self):
        # 这条政策是被人特意从 GATED 挪到 ACCOUNTED 的，别顺手改回去。
        from src import skill_tools
        self.assertIn("node:child_process", skill_tools.NODE_BUILTIN_ACCOUNTED)
        self.assertNotIn("node:child_process", skill_tools.NODE_BUILTIN_GATED)


if __name__ == "__main__":
    unittest.main()
