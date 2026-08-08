#define MyAppName "BraXYTDow"
#define MyAppVersion "3.2.0"
#define MyAppPublisher "Richard Ittou"
#define MyAppExeName "BraXYTDow.exe"

[Setup]
AppId={{C76EBB9A-6D7D-4DA7-8C65-B3D9521AB7D9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://www.instagram.com/richard.ittou?igsh=c210bHhzdzJwcWg1
AppSupportURL=https://www.instagram.com/richard.ittou?igsh=c210bHhzdzJwcWg1
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}.0
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=BraXYTDow-Setup-{#MyAppVersion}-x64
SetupIconFile=..\assets\baixatube.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsedUserAreasWarning=no
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
SetupLogging=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Files]
Source: "..\dist\BraXYTDow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; AppUserModelID: "RichardIttou.BraXYTDow"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon; AppUserModelID: "RichardIttou.BraXYTDow"

[Registry]
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\RichardIttou.BraXYTDow"; ValueType: string; ValueName: "DisplayName"; ValueData: "BraXYTDow"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\RichardIttou.BraXYTDow"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\_internal\assets\baixatube-icon.png"

[InstallDelete]
Type: files; Name: "{app}\BaixaTube.exe"
Type: files; Name: "{autoprograms}\BaixaTube.lnk"
Type: files; Name: "{autodesktop}\BaixaTube.lnk"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos adicionais:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName}"; Flags: nowait postinstall
