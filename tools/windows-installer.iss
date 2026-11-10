; Inno Setup script for vcfa-import. Built by tools/build.py (build_windows_setup),
; which passes MyAppVersion and SourceDir on the command line:
;
;     ISCC.exe /DMyAppVersion=1.1.1 /DSourceDir=dist\onedir\vcfa-import tools\windows-installer.iss
;
; Per-user install (no admin rights needed): PrivilegesRequired=lowest, DefaultDirName
; resolves under the user's own Programs folder in that case. [Code] below adds the
; install directory to this user's PATH on install and removes it on uninstall -- it is
; a CLI tool, so "installed" should mean "runs from any prompt", not just "has a shortcut".
;
; LicenseFile is the Apache 2.0 licence (the normal "I accept this software's licence"
; step every installer has). DISCLAIMER.md -- vcfa-import's own usage/risk terms -- is
; shown as an informational page only; it is NOT accepted here. The tool's own gate
; (`vcfa-import accept`, vcfaimport/legal.py) is the one place acceptance is ever
; recorded, and it always runs again at first use. Never add a second place that does.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\onedir\vcfa-import"
#endif
#ifndef IcoFile
  #define IcoFile "..\dist\vcfa-import.ico"
#endif
#define MyAppName "vcfa-import"
#define MyAppPublisher "vcfa-import project"
#define MyAppURL "https://github.com/yaazer/vcfa-import-project"
#define MyAppExeName "vcfa-import.exe"

[Setup]
AppId={{2E6F3C9A-9C7A-4C3E-9E2E-2F6C2B3C7E41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
; Broadcasts WM_SETTINGCHANGE after install/uninstall so a new PATH entry (below)
; is picked up without logging off -- Inno's own directive for this, not a script call.
ChangesEnvironment=yes
LicenseFile=..\LICENSE
InfoBeforeFile=..\DISCLAIMER.md
OutputDir=..\dist
OutputBaseFilename=vcfa-import-{#MyAppVersion}-windows-amd64-setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile={#IcoFile}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LAB-GUIDE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\DISCLAIMER.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{cmd}"; Parameters: "/K ""{app}\{#MyAppExeName}"" --version"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Open a prompt with {#MyAppName} ready to use"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Code]
const
  EnvironmentKey = 'Environment';

procedure EnvAddPath(Dir: string);
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Paths := '';
  if Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0 then
    exit;
  if (Paths <> '') and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  Paths := Paths + Dir;
  // Path is conventionally REG_EXPAND_SZ (it may itself contain %OTHERVAR%
  // entries); writing it back as plain REG_SZ would stop those expanding.
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths);
end;

procedure EnvRemovePath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    exit;
  P := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    exit;
  if P = 1 then
    Delete(Paths, 1, Length(Dir) + 1)
  else
    Delete(Paths, P - 1, Length(Dir) + 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    EnvAddPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    EnvRemovePath(ExpandConstant('{app}'));
end;
