#!/usr/bin/env python3
"""下载外部 QQ Agent Skill 市场（approved）到本机，并生成说明与清单。

只下载、不执行；zip 原样保存，避免破坏 skill 自带的 panel/service 文件。
"""
from __future__ import annotations
import argparse, concurrent.futures, json, re, subprocess, tempfile, urllib.request, zipfile
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
API='https://kondius.cn/api/community/skills'
OUT=Path(r'D:\聊天机器人\插件市场\Skill市场')
NODE='node'

def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'WhaleGirl-Skill-Audit/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r: return json.loads(r.read().decode('utf-8'))

def download(item):
    target=OUT/f"{item['id']}.zip"
    if target.is_file() and target.stat().st_size: return item,target
    url='https://kondius.cn'+str(item.get('downloadUrl') or '')
    req=urllib.request.Request(url,headers={'User-Agent':'WhaleGirl-Skill-Audit/1.0'})
    with urllib.request.urlopen(req,timeout=30) as r: data=r.read()
    target.write_bytes(data); return item,target

def safe_name(name):
    return ''.join(ch for ch in str(name) if ch not in '\\/:*?"<>|').strip()

def inspect(item,path):
    result={'ok':False,'manifest':{},'problems':[],'syntax':'unknown','host_imports':[],'external':[],'files':0}
    try:
        with zipfile.ZipFile(path) as z:
            names=[n for n in z.namelist() if not n.endswith('/')]
            result['files']=len(names)
            if any('..' in Path(n).parts or Path(n).is_absolute() for n in names):
                result['problems'].append('不安全路径'); return result
            manifests=[n for n in names if Path(n).name in ('skill.json','plugin.json')]
            if not manifests:
                result['problems'].append('缺少 skill.json/plugin.json'); return result
            result['manifest']=json.loads(z.read(manifests[0]).decode('utf-8'))
            js=[n for n in names if n.endswith(('.js','.mjs'))]
            if not js:
                result['problems'].append('缺少 JS 入口'); return result
            entry=next((n for n in js if Path(n).name=='index.js'),js[0])
            code=z.read(entry).decode('utf-8','replace')
            result['host_imports']=sorted(set(re.findall(r"from\s+['\"]([^'\"]*src/[^'\"]*)['\"]",code)))
            result['external']=sorted(set(m for m in re.findall(r"from\s+['\"]([^'\"]+)['\"]",code) if not m.startswith(('.','node:'))))
            with tempfile.TemporaryDirectory() as tmp:
                f=Path(tmp)/'index.mjs'; f.write_text(code,encoding='utf-8')
                p=subprocess.run([NODE,'--check',str(f)],capture_output=True,text=True)
                result['syntax']='ok' if p.returncode==0 else 'error'
                if p.returncode: result['problems'].append('JS 语法错误：'+(p.stderr or '')[:200])
        result['ok']=not result['problems']; return result
    except Exception as exc:
        result['problems'].append(f'{type(exc).__name__}: {exc}'); return result

def guide(item,result):
    m=result['manifest']; lines=[f"# {m.get('name') or item['name']}（{m.get('id') or item['id']}）",'',
      f"- 版本：{m.get('version') or item.get('version')}",f"- 分类：{m.get('category') or item.get('category')}",
      f"- 默认启用：{'是' if m.get('enabledByDefault') else '否'}",'','## 功能','',str(m.get('description') or item.get('description') or '').strip(),'',
      '## 安装','','这是 QQ Agent 0.4 的 Skill 包（`skill.json` + `index.js`），不是鲸鱼娘控制台的 Python 插件。',
      '在 QQ Agent 中：解压到它的 `skills/<id>/` 目录，重启/热重载后在该 Skill 列表启用。','',
      '如果要在鲸鱼娘控制台使用，需要把功能移植为 Python 插件，不能直接导入本 zip。','']
    settings=m.get('settings') or {}
    schema=m.get('configSchema') or {}
    if settings or schema:
        lines+=['## 配置项','','| 键 | 默认值 | 说明 |','|---|---|---|']
        for key,default in settings.items():
            entry=schema.get(key) if isinstance(schema,dict) else None
            desc=str((entry or {}).get('description') or '') if isinstance(entry,dict) else ''
            lines.append(f"| `{key}` | `{default}` | {desc.replace('|','／').replace(chr(10),' ')} |")
        lines.append('')
    lines+=['## 兼容性','',f"- JS 语法：{result['syntax']}",f"- 宿主依赖：{', '.join(result['host_imports']) or '无'}",
            f"- 外部模块：{', '.join(result['external']) or '无'}",'']
    return '\n'.join(lines)

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--workers',type=int,default=12); args=parser.parse_args()
    data=get_json(API); items=[i for i in data.get('items',[]) if i.get('status')=='approved']
    OUT.mkdir(parents=True,exist_ok=True); (OUT/'使用说明').mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(download, items))
    rows=[]
    for item,path in results:
        result=inspect(item,path)
        safe=safe_name(item['name']); target=OUT/f"{safe}-{item['id']}.zip"
        if path!=target:
            if target.exists(): target.unlink()
            path.replace(target)
        (OUT/'使用说明'/f"{safe}-{item['id']}.md").write_text(guide(item,result),encoding='utf-8')
        rows.append({'id':item['id'],'name':item['name'],'version':item.get('version'),'category':item.get('category'),
                     'file':target.name,'ok':result['ok'],'syntax':result['syntax'],'host_imports':result['host_imports'],
                     'external':result['external'],'problems':result['problems'],'files':result['files']})
    (OUT/'index.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# QQ Agent Skill 市场（approved）','',f'- 来源：{API}',f'- 下载：{len(rows)} 个',f'- 语法通过：{sum(1 for r in rows if r["syntax"]=="ok")}',f'- 结构通过：{sum(1 for r in rows if r["ok"])}','',
           '| Skill | 标识 | 分类 | 文件 | 语法 | 结构 | 宿主依赖 |','|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r['name']} | `{r['id']}` | {r['category']} | {r['file']} | {r['syntax']} | {'通过' if r['ok'] else '需检查'} | {', '.join(r['host_imports'][:2]) or '无'} |")
    (OUT/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'downloaded':len(rows),'syntax_ok':sum(1 for r in rows if r['syntax']=='ok'),'valid':sum(1 for r in rows if r['ok']),'cats':Counter(r['category'] for r in rows)},ensure_ascii=False,indent=2))
    print('输出：',OUT)
if __name__=='__main__': main()
