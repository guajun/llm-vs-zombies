param([int]$Jobs = 2, [switch]$SkipBuild)
# Local replacement for the removed GitHub Actions workflow. It performs the
# same distributable checks and never touches the game, user profiles or live
# replay evidence: pinned AvZ identity, native build, Python tests, native
# tests and the pre-main isolation fixture.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$steps = [System.Collections.Generic.List[object]]::new()

function Invoke-Step([string]$Name, [scriptblock]$Action) {
    $started = Get-Date
    Write-Output "== $Name"
    & $Action
    $steps.Add([pscustomobject]@{ step = $Name; seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 3) })
}

Invoke-Step 'pinned AvZ identity' {
    $lock = Get-Content dependencies.lock.json -Raw | ConvertFrom-Json
    $actual = (& git -C avz/framework rev-parse HEAD).Trim()
    if ($actual -ne $lock.avz_commit) { throw "AvZ submodule differs from lock file: $actual" }
}

if (-not $SkipBuild) {
    Invoke-Step 'dependencies' { & ./tools/bootstrap.ps1 }
    Invoke-Step 'native build' {
        & ./tools/build-avz.ps1 -Jobs $Jobs
        if ($LASTEXITCODE -ne 0) { throw 'Native build failed.' }
    }
    Invoke-Step 'launcher build' {
        & ./launcher/build.ps1
        if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed.' }
    }
}

Invoke-Step 'python tests' {
    $env:PYTHONPATH = 'src'
    & python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
}

Invoke-Step 'native tests' {
    & ctest --test-dir build/cmake --output-on-failure
    if ($LASTEXITCODE -ne 0) { throw 'Native tests failed.' }
}

Invoke-Step 'isolation fixture' {
    $fixtureRoot = Join-Path $projectRoot 'work/ci-isolation-fixture'
    New-Item -ItemType Directory -Force $fixtureRoot, (Join-Path $fixtureRoot 'appdata'), (Join-Path $fixtureRoot 'windows') | Out-Null
    $compiler = Join-Path $projectRoot 'third_party/llvm-mingw-20260908-ucrt-x86_64/bin/i686-w64-mingw32-clang++.exe'
    $fixtureExe = Join-Path $projectRoot 'build/launcher/isolation-fixture.exe'
    & $compiler -std=c++20 -O2 -static launcher/isolation_fixture.cpp -luser32 -ladvapi32 -o $fixtureExe
    if ($LASTEXITCODE -ne 0) { throw 'Fixture build failed.' }
    & ./build/launcher/lvz-launcher.exe launch $fixtureExe (Join-Path $projectRoot 'build/launcher/lvz-bootstrap.dll') $fixtureRoot $fixtureRoot (Join-Path $fixtureRoot 'receipt.json')
    if ($LASTEXITCODE -ne 0) { throw 'Pre-main isolation injection failed.' }
    $resultPath = Join-Path $fixtureRoot 'fixture-result.txt'
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if ((Test-Path -LiteralPath $resultPath) -and (Get-Content -LiteralPath $resultPath -Raw) -match '(?m)^passed=1\r?$') { break }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Isolation fixture did not pass; inspect work/ci-isolation-fixture.' }
        Start-Sleep -Milliseconds 50
    } while ($true)
}

$steps | Format-Table -AutoSize | Out-String | Write-Output
Write-Output 'Local CI passed. No game process, user profile or live experiment was touched.'
