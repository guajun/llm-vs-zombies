param([Parameter(Mandatory=$true)][string]$Name, [switch]$Stop, [switch]$NoInitialize, [double]$Timeout=90, [uint32]$Seed=0,
      [string]$Scenario='liangyi')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$experimentRun = Join-Path $projectRoot "experiments/runs/$Name"
if ($Name -notmatch '^[A-Za-z0-9_-]+$') { throw 'Name must contain only letters, numbers, underscore or dash.' }
# The scenario names a launcher registry entry (src/llm_vs_zombies/launcher.py).
# Default liangyi keeps the historic command line; an unknown name is rejected by
# the launcher before any process starts.
$scenarioConfig = Join-Path $projectRoot "experiments/configs/$Scenario.json"
if ($Stop) {
    & python -m llm_vs_zombies.launcher stop --run $experimentRun
} else {
    if (!(Test-Path -LiteralPath (Join-Path $experimentRun 'manifest.json'))) {
        if (!(Test-Path -LiteralPath $scenarioConfig -PathType Leaf)) { throw "scenario config not found: $scenarioConfig" }
        & python -m llm_vs_zombies new-run --name $Name --config $scenarioConfig
        if ($LASTEXITCODE -ne 0) { throw 'Run creation failed.' }
    }
    $launcherArgs = @('-m','llm_vs_zombies.launcher','start','--run',$experimentRun,'--timeout',"$Timeout",'--seed',"$Seed",'--scenario',$Scenario)
    if ($NoInitialize) { $launcherArgs += '--no-initialize' }
    & python @launcherArgs
}
if ($LASTEXITCODE -ne 0) { throw 'Experiment launcher failed; inspect launcher.json and sandbox/bootstrap.log.' }
