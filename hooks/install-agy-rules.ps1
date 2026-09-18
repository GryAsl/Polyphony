# Windows counterpart of install-agy-rules.sh — see that file for why rules are
# installed as files rather than carried in the delegation prompt.
#
# Uses the script's own location, never the shell cwd. Never fails the session:
# every failure path warns and exits 0.

$ErrorActionPreference = 'Continue'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$srcDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'agy/rules'
if (-not (Test-Path -LiteralPath $srcDir)) { exit 0 }

$geminiRoot = if ($env:GEMINI_HOME) { $env:GEMINI_HOME } else { Join-Path $HOME '.gemini' }

# No agy config tree means agy was never run here; don't litter the disk.
if (-not (Test-Path -LiteralPath (Join-Path $geminiRoot 'config'))) { exit 0 }

$destDir = Join-Path $geminiRoot 'config/plugins/polyphony/rules'
try {
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir -ErrorAction Stop | Out-Null
    }
} catch {
    [Console]::Error.WriteLine("[polyphony] could not create $destDir - agy engineering rules not installed")
    exit 0
}

$installed = 0
foreach ($src in Get-ChildItem -LiteralPath $srcDir -Filter *.md -File -ErrorAction SilentlyContinue) {
    $dest = Join-Path $destDir $src.Name
    # Copy only on first install or a real content change, so an unchanged session
    # start does no disk writes and a user's own edits are not rewritten every time.
    if (Test-Path -LiteralPath $dest) {
        $a = (Get-FileHash -LiteralPath $src.FullName -Algorithm SHA256).Hash
        $b = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
        if ($a -eq $b) { continue }
    }
    try {
        Copy-Item -LiteralPath $src.FullName -Destination $dest -Force -ErrorAction Stop
        $installed++
    } catch {
        [Console]::Error.WriteLine("[polyphony] could not write $dest - agy engineering rules not installed")
        exit 0
    }
}

if ($installed -gt 0) {
    [Console]::Error.WriteLine("[polyphony] installed $installed agy engineering rule(s) into $destDir")
}

exit 0
