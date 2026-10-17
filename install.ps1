# AutoSE installer (Windows / PowerShell)
# Installs AutoSE, adds an AutoSE shortcut to your desktop and Start menu, and
# opens the dashboard in your browser.
#
#   irm https://autose.dev/install.ps1 | iex
#
# Run it again at any time to update.
#
# Honors these environment variables:
#   AUTOSE_HOME         where to clone AutoSE          (default: $HOME\.autose-cli)
#   AUTOSE_BIN_DIR      where to place the launchers   (default: $env:LOCALAPPDATA\AutoSE\bin)
#   AUTOSE_REF          git ref to check out           (default: the repo default branch)
#   AUTOSE_REPO         repository to clone            (default: the AutoSE GitHub repo)
#   AUTOSE_NO_SHORTCUT  set to 1 to skip the desktop and Start menu shortcuts
#   AUTOSE_NO_LAUNCH    set to 1 to skip opening the dashboard at the end

$ErrorActionPreference = 'Stop'

$RepoUrl    = if ($env:AUTOSE_REPO)    { $env:AUTOSE_REPO }    else { 'https://github.com/AutoSE-Labs/autose.git' }
$InstallDir = if ($env:AUTOSE_HOME)    { $env:AUTOSE_HOME }    else { Join-Path $HOME '.autose-cli' }
$BinDir     = if ($env:AUTOSE_BIN_DIR) { $env:AUTOSE_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'AutoSE\bin' }
$DataDir    = Join-Path $env:LOCALAPPDATA 'AutoSE'
$Ref        = $env:AUTOSE_REF

function Step($m) {
  Write-Host '> ' -ForegroundColor DarkYellow -NoNewline
  Write-Host $m
}
function Die($m) {
  Write-Host 'error: ' -ForegroundColor DarkYellow -NoNewline
  Write-Host $m
  # `exit` would close the window when this runs through `irm | iex`.
  throw 'AutoSE installation failed.'
}
# Windows PowerShell turns any stderr line from a native command into a
# terminating error under 'Stop', even when it is redirected. git and uv write
# progress and harmless warnings there, so run them under 'Continue' and judge
# them by exit code alone.
function Invoke-Quiet([scriptblock]$Command) {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Command 2>$null | Out-Null } finally { $ErrorActionPreference = $saved }
  return $LASTEXITCODE
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

Write-Host ''
Write-Host 'AutoSE' -ForegroundColor Blue -NoNewline
Write-Host '  local-first autonomous software engineering'
Write-Host 'free edition: web dashboard + terminal, on models you run yourself' -ForegroundColor DarkGray
Write-Host ''

# Prerequisites: git. Installed with winget when it is missing and winget exists.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Step 'Installing git (needed to download and update AutoSE)'
    Invoke-Quiet { winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements } | Out-Null
    Refresh-Path
  }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die 'git is required but was not found. Install it from https://git-scm.com and run this again.'
  }
}

# uv manages the Python runtime and dependencies. Install it if it is missing.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Step 'Installing uv (Python toolchain manager)'
  try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
  catch { Die 'Could not install uv automatically. Install it from https://docs.astral.sh/uv/ and run this again.' }
  $uvBin = Join-Path $env:USERPROFILE '.local\bin'
  if (Test-Path (Join-Path $uvBin 'uv.exe')) { $env:Path = "$uvBin;$env:Path" }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv was installed but is not on your PATH yet. Open a new terminal and run this again.' }
}
# Launchers call uv by full path, so a shortcut works even before Explorer
# picks up a PATH change.
$Uv = (Get-Command uv).Source

# Fetch
if (Test-Path (Join-Path $InstallDir '.git')) {
  Step "Updating AutoSE in $InstallDir"
  $target = if ($Ref) { $Ref } else { 'HEAD' }
  Invoke-Quiet { git -C $InstallDir remote set-url origin $RepoUrl } | Out-Null
  if ((Invoke-Quiet { git -C $InstallDir fetch -q --depth 1 origin $target }) -ne 0) { Die "Could not fetch $RepoUrl" }
  if ((Invoke-Quiet { git -C $InstallDir reset -q --hard FETCH_HEAD }) -ne 0) { Die "Could not update $InstallDir" }
} elseif (Test-Path $InstallDir) {
  Die "$InstallDir already exists but is not a git checkout. Remove it or set AUTOSE_HOME."
} else {
  Step "Downloading AutoSE into $InstallDir"
  $cloned = if ($Ref) {
    Invoke-Quiet { git clone -q --depth 1 --branch $Ref $RepoUrl $InstallDir }
  } else {
    Invoke-Quiet { git clone -q --depth 1 $RepoUrl $InstallDir }
  }
  if ($cloned -ne 0) { Die "Failed to clone $RepoUrl" }
}

# Set up the environment (uv fetches Python and installs dependencies)
Push-Location $InstallDir
try {
  Step 'Setting up Python with uv (the first time can take a minute)'
  if ((Invoke-Quiet { & $Uv sync -q }) -ne 0) { Die "uv sync failed. Run 'uv sync' in $InstallDir to see the error." }
} finally {
  Pop-Location
}

# Launchers
Step "Installing the autose command into $BinDir"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$cli = Join-Path $BinDir 'autose.cmd'
Set-Content -Path $cli -Encoding ascii -NoNewline -Value "@echo off`r`n`"$Uv`" run -q --project `"$InstallDir`" autose %*`r`n"

# The dashboard launcher asks which project to open (remembering the last
# one), then serves the dashboard for it on a free port and opens the browser.
# Closing its window stops the dashboard.
$dashboard = Join-Path $DataDir 'AutoSE Dashboard.ps1'
$dashboardBody = @'
# AutoSE dashboard launcher, generated by install.ps1. Re-run the installer to regenerate it.
param([string]$Workspace)
$ErrorActionPreference = 'Stop'
$Uv = '__UV__'
$InstallDir = '__INSTALL_DIR__'
$LastFile = Join-Path $env:LOCALAPPDATA 'AutoSE\last-workspace.txt'
$Host.UI.RawUI.WindowTitle = 'AutoSE dashboard'

if (-not $Workspace) {
  $last = if (Test-Path $LastFile) { (Get-Content $LastFile -Raw).Trim() } else { '' }
  Add-Type -AssemblyName System.Windows.Forms
  $owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
  $picker = New-Object System.Windows.Forms.FolderBrowserDialog
  $picker.Description = 'Choose the project folder AutoSE should work in'
  $picker.ShowNewFolderButton = $true
  if ($last -and (Test-Path $last)) { $picker.SelectedPath = $last } else { $picker.SelectedPath = [Environment]::GetFolderPath('MyDocuments') }
  if ($picker.ShowDialog($owner) -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
  $Workspace = $picker.SelectedPath
}
Set-Content -Path $LastFile -Value $Workspace -Encoding utf8

Write-Host ''
Write-Host 'AutoSE' -ForegroundColor Blue -NoNewline
Write-Host '  the dashboard is opening in your browser.'
Write-Host 'Keep this window open while you use it. Close it to stop AutoSE.' -ForegroundColor DarkGray
Write-Host ''
& $Uv run -q --project $InstallDir autose --web --port 0 --workspace $Workspace
if ($LASTEXITCODE -ne 0) {
  Write-Host ''
  Write-Host 'AutoSE stopped with an error (shown above).' -ForegroundColor DarkYellow
  Read-Host 'Press Enter to close'
}
'@
$dashboardBody = $dashboardBody.Replace('__UV__', $Uv.Replace("'", "''")).Replace('__INSTALL_DIR__', $InstallDir.Replace("'", "''"))
Set-Content -Path $dashboard -Value $dashboardBody -Encoding utf8

$dashboardCmd = Join-Path $BinDir 'autose-dashboard.cmd'
Set-Content -Path $dashboardCmd -Encoding ascii -NoNewline -Value "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dashboard`" %*`r`n"

# Shortcuts
if ($env:AUTOSE_NO_SHORTCUT -ne '1') {
  Step 'Adding AutoSE to your desktop and Start menu'
  $icon = Join-Path $DataDir 'autose.ico'
  try { Invoke-WebRequest -UseBasicParsing -Uri 'https://autose.dev/favicon.ico' -OutFile $icon } catch { $icon = $null }
  $shell = New-Object -ComObject WScript.Shell
  $places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
  )
  foreach ($place in $places) {
    if (-not $place -or -not (Test-Path $place)) { continue }
    $link = $shell.CreateShortcut((Join-Path $place 'AutoSE.lnk'))
    $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$dashboard`""
    $link.WorkingDirectory = $HOME
    $link.WindowStyle = 7  # minimized: the browser is the interface
    $link.Description = 'Open the AutoSE dashboard'
    if ($icon -and (Test-Path $icon)) { $link.IconLocation = $icon }
    $link.Save()
  }
}

# Add the launcher directory to the user PATH if needed
$pathNote = $false
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split [IO.Path]::PathSeparator) -notcontains $BinDir) {
  Step "Adding $BinDir to your user PATH"
  $newPath = if ([string]::IsNullOrEmpty($userPath)) { $BinDir } else { $userPath + [IO.Path]::PathSeparator + $BinDir }
  [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
  $env:Path = $env:Path + [IO.Path]::PathSeparator + $BinDir
  $pathNote = $true
}

# Done
Write-Host ''
Write-Host 'AutoSE is installed.' -ForegroundColor Green
Write-Host ''
Write-Host 'Open it any time from the AutoSE icon on your desktop or in the Start menu.'
Write-Host 'In a terminal:  autose --web  (dashboard)   autose  (terminal)   autose --doctor'
if ($pathNote) { Write-Host 'Open a new terminal first so the updated PATH takes effect.' -ForegroundColor DarkGray }
Write-Host ''
Write-Host 'AutoSE runs on a model you host. No model yet? Install Ollama from https://ollama.com'
Write-Host 'and run  ollama pull qwen2.5-coder:14b . AutoSE finds it by itself.'
Write-Host ''

if ($env:AUTOSE_NO_LAUNCH -ne '1') {
  Step 'Opening the dashboard. Pick the project folder AutoSE should work in.'
  Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$dashboard`"") -WindowStyle Minimized
}
