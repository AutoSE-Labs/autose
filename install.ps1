# AutoSE installer (Windows / PowerShell)
# Clones the AutoSE repo, builds it, and links the `autose` CLI onto your PATH.
#
#   irm https://autose.dev/install.ps1 | iex
#
# Honors these environment variables:
#   AUTOSE_HOME     where to clone AutoSE        (default: $HOME\.autose-cli)
#   AUTOSE_BIN_DIR  where to place the launcher  (default: $env:LOCALAPPDATA\AutoSE\bin)
#   AUTOSE_REF      git ref to check out         (default: poc)

$ErrorActionPreference = 'Stop'

$RepoUrl    = 'https://github.com/AutoSE-Labs/autose.git'
$InstallDir = if ($env:AUTOSE_HOME)    { $env:AUTOSE_HOME }    else { Join-Path $HOME '.autose-cli' }
$BinDir     = if ($env:AUTOSE_BIN_DIR) { $env:AUTOSE_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'AutoSE\bin' }
$Ref        = if ($env:AUTOSE_REF)     { $env:AUTOSE_REF }     else { 'poc' }

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
if (-not (Get-Command git  -ErrorAction SilentlyContinue)) { Die 'git is required but was not found. Install it from https://git-scm.com' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Die 'Node.js 20 or newer is required but was not found. Install it from https://nodejs.org' }
if (-not (Get-Command npm  -ErrorAction SilentlyContinue)) { Die 'npm is required but was not found. It ships with Node.js (https://nodejs.org)' }

$nodeMajor = [int](node -p 'process.versions.node.split(".")[0]')
if ($nodeMajor -lt 20) { Die "Node.js 20 or newer is required (found $(node -v)). Upgrade at https://nodejs.org" }

# Fetch
if (Test-Path (Join-Path $InstallDir '.git')) {
  Step "Updating AutoSE in $InstallDir"
  git -C $InstallDir fetch --depth 1 origin $Ref | Out-Null
  git -C $InstallDir checkout -q $Ref
  try { git -C $InstallDir reset --hard -q "origin/$Ref" } catch { git -C $InstallDir pull -q }
} elseif (Test-Path $InstallDir) {
  Die "$InstallDir already exists but is not a git checkout. Remove it or set AUTOSE_HOME."
} else {
  Step "Cloning AutoSE into $InstallDir"
  git clone --depth 1 --branch $Ref $RepoUrl $InstallDir 2>$null
  if ($LASTEXITCODE -ne 0) { git clone --depth 1 $RepoUrl $InstallDir }
  if ($LASTEXITCODE -ne 0) { Die "Failed to clone $RepoUrl" }
}

# Build
Push-Location $InstallDir
try {
  Step 'Installing dependencies (this can take a minute)'
  npm install --no-audit --no-fund | Out-Null
  if ($LASTEXITCODE -ne 0) { Die "npm install failed. Run it manually in $InstallDir to see the error." }

  Step 'Building AutoSE'
  npm run build | Out-Null
  if ($LASTEXITCODE -ne 0) { Die "Build failed. Run 'npm run build' in $InstallDir to see the error." }
} finally {
  Pop-Location
}

# Link the launcher
Step "Linking the autose launcher into $BinDir"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$launcher = Join-Path $BinDir 'autose.cmd'
$target   = Join-Path $InstallDir 'bin\autose.cmd'
$launcherBody = "@echo off`r`ncall `"$target`" %*`r`n"
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
Write-Host '  1. Install and start Ollama    https://ollama.com'
Write-Host '     ollama pull llama3.2, then ollama pull qwen2.5-coder:14b' -ForegroundColor DarkGray
Write-Host '  2. Initialize a workspace      autose init --workspace .'
Write-Host '  3. Start engineering           autose chat --workspace .'
Write-Host ''
