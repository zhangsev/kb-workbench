# kb-workbench launcher (Windows). Usage: .\start.ps1 [setup|index|serve|status|stop|doctor] [args]
# Checks dependencies and prints the exact fix command. It never downloads anything
# itself: only `.\start.ps1 setup` installs. All messages go to stderr because in
# `serve` mode stdout carries the MCP protocol.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Cmd = if ($args.Count -gt 0) { $args[0] } else { 'serve' }
$Rest = @($args | Select-Object -Skip 1)
function Say($m) { [Console]::Error.WriteLine("[kbw] $m") }

$VPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VPy)) {
    if ($Cmd -ne 'setup') { Say 'not set up yet. Fix: .\start.ps1 setup   (offline: .\start.ps1 setup --offline)'; exit 1 }
    $Py = $null
    foreach ($c in @(@('py', '-3.11'), @('py', '-3'), @('python'))) {
        if (Get-Command $c[0] -ErrorAction SilentlyContinue) {
            $extra = @(); if ($c.Count -gt 1) { $extra = $c[1..($c.Count - 1)] }
            # Windows PowerShell treats redirected native stderr as an error.
            # A missing optional runtime must not prevent trying the next one.
            $ErrorActionPreference = 'Continue'
            & $c[0] @extra -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE -eq 0) { $Py = $c; break }
        }
    }
    if (-not $Py) { Say 'Python 3.11+ not found. Fix: winget install Python.Python.3.11'; exit 1 }
    Say "creating .venv with $($Py -join ' ')"
    $extra = @(); if ($Py.Count -gt 1) { $extra = $Py[1..($Py.Count - 1)] }
    & $Py[0] @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { Say 'could not create .venv'; exit 1 }
}
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$PSScriptRoot;$env:PYTHONPATH" } else { $PSScriptRoot }
$env:PYTHONUTF8 = '1'

if ($Cmd -ne 'setup') {
    & $VPy -c 'import mcp, yaml' 2>$null
    if ($LASTEXITCODE -ne 0) { Say 'Python packages missing. Fix: .\start.ps1 setup'; exit 1 }
    if (-not (Test-Path -LiteralPath 'kbw.yaml') -and -not $env:KBW_CONFIG) {
        Say 'kbw.yaml missing. Fix: Copy-Item kbw.example.yaml kbw.yaml and edit it, or .\start.ps1 setup --examples'; exit 1
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Say 'warning: node not found; knowledge and code downstreams will be unavailable. Fix: winget install OpenJS.NodeJS.LTS' }
    if (-not (Test-Path -LiteralPath 'vendor\bin\rg.exe') -and -not (Get-Command rg -ErrorAction SilentlyContinue)) { Say 'warning: ripgrep not found; exact search unavailable. Fix: .\start.ps1 setup' }
}
& $VPy -m kbw $Cmd @Rest
exit $LASTEXITCODE
