# AutoSE installer (Windows / PowerShell)
# Clones AutoSE and sets up the `autose` CLI with uv.
#
#   irm https://autose.dev/install.ps1 | iex
#
# Honors these environment variables:
#   AUTOSE_HOME     where to clone AutoSE        (default: $HOME\.autose-cli)
#   AUTOSE_BIN_DIR  where to place the launcher  (default: $env:LOCALAPPDATA\AutoSE\bin)
#   AUTOSE_REF      git ref to check out         (default: the repo default branch)

$ErrorActionPreference = 'Stop'

$RepoUrl    = 'https://github.com/AutoSE-Labs/autose.git'
$InstallDir = if ($env:AUTOSE_HOME)    { $env:AUTOSE_HOME }    else { Join-Path $HOME '.autose-cli' }
$BinDir     = if ($env:AUTOSE_BIN_DIR) { $env:AUTOSE_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'AutoSE\bin' }
$Ref        = $env:AUTOSE_REF

function Step($m) {
  Write-Host '> ' -ForegroundColor DarkYellow -NoNewline
  Write-Host $m
}
function Die($m) {
  Write-Host 'error: ' -ForegroundColor DarkYellow -NoNewline
  Write-Host $m
  exit 1
}

Write-Host ''
Write-Host 'AutoSE' -ForegroundColor Blue -NoNewline
Write-Host '  sovereign autonomous software engineering'
Write-Host 'local-first, org-aware, zero cloud' -ForegroundColor DarkGray
Write-Host ''

# Prerequisites
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die 'git is required but was not found. Install it from https://git-scm.com' }

# uv manages the Python runtime and dependencies. Install it if it is missing.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Step 'Installing uv (Python toolchain manager)'
  try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
  catch { Die 'Could not install uv automatically. Install it from https://docs.astral.sh/uv/ and re-run.' }
  $uvBin = Join-Path $env:USERPROFILE '.local\bin'
  if (Test-Path (Join-Path $uvBin 'uv.exe')) { $env:Path = "$uvBin;$env:Path" }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv was installed but is not on your PATH yet. Open a new terminal and re-run this installer.' }
}

# Fetch
if (Test-Path (Join-Path $InstallDir '.git')) {
  Step "Updating AutoSE in $InstallDir"
  git -C $InstallDir fetch --depth 1 origin | Out-Null
  if ($Ref) {
    git -C $InstallDir checkout -q $Ref
    try { git -C $InstallDir reset --hard -q "origin/$Ref" } catch { git -C $InstallDir pull -q }
  } else {
    try { git -C $InstallDir reset --hard -q '@{u}' } catch { git -C $InstallDir pull -q }
  }
} elseif (Test-Path $InstallDir) {
  Die "$InstallDir already exists but is not a git checkout. Remove it or set AUTOSE_HOME."
} else {
  Step "Cloning AutoSE into $InstallDir"
  if ($Ref) {
    git clone --depth 1 --branch $Ref $RepoUrl $InstallDir 2>$null
  } else {
    git clone --depth 1 $RepoUrl $InstallDir 2>$null
  }
  if ($LASTEXITCODE -ne 0) { Die "Failed to clone $RepoUrl" }
}

# Set up the environment (uv fetches Python and installs dependencies)
Push-Location $InstallDir
try {
  Step 'Setting up the Python environment with uv (this can take a minute)'
  uv sync | Out-Null
  if ($LASTEXITCODE -ne 0) { Die "uv sync failed. Run 'uv sync' in $InstallDir to see the error." }
} finally {
  Pop-Location
}

# Link the launcher
Step "Linking the autose launcher into $BinDir"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$launcher = Join-Path $BinDir 'autose.cmd'
$launcherBody = "@echo off`r`nuv run --project `"$InstallDir`" autose %*`r`n"
Set-Content -Path $launcher -Value $launcherBody -Encoding ascii -NoNewline

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
if ($pathNote) {
  Write-Host 'Open a new terminal so the updated PATH takes effect.' -ForegroundColor DarkYellow
  Write-Host ''
}
Write-Host 'Next steps:'
Write-Host '  1. Point AutoSE at your model. It works with any OpenAI-compatible endpoint'
Write-Host '     (Ollama, LM Studio, vLLM, and so on). Set base_url and model in'
Write-Host '     %APPDATA%\AutoSE\config.yaml, or copy profiles\config.yaml from the repo.'
Write-Host '  2. Run  autose  in your project to start an interactive session.'
Write-Host ''
