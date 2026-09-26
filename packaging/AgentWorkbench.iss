#define AppVersion "0.2.0"

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
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\.local\package-build\AgentWorkbench\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Agent Workbench"; Filename: "{app}\AgentWorkbench.exe"; Parameters: "open --db ""{localappdata}\AgentWorkbench\data\agent-workbench.db"""; WorkingDir: "{app}"
Name: "{autodesktop}\Agent Workbench"; Filename: "{app}\AgentWorkbench.exe"; Parameters: "open --db ""{localappdata}\AgentWorkbench\data\agent-workbench.db"""; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "其他选项："; Flags: unchecked

[Run]
Filename: "{app}\AgentWorkbench.exe"; Parameters: "open --db ""{localappdata}\AgentWorkbench\data\agent-workbench.db"""; Description: "启动 Agent Workbench"; Flags: nowait postinstall skipifsilent
