param([Parameter(Mandatory=$true)][string]$Name, [switch]$Stop, [switch]$NoInitialize, [double]$Timeout=90, [uint32]$Seed=0)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$experimentRun = Join-Path $projectRoot "experiments/runs/$Name"
if ($Name -notmatch '^[A-Za-z0-9_-]+$') { throw 'Name must contain only letters, numbers, underscore or dash.' }
if ($Stop) {
    & python -m llm_vs_zombies.launcher stop --run $experimentRun
} else {
    if (!(Test-Path -LiteralPath (Join-Path $experimentRun 'manifest.json'))) {
        & python -m llm_vs_zombies new-run --name $Name
        if ($LASTEXITCODE -ne 0) { throw 'Run creation failed.' }
    }
    $launcherArgs = @('-m','llm_vs_zombies.launcher','start','--run',$experimentRun,'--timeout',"$Timeout",'--seed',"$Seed")
    if ($NoInitialize) { $launcherArgs += '--no-initialize' }
    & python @launcherArgs
}
if ($LASTEXITCODE -ne 0) { throw 'Experiment launcher failed; inspect launcher.json and sandbox/bootstrap.log.' }
