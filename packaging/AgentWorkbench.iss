#define AppVersion "0.4.1"

[Setup]
AppId={{AB1AF47D-451C-4C53-8F6D-25016B11DE06}
AppName=Agent Workbench
AppVersion={#AppVersion}
AppPublisher=Agent Workbench Contributors
AppPublisherURL=https://github.com/zznmdhz/agent-workbench
DefaultDirName={localappdata}\Programs\AgentWorkbench
DefaultGroupName=Agent Workbench
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AgentWorkbench-Setup-{#AppVersion}-Windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
UninstallDisplayName=Agent Workbench
CloseApplications=force
RestartApplications=no

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Request: Variant;
  I: Integer;
begin
  Result := '';
  { Ask the running desktop server to exit before replacing its executable. }
  try
    Request := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Request.SetTimeouts(1000, 1000, 1000, 1000);
    Request.Open('POST', 'http://127.0.0.1:8765/auth/close-local', False);
    Request.Send('');
    for I := 1 to 20 do Sleep(250);
  except
    { No server is running, or Restart Manager will close a legacy process. }
  end;
end;

[Files]
Source: "..\.local\package-build\AgentWorkbench\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\docs\third_party\CC_SWITCH_LICENSE.txt"; DestDir: "{app}"; DestName: "THIRD-PARTY-CC-SWITCH-LICENSE.txt"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\AgentWorkbenchReset.exe"
Type: files; Name: "{group}\重设管理员密码.lnk"

[Icons]
Name: "{group}\Agent Workbench"; Filename: "{app}\AgentWorkbench.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Agent Workbench"; Filename: "{app}\AgentWorkbench.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "其他选项："; Flags: unchecked

[Run]
Filename: "{app}\AgentWorkbench.exe"; Description: "启动 Agent Workbench"; Flags: nowait postinstall skipifsilent
