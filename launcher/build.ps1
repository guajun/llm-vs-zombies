param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (!$OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'build/launcher' }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$compiler = Join-Path $projectRoot 'third_party/llvm-mingw-20260908-ucrt-x86_64/bin/i686-w64-mingw32-clang++.exe'
if (!(Test-Path -LiteralPath $compiler)) { throw 'Run tools/bootstrap.ps1 first.' }
& $compiler -std=c++20 -O2 -static -municode (Join-Path $PSScriptRoot 'native_launcher.cpp') -o (Join-Path $OutputDirectory 'lvz-launcher.exe')
if ($LASTEXITCODE -ne 0) { throw 'Native launcher build failed.' }
& $compiler -std=c++20 -O2 -shared -static (Join-Path $PSScriptRoot 'bootstrap.cpp') -luser32 -ladvapi32 -lshell32 -o (Join-Path $OutputDirectory 'lvz-bootstrap.dll')
if ($LASTEXITCODE -ne 0) { throw 'Isolation bootstrap build failed.' }
Write-Output $OutputDirectory
