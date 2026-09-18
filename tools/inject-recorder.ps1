$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dll = Join-Path $projectRoot 'build/recorder.dll'
$cfg = Join-Path $projectRoot 'build/recorder.cfg'
if (!(Test-Path -LiteralPath $dll) -or !(Test-Path -LiteralPath $cfg)) { throw 'Build the recorder and create a new run first.' }
$runDirectory = Get-Content -LiteralPath $cfg -Encoding utf8 | Select-Object -First 1
$manifest = Get-Content -LiteralPath (Join-Path $runDirectory 'manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
$dllHash = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifest.implementation.recorder_sha256 -ne $dllHash) { throw 'Recorder changed since this run was created. Create a new run after building.' }
Write-Output 'Select the verified PvZ 1.0.0.1051 game window in the AvZ injector. Press 7 in-game to stop capture.'
& (Join-Path $projectRoot 'avz/runtime/bin/injector.exe') $dll
if ($LASTEXITCODE -ne 0) { throw 'Injector returned an error' }
