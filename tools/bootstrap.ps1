$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& git -C $projectRoot submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw 'AvZ checkout failed' }
$referenceSave = Join-Path $projectRoot 'experiments/scenarios/liangyi/game1_13.dat'
if (!(Test-Path -LiteralPath $referenceSave)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'avz/framework/tutorial/scripts/liang_yi/game1_13.dat') -Destination $referenceSave
}
$runtime = Join-Path $projectRoot 'avz/runtime'
if (!(Test-Path -LiteralPath (Join-Path $runtime 'bin/injector.exe'))) {
    Expand-Archive -LiteralPath (Join-Path $projectRoot 'avz/framework/release/env2/2.9.2_2026_06_26.zip') -DestinationPath $runtime
}
$compiler = Join-Path $projectRoot 'third_party/llvm-mingw-20260908-ucrt-x86_64/bin/i686-w64-mingw32-clang++.exe'
if (!(Test-Path -LiteralPath $compiler)) {
    New-Item -ItemType Directory -Path (Join-Path $projectRoot 'work'),(Join-Path $projectRoot 'third_party') -Force | Out-Null
    $archive = Join-Path $projectRoot 'work/llvm-mingw-20260908.zip'
    if (!(Test-Path -LiteralPath $archive)) {
        Invoke-WebRequest -Uri 'https://github.com/mstorsjo/llvm-mingw/releases/download/20260908/llvm-mingw-20260908-ucrt-x86_64.zip' -OutFile $archive
    }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne '1bcf74d06b724aeecaa6412ca85f5b26fb1da770e7cdcefa9263c9c5c3ad34b6') { throw 'Compiler archive hash mismatch' }
    Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $projectRoot 'third_party')
}
Write-Output 'Dependencies ready. Game files are local and are not downloaded by this script.'
