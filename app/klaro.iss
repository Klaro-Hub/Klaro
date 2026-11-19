#ifndef MyAppVersion
  #define MyAppVersion "1.2.0"
#endif
#define MyAppName "Klaro"

[Setup]
AppId={{8F3A2C71-6D4B-4E9A-B5C0-2A7D1E9F4B36}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Klaro
DefaultDirName={autopf}\Klaro
DefaultGroupName=Klaro
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=KlaroLauncherSetup
SetupIconFile=klaro.ico
UninstallDisplayIcon={app}\Klaro.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Klaro\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Klaro"; Filename: "{app}\Klaro.exe"
Name: "{autodesktop}\Klaro"; Filename: "{app}\Klaro.exe"; Tasks: desktopicon

[Run]
; Instalación normal: ofrece abrir Klaro al terminar
Filename: "{app}\Klaro.exe"; Description: "{cm:LaunchProgram,Klaro}"; Flags: nowait postinstall skipifsilent
; Actualización automática (en silencio): vuelve a abrir Klaro sola
Filename: "{app}\Klaro.exe"; Flags: nowait skipifnotsilent
