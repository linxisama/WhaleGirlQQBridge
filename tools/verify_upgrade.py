"""隔离 AppId/快捷方式/数据目录验证旧版带数据覆盖升级，不操作用户正式安装。"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
VERSION=re.search(r"APP_VERSION\s*=\s*'([^']+)'",(ROOT/'src/runtime_identity.py').read_text(encoding='utf-8')).group(1)
BASE='1.6.17'
FOLDER=ROOT/'.tmp-upgrade-verify'/VERSION


def run(args,env=None):
    result=subprocess.run([str(v) for v in args],cwd=ROOT,env=env,capture_output=True,creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError(f'验证程序失败：{args[0]}，退出码 {result.returncode}，输出末尾 {result.stderr[-500:]!r}')


def main():
    FOLDER.mkdir(parents=True,exist_ok=True)
    files_only = '--files-only' in sys.argv
    source=(ROOT/'installer.iss').read_text(encoding='utf-8-sig')
    assert 'AppId={{4B9B8779-8646-4D84-9082-BC25B9EC688B}' in source
    source=source.replace('AppId={{4B9B8779-8646-4D84-9082-BC25B9EC688B}','AppId=WhaleGirlQQ-IsolatedUpgradeVerification')
    source=source.replace('#define AppName "鲸鱼娘 QQ 控制台"','#define AppName "WhaleGirl QQ Upgrade Verification"')
    source=source.replace('OutputDir=installer','OutputDir='+str(FOLDER))
    source=re.sub(r'Source: "([^"]+)"',lambda m:'Source: "'+str(ROOT/m.group(1))+'"',source)
    if files_only:
        source=source.replace('[Setup]', '[Setup]\nCreateUninstallRegKey=no')
        source=re.sub(r'\[Icons\].*?(?=\[Run\])', '', source, flags=re.S)
        source=source.split('[Code]')[0]
    task_temp=FOLDER/'temp';task_temp.mkdir(exist_ok=True)
    os.environ['TEMP']=str(task_temp);os.environ['TMP']=str(task_temp)
    script=FOLDER/'verify.iss';script.write_text(source,encoding='utf-8-sig')
    target=FOLDER/'app'
    profile=FOLDER/'profile'
    env=dict(os.environ,LOCALAPPDATA=str(profile),QT_QPA_PLATFORM='offscreen')
    for version in (BASE,VERSION):
        assert (ROOT/'dist'/version/'WhaleGirlQQ.exe').exists()
        run([ROOT/'.build-tools/InnoSetup/ISCC.exe','/DAppVersion='+version,script])
    data=profile/'WhaleGirlQQ';(data/'data/qq-only').mkdir(parents=True,exist_ok=True)
    config=data/'config.qq.yaml';config.write_text('ai:\n  model: preserved-model\nqq_only:\n  mode: official\n',encoding='utf-8')
    gui=data/'data/gui_config.json';gui.write_text('{"transparency":0.23}',encoding='utf-8')
    plugins=data/'data/qq-only/plugin_settings.json'
    plugins.write_text('{"image_understanding":{"enabled":true,"visionModel":"preserved-vision"}}',encoding='utf-8')
    persona=data/'styles/custom/preserved.yaml';persona.parent.mkdir(parents=True,exist_ok=True)
    persona.write_text('name: preserved-persona\nprompt: keep-this-persona\n',encoding='utf-8')
    dbfile=data/'data/qq-only/messages.sqlite3'
    with sqlite3.connect(dbfile) as db:
        db.execute("CREATE TABLE IF NOT EXISTS inbox(key TEXT PRIMARY KEY,mode TEXT,account TEXT,room TEXT,payload TEXT,state TEXT,reply TEXT,error TEXT,created REAL)")
        db.execute("INSERT OR IGNORE INTO inbox VALUES('preserve','official','fixture','private:test','{}','sent','保留历史','',1)")
    files=[config,gui,plugins,persona,dbfile]
    before={str(p.relative_to(data)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    legacy_browser=target/'浏览器兼容启动.cmd'
    for version in (BASE,VERSION):
        print('Install/upgrade',version,flush=True)
        run([FOLDER/f'WhaleGirlQQ-Setup-{version}.exe','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',f'/DIR={target}',f'/LOG={FOLDER}/install-{version}.log'])
        assert hashlib.sha256((target/'WhaleGirlQQ.exe').read_bytes()).digest()==hashlib.sha256((ROOT/'dist'/version/'WhaleGirlQQ.exe').read_bytes()).digest()
        assert all(hashlib.sha256(p.read_bytes()).hexdigest()==before[str(p.relative_to(data))] for p in files)
        run([target/'WhaleGirlQQ.exe','--self-test',FOLDER/f'selftest-{version}.json'],env=env)
        result=json.loads((FOLDER/f'selftest-{version}.json').read_text(encoding='utf-8'))
        assert result['ok'] and result['frozen']
        if version == BASE:
            # 重现1.6.5安装目录中自带的旧网页入口；新安装包必须只清理这个内置文件。
            legacy_browser.write_bytes(b'@echo off\r\nstart "" "%~dp0WhaleGirlQQ.exe" --browser-mode\r\n')
        else:
            assert not legacy_browser.exists(), '旧网页入口没有被覆盖安装移除'
    with sqlite3.connect(dbfile) as db:
        assert db.execute("SELECT reply FROM inbox WHERE key='preserve'").fetchone()[0]=='保留历史'
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==before[str(p.relative_to(data))] for p in files)
    # 只卸载隔离的测试实例；覆盖升级前没有卸载。
    run([target/'unins000.exe','/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'])
    assert all(p.exists() for p in files)
    (FOLDER/'result.json').write_text(json.dumps({'ok':True,'from':BASE,'to':VERSION,'data_preserved':before,'legacy_browser_entry_removed':not legacy_browser.exists(),'isolated_app_id':True,'official_app_id_unchanged':True,'registry_shortcuts_verified':not files_only},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Upgrade and installed self-tests passed; configuration and history preserved.',flush=True)

if __name__=='__main__':main()
