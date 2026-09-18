$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    & python -m llm_vs_zombies @args
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $priorPythonPath
}
