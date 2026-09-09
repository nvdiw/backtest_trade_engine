param(
    [ValidateRange(1, 1000000)][int]$Cycles = 200,
    [ValidateRange(1, 256)][int]$Workers = 8,
    [string]$Resume = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonExecutable = (Get-Command python -ErrorAction Stop).Source
$env:PYTHONUNBUFFERED = '1'
$env:MPLBACKEND = 'Agg'
# Let the selected strategy own its data and let optimize detect candle spacing.
Remove-Item Env:BTE_DATA_FILE -ErrorAction SilentlyContinue
Remove-Item Env:BTE_TIMEFRAME -ErrorAction SilentlyContinue

if ($Resume) {
    $optimizerArguments = @('optimize.py', '--resume', $Resume)
} else {
    $campaignFolder = Join-Path $PSScriptRoot ('outputs/pulse/optimize/pulse_quality_200_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $optimizerArguments = @(
        'optimize.py', '--strategy', 'pulse', '--auto', '--profile', 'full',
        '--param-grid', 'param_grids/pulse_quality_1m.json',
        '--output-dir', $campaignFolder,
        '--base-source', 'config', '--base-params', (Join-Path $campaignFolder 'best_params.json'),
        '--auto-tests', '128', '--auto-validation-top', '24', '--auto-stress-top', '12',
        '--auto-walk-forward-top', '8', '--auto-walk-forward-folds', '3', '--auto-final-top', '4',
        '--auto-hall-size', '100', '--auto-advanced-min-candidates', '32', '--auto-halving-rungs', '0',
        '--auto-surrogate-min-samples', '64', '--auto-surrogate-max-samples', '10000',
        '--auto-learning-target', 'profit-evidence', '--min-trades', '30', '--max-drawdown', '25',
        '--snapshot-cycles', '50', '--snapshot-top', '100', '--log-every', '10'
    )
    Write-Host "Campaign folder: $campaignFolder" -ForegroundColor Cyan
}
$optimizerArguments += @('--auto-cycles', [string]$Cycles, '--workers', [string]$Workers, '--performance', 'normal')
if ($DryRun) { $optimizerArguments += '--dry-run' }
Write-Host "Pulse | $Cycles additional cycles | $Workers workers | timeframe detected from strategy data" -ForegroundColor Green
Write-Host 'Ctrl+C stops safely. Use -Resume FOLDER -Cycles REMAINING to continue.'
# Run directly in this console: no hidden process, job or detached Python worker launcher.
& $pythonExecutable @optimizerArguments
if ($LASTEXITCODE -ne 0) { throw "Optimizer exited with code $LASTEXITCODE" }
Write-Host 'Optimizer finished. This terminal remains open when launched with -NoExit.' -ForegroundColor Green
