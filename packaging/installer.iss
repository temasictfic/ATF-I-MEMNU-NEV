; Inno Setup 6 script for Atf-ı Memnu (Kaynakça Kontrol Sistemi).
; Replaces electron-builder's NSIS target.
;
; Built by scripts/build.ps1, which first generates:
;   packaging/build/app.iss              version + NER model manifest (#defines)
;   packaging/build/MicrosoftEdgeWebview2Setup.exe   Evergreen bootstrapper
;   packaging/dist/KaynakcaKontrolSistemi/          PyInstaller one-folder build
;
; Install behaviour:
;   * Per-user install (no admin) into %LOCALAPPDATA%\Programs, with a
;     changeable directory. Same model as the old NSIS installer.
;   * Installs the WebView2 Runtime if it is missing. It ships with
;     Windows 11 and current Windows 10, so this is normally a no-op.
;   * Downloads the 125 MB citation NER model once into
;     %LOCALAPPDATA%\atfi-memnu-app\models\..., outside the install dir, so
;     every later update reuses it. If the old Electron app is installed, its
;     bundled copy is reused (SHA-256 checked) instead of downloading. A
;     failed download is not fatal: the app retries in the background.
;   * Offers to remove an old Electron-based install. Settings, API keys and
;     caches in %APPDATA%\atfi-memnu-app are kept.
;   * /SILENT /RELAUNCH (used by the in-app updater) restarts the app after
;     installing.

#include "build\app.iss"

#if ModelFileCount != 3
  #error The [Code] section below handles exactly 3 model files; update ModelFileName/ModelFileSha/ModelFileUrl.
#endif

#define AppName "Kaynakça Kontrol Sistemi"
#define AppExe "KaynakcaKontrolSistemi.exe"
#define AppPublisher "temasictfic"
#define AppURL "https://github.com/temasictfic/ATF-I-MEMNU-NEV"
#define WebView2ClientKey "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{6E9B3C1A-5B8E-4E8B-9F0A-7A3C2D1E4F50}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={localappdata}\Programs\Kaynakca Kontrol Sistemi
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=Kaynakca-Kontrol-Sistemi-Setup-{#AppVersion}
SetupIconFile=..\resources\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
turkish.DesktopIcon=Masaüstü kısayolu oluştur
english.DesktopIcon=Create a desktop shortcut
turkish.ModelCaption=Kaynakça modeli indiriliyor
english.ModelCaption=Downloading the citation model
turkish.ModelDescription=Kaynakça ayrıştırma modeli (yaklaşık 125 MB) yalnızca bir kez indirilir; sonraki güncellemeler aynı modeli kullanır.
english.ModelDescription=The citation parser model (about 125 MB) is downloaded only once; later updates reuse it.
turkish.ModelFailed=Model şu anda indirilemedi. Uygulama ilk açılışta indirmeyi arka planda yeniden deneyecek.%n%n%1
english.ModelFailed=The model could not be downloaded right now. The app will retry in the background on first launch.%n%n%1
turkish.WebView2Installing=Microsoft Edge WebView2 Runtime kuruluyor...
english.WebView2Installing=Installing the Microsoft Edge WebView2 Runtime...
turkish.RemoveLegacy=Bilgisayarda uygulamanın eski (Electron tabanlı) sürümü kurulu. Kaldırılsın mı?%n%nAyarlarınız, API anahtarlarınız ve kayıtlarınız korunur.
english.RemoveLegacy=An older (Electron-based) version of the app is installed. Remove it?%n%nYour settings, API keys and records are kept.

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "dist\KaynakcaKontrolSistemi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
; Machine-local, re-creatable data only. The NER model and the roaming
; settings in %APPDATA% stay, so a reinstall is instant and nothing is lost.
Type: filesandordirs; Name: "{localappdata}\atfi-memnu-app\updates"
Type: filesandordirs; Name: "{localappdata}\atfi-memnu-app\logs"

[Code]
var
  ModelDownloadPage: TDownloadWizardPage;
  ModelReady: Boolean;

function ShouldRelaunch(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RELAUNCH') = 0 then
      Result := True;
end;

{ ---------------- WebView2 runtime ---------------- }

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'))
    or (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'))
    or (RegQueryStringValue(HKCU, '{#WebView2ClientKey}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

procedure EnsureWebView2();
var
  ResultCode: Integer;
begin
  if WebView2Installed() then
    Exit;
  WizardForm.StatusLabel.Caption := CustomMessage('WebView2Installing');
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('WebView2 bootstrapper failed to start');
end;

{ ---------------- NER model (downloaded once) ---------------- }

function ModelDir(): String;
begin
  Result := ExpandConstant('{localappdata}\atfi-memnu-app\models\{#ModelName}\{#ModelVersion}');
end;

function ModelFileName(I: Integer): String;
begin
  case I of
    0: Result := '{#ModelFile0Name}';
    1: Result := '{#ModelFile1Name}';
    2: Result := '{#ModelFile2Name}';
  end;
end;

function ModelFileSha(I: Integer): String;
begin
  case I of
    0: Result := '{#ModelFile0Sha}';
    1: Result := '{#ModelFile1Sha}';
    2: Result := '{#ModelFile2Sha}';
  end;
end;

function ModelFileUrl(I: Integer; Mirror: Boolean): String;
begin
  if Mirror then
    case I of
      0: Result := '{#ModelFile0Mirror}';
      1: Result := '{#ModelFile1Mirror}';
      2: Result := '{#ModelFile2Mirror}';
    end
  else
    case I of
      0: Result := '{#ModelFile0Url}';
      1: Result := '{#ModelFile1Url}';
      2: Result := '{#ModelFile2Url}';
    end;
end;

function ModelAlreadyInstalled(): Boolean;
var
  Marker: AnsiString;
  I: Integer;
begin
  Result := LoadStringFromFile(ModelDir() + '\.verified', Marker) and (Trim(String(Marker)) = '{#ModelDigest}');
  if Result then
    for I := 0 to {#ModelFileCount} - 1 do
      if not FileExists(ModelDir() + '\' + ModelFileName(I)) then
        Result := False;
end;

function LegacyModelDir(): String;
var
  Candidates: array[0..2] of String;
  I: Integer;
begin
  Result := '';
  Candidates[0] := ExpandConstant('{localappdata}\Programs\{#AppName}\resources\backend\_internal\models\citation-ner-int8');
  Candidates[1] := ExpandConstant('{localappdata}\Programs\kaynakca-kontrol-sistemi\resources\backend\_internal\models\citation-ner-int8');
  Candidates[2] := ExpandConstant('{localappdata}\Programs\atfi-memnu-app\resources\backend\_internal\models\citation-ner-int8');
  for I := 0 to 2 do
    if FileExists(Candidates[I] + '\' + ModelFileName(0)) then
    begin
      Result := Candidates[I];
      Exit;
    end;
end;

function AdoptLegacyModel(): Boolean;
var
  Source: String;
  I: Integer;
begin
  Result := False;
  Source := LegacyModelDir();
  if Source = '' then
    Exit;
  ForceDirectories(ModelDir());
  for I := 0 to {#ModelFileCount} - 1 do
  begin
    if not FileExists(Source + '\' + ModelFileName(I)) then
      Exit;
    if CompareText(GetSHA256OfFile(Source + '\' + ModelFileName(I)), ModelFileSha(I)) <> 0 then
      Exit;
    if not FileCopy(Source + '\' + ModelFileName(I), ModelDir() + '\' + ModelFileName(I), False) then
      Exit;
  end;
  Result := True;
end;

function OnModelProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

function DownloadModel(Mirror: Boolean): String;
var
  I: Integer;
begin
  Result := '';
  ModelDownloadPage.Clear;
  for I := 0 to {#ModelFileCount} - 1 do
    ModelDownloadPage.Add(ModelFileUrl(I, Mirror), ModelFileName(I), ModelFileSha(I));
  try
    ModelDownloadPage.Download;
  except
    Result := GetExceptionMessage;
  end;
end;

procedure InitializeWizard();
begin
  ModelDownloadPage := CreateDownloadPage(CustomMessage('ModelCaption'), CustomMessage('ModelDescription'), @OnModelProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Error: String;
begin
  Result := True;
  if (CurPageID <> wpReady) or ModelReady then
    Exit;
  if ModelAlreadyInstalled() or AdoptLegacyModel() then
  begin
    ModelReady := True;
    Exit;
  end;
  ModelDownloadPage.Show;
  try
    Error := DownloadModel(False);
    if Error <> '' then
      Error := DownloadModel(True);  { fall back to the mirror }
    if Error = '' then
      ModelReady := True
    else if not WizardSilent then
      MsgBox(FmtMessage(CustomMessage('ModelFailed'), [Error]), mbInformation, MB_OK);
  finally
    ModelDownloadPage.Hide;
  end;
end;

procedure InstallModelFiles();
var
  I: Integer;
  Digest: String;
begin
  if not ModelReady then
    Exit;
  if ModelAlreadyInstalled() then
    Exit;
  ForceDirectories(ModelDir());
  for I := 0 to {#ModelFileCount} - 1 do
    if FileExists(ExpandConstant('{tmp}\') + ModelFileName(I)) then
      FileCopy(ExpandConstant('{tmp}\') + ModelFileName(I), ModelDir() + '\' + ModelFileName(I), False);
  for I := 0 to {#ModelFileCount} - 1 do
    if not FileExists(ModelDir() + '\' + ModelFileName(I)) then
      Exit;
  Digest := '{#ModelDigest}';
  SaveStringToFile(ModelDir() + '\.verified', Digest, False);
end;

{ ---------------- Old Electron install ---------------- }

procedure OfferLegacyUninstall();
var
  Keys: TArrayOfString;
  I, ResultCode: Integer;
  Root, DisplayName, Publisher, Quiet: String;
begin
  Root := 'Software\Microsoft\Windows\CurrentVersion\Uninstall';
  if not RegGetSubkeyNames(HKCU, Root, Keys) then
    Exit;
  for I := 0 to GetArrayLength(Keys) - 1 do
  begin
    if CompareText(Keys[I], '{6E9B3C1A-5B8E-4E8B-9F0A-7A3C2D1E4F50}_is1') = 0 then
      Continue;
    if not RegQueryStringValue(HKCU, Root + '\' + Keys[I], 'DisplayName', DisplayName) then
      Continue;
    RegQueryStringValue(HKCU, Root + '\' + Keys[I], 'Publisher', Publisher);
    if (Pos('{#AppName}', DisplayName) <> 1) or (CompareText(Publisher, '{#AppPublisher}') <> 0) then
      Continue;
    if not RegQueryStringValue(HKCU, Root + '\' + Keys[I], 'QuietUninstallString', Quiet) then
      Continue;
    if WizardSilent or (MsgBox(CustomMessage('RemoveLegacy'), mbConfirmation, MB_YESNO) <> IDYES) then
      Exit;
    { electron-builder's per-user uninstaller keeps %APPDATA% data by default. }
    Exec(ExpandConstant('{cmd}'), '/C "' + Quiet + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exit;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    OfferLegacyUninstall();
    EnsureWebView2();
  end;
  if CurStep = ssPostInstall then
    InstallModelFiles();
end;
