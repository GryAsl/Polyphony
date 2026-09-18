$ErrorActionPreference = 'Continue'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$onWindows = [System.IO.Path]::DirectorySeparatorChar -eq '\'

$srcDir = Join-Path (Split-Path -Parent $PSScriptRoot) (Join-Path 'agy' 'rules')
if (-not (Test-Path -LiteralPath $srcDir)) { exit 0 }

$geminiRoot = if ($env:GEMINI_HOME) { $env:GEMINI_HOME } else { Join-Path $HOME '.gemini' }
if (-not $onWindows -and $geminiRoot) {
    $geminiRoot = $geminiRoot -replace '\\', '/'
}
if (-not (Test-Path -LiteralPath (Join-Path $geminiRoot 'config'))) { exit 0 }

$pluginDir = Join-Path $geminiRoot (Join-Path (Join-Path 'config' 'plugins') 'polyphony')
$destDir = Join-Path $pluginDir 'rules'
try {
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir -ErrorAction Stop | Out-Null
    }
} catch {
    [Console]::Error.WriteLine("[polyphony] could not create $destDir - agy engineering rules not installed")
    exit 0
}

$manifest = "{`n  `"name`": `"polyphony`",`n  `"description`": `"Polyphony engineering rules for agy workers.`"`n}`n"
$manifestPath = Join-Path $pluginDir 'plugin.json'
$current = $null
if (Test-Path -LiteralPath $manifestPath) {
    try { $current = [System.IO.File]::ReadAllText($manifestPath) } catch { $current = $null }
}
if ($current -ne $manifest) {
    try {
        [System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding($false)))
        [Console]::Error.WriteLine("[polyphony] wrote $manifestPath - agy does not read rules/ without it")
    } catch {
        [Console]::Error.WriteLine("[polyphony] could not write $manifestPath - agy engineering rules not installed")
        exit 0
    }
}

$installed = 0
foreach ($src in Get-ChildItem -LiteralPath $srcDir -Filter *.md -File -ErrorAction SilentlyContinue) {
    $dest = Join-Path $destDir $src.Name
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
