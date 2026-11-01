"""tool_schema 单元测试（WP5c）。

两条底线都要守：能从源码对象字面量里抠出完整 schema；抠不到 / 源码损坏时安静降级，
既不抛也不许把其它工具一起带崩。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from src import tool_schema

EMPTY = {'type': 'object', 'properties': {}}


def make_package(root, files):
    """把 相对路径 → 文本 落成目录树，返回目录。"""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.case = 0

    def package(self, files, name=None):
        self.case += 1
        root = self.tmp / (name or 'pkg%d' % self.case)
        root.mkdir(parents=True, exist_ok=True)
        return make_package(root, files)


WIKI_SOURCE = """module.exports = function setup(api) {
  api.registerTool({
    id: 'wiki_query',
    name: '查 GBF 灰机 wiki',
    description: '查询《碧蓝幻想》灰机中文 wiki：角色、武器、召唤石',
    category: 'knowledge',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '要查的名字' },
        kind: {
          type: 'string',
          enum: ['character', 'weapon', 'summon'],
        },
        section: { type: 'string' },
      },
      required: ['query'],
    },
    async execute(ctx, args) {
      return { ok: true, echo: args.query };
    },
  });
};
"""


class CodeExtractionTest(TempCase):
    def test_basic_object_with_method_shorthand(self):
        root = self.package({'index.js': WIKI_SOURCE})
        tools = tool_schema.extract_tools(root)
        self.assertEqual(list(tools), ['wiki_query'])
        tool = tools['wiki_query']
        self.assertEqual(tool['id'], 'wiki_query')
        self.assertEqual(tool['name'], 'wiki_query')
        self.assertEqual(tool['label'], '查 GBF 灰机 wiki')
        self.assertTrue(tool['description'].startswith('查询《碧蓝幻想》'))
        self.assertEqual(tool['source'], 'code')
        params = tool['parameters']
        self.assertEqual(params['type'], 'object')
        self.assertEqual(sorted(params['properties']), ['kind', 'query', 'section'])
        self.assertEqual(params['properties']['kind']['enum'], ['character', 'weapon', 'summon'])
        self.assertEqual(params['required'], ['query'])

    def test_double_quotes_trailing_commas_and_comments(self):
        source = (
            'api.registerTool({\n'
            '  "id": "ping",\n'
            '  // 行注释里带个花括号 } 不该影响解析\n'
            '  "description": "探活",\n'
            '  /* 块注释 { 也一样 */\n'
            '  "parameters": {\n'
            '    "type": "object",\n'
            '    "properties": {\n'
            '      "target": { "type": "string", },\n'
            '      "times": { "type": "integer", "default": 1, },\n'
            '    },\n'
            '    "required": ["target",],\n'
            '  },\n'
            '});\n'
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertEqual(sorted(tools['ping']['parameters']['properties']), ['target', 'times'])
        self.assertEqual(tools['ping']['parameters']['required'], ['target'])
        self.assertEqual(tools['ping']['description'], '探活')

    def test_string_concatenation_is_merged(self):
        source = (
            "api.registerTool({ id: 'cat', description: '前' + '中' + '后',\n"
            "  parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n"
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertEqual(tools['cat']['description'], '前中后')

    def test_template_string_with_expression_is_dropped(self):
        source = (
            "api.registerTool({ id: 'tpl', name: 'Tpl',\n"
            "  description: `数 ${{count} 条`,\n"
            "  parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n"
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('tpl', tools)
        self.assertNotIn('${', tools['tpl']['description'])
        self.assertEqual(tools['tpl']['description'], 'Tpl')
        self.assertIn('q', tools['tpl']['parameters']['properties'])

    def test_identifier_and_arrow_values_dropped_not_fatal(self):
        source = (
            "api.registerTool({ id: 'idv', name: 'IDV', category: CATEGORIES.weapon,\n"
            "  handler: doSomething,\n"
            "  validate: (args) => args.q,\n"
            "  parameters: { type: 'object', properties: { q: { type: 'string', default: true } } } });\n"
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('idv', tools)
        params = tools['idv']['parameters']
        self.assertEqual(sorted(params['properties']), ['q'])
        self.assertIs(params['properties']['q']['default'], True)

    def test_multiple_calls_and_comment_call_ignored(self):
        source = (
            "// api.registerTool({ id: 'ghost' });\n"
            "/* api.registerTool({ id: 'ghost2', parameters: { type: 'object' } }); */\n"
            "api.registerTool({ id: 'one', parameters: { type: 'object', properties: { a: { type: 'string' } } } });\n"
            "api.registerTool({ id: 'two', parameters: { type: 'object', properties: { b: { type: 'string' } } } });\n"
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertEqual(sorted(tools), ['one', 'two'])
        self.assertNotIn('ghost', tools)
        self.assertNotIn('ghost2', tools)

    def test_scope_tools_register_and_registry_add(self):
        source = (
            "scope.tools.register({ name: 'fast_vision', description: '看图',\n"
            "  parameters: { type: 'object', properties: { file: { type: 'string' } } } });\n"
            "registry.add({ id: 'extra', parameters: { type: 'object', properties: { k: { type: 'string' } } } });\n"
        )
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('fast_vision', tools)
        self.assertIn('extra', tools)

    def test_truncated_source_does_not_raise(self):
        source = "api.registerTool({ id: 'half', parameters: { type: 'object', properties: { q: { type: 'str"
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIsInstance(tools, dict)
        for tool in tools.values():
            self.assertIsInstance(tool['parameters'], dict)

    def test_garbage_source_does_not_raise(self):
        source = '}}}{{{ api.registerTool(((( ((( \x00 \x01 not javascript at all \n\n )))'
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIsInstance(tools, dict)

    def test_unbalanced_and_huge_nesting_does_not_raise(self):
        source = "api.registerTool({ id: 'deep', parameters: " + '{ a: ' * 200 + '1' + ' }' * 200 + ' });'
        self.assertIsInstance(tool_schema.extract_tools(self.package({'index.js': source})), dict)


REGEX_QUOTES_SOURCE = """async function findAssets(res) {
  const html = await res.text();
  const m = html.match(/<script[^>]*src=["']([^"']*)["']/i)
    || html.match(/src=["'][^"']*[.]js["']/i);
  return m ? m[1] : '';
}

module.exports = function setup(api) {
  api.registerTool({
    id: 'search',
    name: '搜烂梗',
    description: '在 6657 烂梗库里搜梗',
    parameters: {
      type: 'object',
      properties: { q: { type: 'string', description: '关键词' } },
      required: ['q'],
    },
    async execute(ctx, args) {
      return { content: findAssets(args), isError: false };
    },
  });
};
"""


class MaskTest(TempCase):
    """正则字面量里的孤立引号不能把后面的代码整片遮掉（真实 sb6657-memes 回归）。"""

    def test_regex_literal_quotes_do_not_hide_register_call(self):
        root = self.package({'index.js': REGEX_QUOTES_SOURCE})
        tools = tool_schema.extract_tools(root)
        self.assertIn('search', tools)
        self.assertEqual(tools['search']['parameters']['required'], ['q'])
        self.assertIn('q', tools['search']['parameters']['properties'])

    def test_unterminated_quote_does_not_hide_following_code(self):
        source = 'const s = "unclosed\n' + "api.registerTool({ id: 'x', parameters: { type: 'object' } });\n"
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('x', tools)

    def test_comment_quotes_do_not_start_a_string(self):
        source = "// don't call registerTool( in comments\napi.registerTool({ id: 'real' });\n"
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertEqual(list(tools), ['real'])

    def test_mask_keeps_length_and_newlines(self):
        text = "// 注释里的 registerTool\n/* 块注释 '\" */\napi.registerTool({ id: 'a' });\n"
        masked = tool_schema._mask(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count('\n'), text.count('\n'))
        self.assertIn('registerTool', masked)
        self.assertNotIn('注释里的', masked)

    def test_optional_call_syntax_is_found(self):
        source = ("export function setup(a) {\n"
                  "  a.registerTool?.({ id: 'opt', parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n"
                  "}\n")
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('opt', tools)
        self.assertIn('q', tools['opt']['parameters']['properties'])

    def test_division_is_not_treated_as_regex(self):
        source = ("const ratio = total / count;\n"
                  "const label = 'a/b';\n"
                  "api.registerTool({ id: 'after-div', parameters: { type: 'object' } });\n")
        tools = tool_schema.extract_tools(self.package({'index.js': source}))
        self.assertIn('after-div', tools)

    def test_template_string_spanning_lines_is_masked(self):
        tick = chr(96)
        text = "const t = " + tick + "第一行\nregisterTool( 假的" + tick + ";\napi.registerTool({ id: 'b' });\n"
        tools = tool_schema.extract_tools(self.package({'index.js': text}))
        self.assertEqual(list(tools), ['b'])


class ManifestSourceTest(TempCase):
    def test_manifest_tools_are_used(self):
        root = self.package({
            'skill.json': json.dumps({'id': 'sk', 'tools': [
                {'id': 'sk_one', 'description': '清单工具',
                 'parameters': {'type': 'object', 'properties': {'x': {'type': 'string'}}}},
                {'id': 'sk_two'},
            ]}, ensure_ascii=False),
            'index.js': '// 没有 registerTool',
        })
        tools = tool_schema.extract_tools(root)
        self.assertEqual(sorted(tools), ['sk_one', 'sk_two'])
        self.assertEqual(tools['sk_one']['source'], 'manifest')
        self.assertEqual(tools['sk_one']['description'], '清单工具')
        self.assertEqual(tools['sk_one']['parameters']['properties']['x']['type'], 'string')

    def test_code_overrides_manifest_parameters(self):
        root = self.package({
            'skill.json': json.dumps({'tools': [{'id': 'both', 'description': '清单描述'}]}),
            'index.js': (
                "api.registerTool({ id: 'both',\n"
                "  parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n"
            ),
        })
        tools = tool_schema.extract_tools(root)
        self.assertEqual(tools['both']['source'], 'code')
        self.assertEqual(tools['both']['description'], '清单描述')
        self.assertEqual(sorted(tools['both']['parameters']['properties']), ['q'])

    def test_broken_manifest_does_not_raise(self):
        root = self.package({'skill.json': '{not json', 'index.js': 'api.registerTool({ id: "ok" });'})
        self.assertIn('ok', tool_schema.extract_tools(root))


class SchemaForTest(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.package({
            'index.js': (
                "api.registerTool({ id: 'wiki-query', name: 'Wiki',\n"
                "  parameters: { type: 'object', properties: { query: { type: 'string' } } } });\n"
                "api.registerTool({ id: 'no_schema' });\n"
                "api.registerTool({ id: 'flat', parameters: { q: { type: 'string' } } });\n"
            ),
        })

    def test_exact_and_cleaned_name_hit(self):
        for key in ('wiki-query', 'wiki_query'):
            params = tool_schema.schema_for(self.root, key)
            self.assertEqual(sorted(params['properties']), ['query'], key)

    def test_missing_tool_returns_empty_schema(self):
        params = tool_schema.schema_for(self.root, 'nope')
        self.assertEqual(params, EMPTY)

    def test_empty_schema_is_a_copy(self):
        params = tool_schema.schema_for(self.root, 'nope')
        params['properties']['injected'] = {'type': 'string'}
        self.assertEqual(tool_schema.EMPTY_SCHEMA, EMPTY)

    def test_fallback_is_returned_as_is(self):
        fallback = {'type': 'object', 'properties': {'z': {'type': 'integer'}}}
        self.assertIs(tool_schema.schema_for(self.root, 'nope', fallback), fallback)
        self.assertEqual(tool_schema.schema_for(self.root, 'nope', 'not-a-dict'), EMPTY)

    def test_tool_without_parameters_falls_back(self):
        self.assertEqual(tool_schema.schema_for(self.root, 'no_schema'), EMPTY)

    def test_flat_properties_get_wrapped(self):
        params = tool_schema.schema_for(self.root, 'flat')
        self.assertEqual(params['type'], 'object')
        self.assertEqual(sorted(params['properties']), ['q'])

    def test_non_directory_returns_empty(self):
        self.assertEqual(tool_schema.extract_tools(self.tmp / 'missing'), {})
        self.assertEqual(tool_schema.schema_for(self.tmp / 'missing', 'x'), EMPTY)
        self.assertEqual(tool_schema.tool_catalog(self.tmp / 'missing'), [])


class CatalogTest(TempCase):
    def test_shape_and_sorting(self):
        root = self.package({
            'index.js': (
                "api.registerTool({ id: 'zzz', description: 'Z',\n"
                "  parameters: { type: 'object', properties: { a: { type: 'string' } } } });\n"
                "api.registerTool({ id: 'aaa', description: 'A' });\n"
            ),
        })
        catalog = tool_schema.tool_catalog(root)
        self.assertEqual([item['name'] for item in catalog], ['aaa', 'zzz'])
        for item in catalog:
            self.assertEqual(sorted(item), ['description', 'name', 'parameters'])
            self.assertIsInstance(item['parameters'], dict)
        self.assertEqual(catalog[0]['parameters'], EMPTY)
        self.assertEqual(catalog[1]['parameters']['properties']['a']['type'], 'string')

    def test_empty_directory(self):
        root = self.tmp / 'empty'
        root.mkdir()
        self.assertEqual(tool_schema.tool_catalog(root), [])


class IdempotencyTest(TempCase):
    def test_repeated_calls_are_equal(self):
        root = self.package({'index.js': WIKI_SOURCE})
        first = tool_schema.extract_tools(root)
        second = tool_schema.extract_tools(root)
        self.assertEqual(first, second)
        self.assertEqual(tool_schema.schema_for(root, 'wiki_query'),
                         tool_schema.schema_for(root, 'wiki_query'))
        self.assertEqual(tool_schema.tool_catalog(root), tool_schema.tool_catalog(root))


class ClosureTest(TempCase):
    def test_relative_require_is_followed(self):
        root = self.package({
            'index.js': (
                "const tools = require('./lib/tools');\n"
                "api.registerTool({ id: 'entry_tool', parameters: { type: 'object', properties: { a: { type: 'string' } } } });\n"
            ),
            'lib/tools.js': (
                "api.registerTool({ id: 'lib_tool', parameters: { type: 'object', properties: { b: { type: 'string' } } } });\n"
            ),
        })
        tools = tool_schema.extract_tools(root)
        self.assertEqual(sorted(tools), ['entry_tool', 'lib_tool'])

    def test_import_without_extension_is_followed(self):
        root = self.package({
            'index.js': "import { x } from './modules/impl';\napi.registerTool({ id: 'top' });\n",
            'modules/impl.js': "api.registerTool({ id: 'deep', parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n",
        })
        self.assertIn('deep', tool_schema.extract_tools(root))

    def test_entry_files_override(self):
        root = self.package({
            'index.js': "api.registerTool({ id: 'ignored' });\n",
            'custom.js': "api.registerTool({ id: 'chosen' });\n",
        })
        self.assertEqual(sorted(tool_schema.extract_tools(root, ['custom.js'])), ['chosen'])
        self.assertEqual(sorted(tool_schema.extract_tools(root)), ['ignored'])

    def test_manifest_entry_is_honoured(self):
        root = self.package({
            'skill.json': json.dumps({'entry': 'src/boot.js'}),
            'src/boot.js': "api.registerTool({ id: 'booted' });\n",
        })
        self.assertIn('booted', tool_schema.extract_tools(root))

    def test_file_limits_are_respected(self):
        root = self.package({
            'index.js': "require('./a');\nrequire('./b');\nrequire('./c');\napi.registerTool({ id: 'root_tool' });\n",
            'a.js': "api.registerTool({ id: 'a_tool' });\n",
            'b.js': "api.registerTool({ id: 'b_tool' });\n",
            'c.js': "api.registerTool({ id: 'c_tool' });\n",
        })
        with mock.patch.object(tool_schema, 'MAX_FILES', 2):
            self.assertLessEqual(len(tool_schema._closure(root, ['index.js'])), 2)
            tools = tool_schema.extract_tools(root)
        self.assertIn('root_tool', tools)

    def test_oversized_file_is_skipped(self):
        root = self.package({'index.js': "api.registerTool({ id: 'big' });\n" + 'x' * 2000})
        with mock.patch.object(tool_schema, 'MAX_FILE_BYTES', 100):
            self.assertEqual(tool_schema.extract_tools(root), {})

    def test_time_budget_stops_after_first_file(self):
        root = self.package({
            'index.js': "require('./a');\nrequire('./b');\napi.registerTool({ id: 'first' });\n",
            'a.js': "api.registerTool({ id: 'a_tool' });\n",
            'b.js': "api.registerTool({ id: 'b_tool' });\n",
        })
        # Windows 的 time.monotonic() 粒度约 15ms，真跑小包恒为 0.0ms，只能喂假时钟。
        clock = mock.Mock(side_effect=[0.0, 0.001, 5.0, 5.0])
        with mock.patch.object(tool_schema, 'time', mock.Mock(monotonic=clock)):
            tools = tool_schema.extract_tools(root)
        self.assertIn('first', tools)
        self.assertNotIn('b_tool', tools)


class ScanStatsTest(TempCase):
    def test_reports_reason_for_empty_and_missing(self):
        empty = self.tmp / 'empty'
        empty.mkdir()
        self.assertEqual(tool_schema.scan_stats(empty)['reason'], 'no-entry-file')
        self.assertEqual(tool_schema.scan_stats(self.tmp / 'gone')['reason'], 'not-a-directory')

    def test_counts_tools_with_parameters(self):
        root = self.package({
            'index.js': (
                "api.registerTool({ id: 'with_schema', parameters: { type: 'object', properties: { q: { type: 'string' } } } });\n"
                "api.registerTool({ id: 'no_schema' });\n"
            ),
        })
        stats = tool_schema.scan_stats(root)
        self.assertEqual(stats['files'], ['index.js'])
        self.assertEqual(stats['calls'], 2)
        self.assertEqual(stats['tools'], 2)
        self.assertEqual(stats['withParameters'], 1)
        self.assertEqual(stats['reason'], '')
        self.assertIsInstance(stats['elapsedMs'], float)
