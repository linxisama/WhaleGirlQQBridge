#define AppName "鲸鱼娘 QQ 账号版"
; 版本号由 build_installer.ps1 用 /DAppVersion=x.y.z 传入（取自 src/runtime_identity.py），
; 直接双击编译时用下面的默认值。这样"代码版本"与"安装包版本"不会再各写一份。
#ifndef AppVersion
  #define AppVersion "5.2.100"
#endif
[Setup]
AppId={{CEA15F06-D0AE-4A7B-9442-DC8DEFAFEE54}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\WhaleGirlQQBridge
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer
OutputBaseFilename=WhaleGirlQQBridge-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\WhaleGirlQQBridge.exe
; 旧版控制台在运行时，Setup 用 Restart Manager 关它会失败，于是弹出
; “Setup was unable to automatically close all applications / Select action”，
; 用户只能手动去任务管理器结束进程才能继续升级。
; 原因：控制台 exe 运行时还会拉起网关等子进程（旧 onefile 包则是有窗口的子进程 +
; 无窗口的引导父进程），父/子进程收不到 WM_CLOSE，Restart Manager 的平稳关闭就报失败。
; force 让 Setup 改用强制关闭（RM 对不响应的进程会在超时后强制结束），不用再人工点选。
; 占用检查沿用 Inno 默认的 CloseApplicationsFilter（*.exe,*.dll,*.chm），不额外改写。
CloseApplications=force
; 不做自动重启：装完由下面 [Run] 里的“启动控制台”负责，避免同时冒出两个窗口。
RestartApplications=no
DisableProgramGroupPage=yes
; 静默安装（/VERYSILENT）不能沿用上一版勾选过的任务：Inno 的 UsePreviousTasks 默认开启，
; 会把上版安装时勾过的 desktopicon 带进这一版，于是静默升级照样建出桌面快捷方式。
; 关掉它以后，交互安装仍然可以自己勾桌面图标（下面 [Tasks] 的 desktopicon 保留，[Icons] 照旧绑定它）。
UsePreviousTasks=no
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Files]
; onedir 包：dist\<ver>\WhaleGirlQQBridge\ 下是 WhaleGirlQQBridge.exe + _internal\（PyInstaller 运行时依赖）。
; 整棵树递归拷进 {app}，所以装完是 {app}\WhaleGirlQQBridge.exe 与 {app}\_internal\... 的展开结构，
; 更新包（tools/build_update.py）才能只替换变化的那几个文件，而不是每次重下整包。
Source: "dist\{#AppVersion}\WhaleGirlQQBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 安装目录里放一份 version.txt，更新包（tools/build_update.py）据此判断基础版本对不对得上。
Source: "version.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "对话与电脑操控使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "QQ账号版使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "GPT-SoVITS语音接口说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "快速语音接入说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "快速语音配置示例.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "安装使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "安全模式启动.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "收集诊断.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "收集诊断.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "功能对照与使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "插件本地文件接入说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "插件功能与接入说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "third_party\memes\*"; DestDir: "{app}\third_party\memes"; Flags: ignoreversion
Source: "third_party\estrella\*"; DestDir: "{app}\third_party\estrella"; Flags: ignoreversion
Source: "third_party\mio\*"; DestDir: "{app}\third_party\mio"; Flags: ignoreversion
Source: "新版界面使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "控制台语音接入说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "docs\控制台操作指南.md"; DestDir: "{app}\docs"; Flags: ignoreversion
[InstallDelete]
; 只移除上版内置网页入口，不触碰数据目录与任何用户文件。
Type: files; Name: "{app}\浏览器兼容启动.cmd"
; 5.2.112 起不再随包发 Qt6Pdf（全项目无引用）与多余语言包。覆盖安装时旧文件不会自己消失，
; 必须在这里点名删掉，否则升级后体积只增不减；[InstallDelete] 在 [Files] 之前执行，
; 保留下来的 4 个 .qm 会被随后的 [Files] 重新铺回去。
Type: files; Name: "{app}\_internal\PyQt6\Qt6\bin\Qt6Pdf.dll"
Type: files; Name: "{app}\_internal\PyQt6\Qt6\plugins\imageformats\qpdf.dll"
Type: files; Name: "{app}\_internal\PyQt6\Qt6\translations\*.qm"
[UninstallDelete]
; 卸载时把 onedir 的 _internal\ 整棵树删掉：里面可能有运行时生成/改名过的文件，
; 不在安装记录里，光靠 [Files] 的卸载清单会留下空目录与残留。
; data\ 等用户数据不在这里（数据目录在程序目录之外），删它不影响用户资料。
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\version.txt"
[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\WhaleGirlQQBridge.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\WhaleGirlQQBridge.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\WhaleGirlQQBridge.exe"; Description: "Launch WhaleGirl QQ"; Flags: nowait postinstall skipifsilent
[Code]
var
  DesktopShortcutWasThere: Boolean;
  LegacyConsoleFound: Boolean;

function LegacyConsolePresent(): Boolean;
var
  DisplayName: String;
  Key: String;
begin
  Result := False;
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{4B9B8779-8646-4D84-9082-BC25B9EC688B}_is1';
  if RegQueryStringValue(HKCU, Key, 'DisplayName', DisplayName) then
    Result := True;
  if (not Result) and RegQueryStringValue(HKLM, Key, 'DisplayName', DisplayName) then
    Result := True;
  if (not Result) and FileExists(ExpandConstant('{localappdata}\Programs\WhaleGirlQQ\WhaleGirlQQ.exe')) then
    Result := True;
  if (not Result) and FileExists(ExpandConstant('{pf32}\WhaleGirlQQ\WhaleGirlQQ.exe')) then
    Result := True;
end;

function ShortcutPath(const Where: String): String;
begin
  Result := ExpandConstant(Where) + '\{#AppName}.lnk';
end;

{ 只在快捷方式缺失时补建，已存在的交给 [Icons] 处理，避免覆盖用户自己改过的名字 }
procedure EnsureShortcut(const Where: String);
var
  Link: String;
begin
  Link := ShortcutPath(Where);
  if FileExists(Link) then
    Exit;
  try
    CreateShellLink(Link, '{#AppName}', ExpandConstant('{app}\WhaleGirlQQBridge.exe'), '',
      ExpandConstant('{app}'), '', 0, SW_SHOWNORMAL);
    Log('已补建快捷方式: ' + Link);
  except
    { 快捷方式失败不该让安装失败，记一条日志即可 }
    Log('无法创建快捷方式 ' + Link + '：' + GetExceptionMessage);
  end;
end;

function InitializeSetup(): Boolean;
begin
  { 桌面图标属于可选项，任务没勾选时 Setup 会删掉上次装的桌面快捷方式；
    所以升级前先记住它在不在，装完补回来。开始菜单那个是非可选项，始终补齐。 }
  LegacyConsoleFound := LegacyConsolePresent();
  DesktopShortcutWasThere := FileExists(ShortcutPath('{autodesktop}'));
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    EnsureShortcut('{autoprograms}');
    if DesktopShortcutWasThere or LegacyConsoleFound then
      EnsureShortcut('{autodesktop}');
    if LegacyConsoleFound and not WizardSilent then
      MsgBox('检测到这台电脑还安装着旧版「鲸鱼娘 QQ 控制台」。新版已安装完成；以后请从「鲸鱼娘 QQ 账号版」启动，旧版不会自动删除。', mbInformation, MB_OK);
  end;
end;
