param([switch]$PolicyOnly, [Parameter(ValueFromRemainingArguments=$true)][string[]]$HookArguments)

# Codex Windows launcher: use the script's own location, never the shell cwd.
$ErrorActionPreference = 'Continue'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $OutputEncoding
[Console]::InputEncoding = $OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
if ($PolicyOnly) {
    if ($env:CLAUDE_PLUGIN_OPTION_CODING_POLICY -notmatch '^(off|false|0|no|disabled)$') {
        Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'policy-context.json')
    }
    exit 0
}
$payload = [Console]::In.ReadToEnd()
$candidates = @()
if ($env:AGY_BRIDGE_PYTHON) { $candidates += ,@($env:AGY_BRIDGE_PYTHON) }
foreach ($name in @('python3.exe', 'py.exe', 'python.exe')) {
    $found = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
    if ($found) {
        if ($name -eq 'py.exe') { $candidates += ,@($found.Source, '-3') }
        else { $candidates += ,@($found.Source) }
    }
}
# Native installs may not be on the desktop host's inherited PATH yet.
if ($env:LOCALAPPDATA) {
    $installPatterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python*/python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python/pythoncore-*/python.exe')
    )
    Get-ChildItem -Path $installPatterns -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $candidates += ,@($_.FullName) }
}
foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $prefix = @($candidate | Select-Object -Skip 1)
    try {
        & $exe @prefix -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { continue }
        $payload | & $exe @prefix (Join-Path $PSScriptRoot 'agy_opportunity_reminder.py') @HookArguments
        exit $LASTEXITCODE
    } catch { continue }
}
$base = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Polyphony' } else { [IO.Path]::GetTempPath() }
$stamp = Join-Path $base 'hook-python-missing.warned'
try {
    if (!(Test-Path -LiteralPath $stamp) -or (Get-Item -LiteralPath $stamp).LastWriteTime -lt (Get-Date).AddDays(-1)) {
        New-Item -ItemType Directory -Path $base -Force -ErrorAction Stop | Out-Null
        Set-Content -LiteralPath $stamp -Value 'Python missing' -Encoding UTF8 -ErrorAction Stop
        [Console]::Error.WriteLine('[Polyphony] Hook Python unavailable; routing enforcement is inactive. Install Python 3.9+ or set AGY_BRIDGE_PYTHON to its executable, then restart the host.')
    }
} catch { }
exit 0
