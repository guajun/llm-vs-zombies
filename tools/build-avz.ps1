param([int]$Jobs = 4)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolchainBin = Join-Path $projectRoot 'third_party/llvm-mingw-20260908-ucrt-x86_64/bin'
$compilerC = Join-Path $toolchainBin 'i686-w64-mingw32-clang.exe'
$compilerCpp = Join-Path $toolchainBin 'i686-w64-mingw32-clang++.exe'
if (!(Test-Path -LiteralPath $compilerCpp)) { throw 'Run tools/bootstrap.ps1 first.' }
$buildDir = Join-Path $projectRoot 'build/cmake'
& cmake -S $projectRoot -B $buildDir -G Ninja '-DCMAKE_BUILD_TYPE=Release' '-DCMAKE_SYSTEM_NAME=Windows' "-DCMAKE_C_COMPILER=$compilerC" "-DCMAKE_CXX_COMPILER=$compilerCpp"
if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed' }
& cmake --build $buildDir --parallel $Jobs
if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
