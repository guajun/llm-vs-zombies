<#
.SYNOPSIS
Build a recorder.dll with a hosted AvZ script compiled in, and record its SHA256.

.DESCRIPTION
The default build (tools/build-avz.ps1) produces a recorder.dll with no script
in it. A hosted build compiles one AvZ script source into the same DLL, where
the resident runtime drives it as a coroutine under its own frame ownership
(docs/avz-script-hosting.md). The DLL is a different artifact, so the launcher
refuses it for a run whose manifest was written for the default one: this
script prints the SHA256 that `manifest.implementation.recorder_sha256` has to
carry, writes it next to the DLL, and the run must be created *after* this
build.

Two shipped sources can be hosted:
  logger\avz\hosted\atime_probe.cpp  - three co_await ATime(1, ...) waits; its
                                       state lands in the run directory, which
                                       makes it the live observability probe.
  logger\avz\hosted\jing_dian_12.cpp - the P2 12-cannon script.

.PARAMETER Script
Hosted script source, relative to the checkout or absolute. It must define
`ACoroutine lvz::hosted::Script()`; define `void lvz::hosted::Observe(...)` in
it if the run should also report its state (logger/avz/hosted_script.hpp).

.PARAMETER OutputDirectory
Where recorder.dll is written. Default build\ - the path every other tool uses.
Point it at build\hosted to keep the default DLL in place and copy the hosted
one over build\recorder.dll only for the run that needs it.

.PARAMETER BuildDirectory
CMake build tree. Default: build\hosted-<script name>, so two hosted scripts do
not reconfigure each other's tree. The default build tree build\cmake is never
touched.

.PARAMETER Jobs
Parallel compile jobs (default 4).

.EXAMPLE
PS> .\tools\build-hosted.ps1 -Script logger\avz\hosted\atime_probe.cpp
PS> .\tools\launch-experiment.ps1 -Name hosted-atime-01
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$OutputDirectory,
    [string]$BuildDirectory,
    [int]$Jobs = 4
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolchainBin = Join-Path $projectRoot 'third_party/llvm-mingw-20260908-ucrt-x86_64/bin'
$compilerC = Join-Path $toolchainBin 'i686-w64-mingw32-clang.exe'
$compilerCpp = Join-Path $toolchainBin 'i686-w64-mingw32-clang++.exe'
if (!(Test-Path -LiteralPath $compilerCpp)) {
    throw 'Toolchain missing. Run tools/prepare-checkout.ps1 (or tools/bootstrap.ps1) first.'
}

$scriptPath = (Resolve-Path -LiteralPath $Script -ErrorAction SilentlyContinue).Path
if (!$scriptPath) { throw "Hosted script not found: $Script" }
if (!(Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Hosted script is not a file: $scriptPath" }

if (!$OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'build' }
$outputPath = [IO.Path]::GetFullPath($OutputDirectory)
if (!$BuildDirectory) {
    $BuildDirectory = Join-Path $projectRoot ('build/hosted-' + [IO.Path]::GetFileNameWithoutExtension($scriptPath))
}
$buildPath = [IO.Path]::GetFullPath($BuildDirectory)
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

Write-Host "hosted script : $scriptPath"
Write-Host "build tree    : $buildPath"
Write-Host "output        : $outputPath"
& cmake -S $projectRoot -B $buildPath -G Ninja '-DCMAKE_BUILD_TYPE=Release' '-DCMAKE_SYSTEM_NAME=Windows' `
    "-DCMAKE_C_COMPILER=$compilerC" "-DCMAKE_CXX_COMPILER=$compilerCpp" `
    "-DLVZ_AVZ_HOSTED_SCRIPT=$scriptPath" "-DLVZ_RECORDER_OUTPUT_DIR=$outputPath"
if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed' }
& cmake --build $buildPath --target recorder --parallel $Jobs
if ($LASTEXITCODE -ne 0) { throw 'Hosted native build failed' }

$dll = Join-Path $outputPath 'recorder.dll'
if (!(Test-Path -LiteralPath $dll -PathType Leaf)) { throw "The build did not produce $dll" }
$dllHash = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()
$scriptHash = (Get-FileHash -LiteralPath $scriptPath -Algorithm SHA256).Hash.ToLowerInvariant()

# Evidence next to the DLL: the launcher checks the run manifest against the
# DLL's SHA256, so the build has to state it in a form an operator can paste
# or diff. `recorder.sha256` uses the sha256sum layout.
Set-Content -LiteralPath (Join-Path $outputPath 'recorder.sha256') -Value "$dllHash  recorder.dll" -Encoding ascii
$record = [ordered]@{
    schema               = 'lvz.hosted-build.v1'
    built_at             = (Get-Date).ToUniversalTime().ToString('o')
    hosted_script        = $scriptPath
    hosted_script_sha256 = $scriptHash
    recorder             = $dll
    recorder_sha256      = $dllHash
    build_directory      = $buildPath
    cmake_generator      = 'Ninja'
    cmake_build_type     = 'Release'
}
Set-Content -LiteralPath (Join-Path $outputPath 'hosted-build.json') -Value ($record | ConvertTo-Json -Depth 4) -Encoding utf8

Write-Host ''
Write-Host "recorder.dll    : $dll"
Write-Host "recorder_sha256 : $dllHash"
Write-Host "hosted script   : $scriptHash  $scriptPath"
Write-Host "evidence        : $(Join-Path $outputPath 'recorder.sha256'), $(Join-Path $outputPath 'hosted-build.json')"
$defaultDll = Join-Path $projectRoot 'build/recorder.dll'
if ($outputPath.TrimEnd('\') -ne (Split-Path -Parent $defaultDll).TrimEnd('\')) {
    Write-Host ''
    Write-Host "This DLL is not the one the launcher reads. For a hosted run:"
    Write-Host "  Copy-Item '$dll' '$defaultDll' -Force"
}
Write-Host ''
Write-Host 'Create the run after this build: the run manifest binds recorder_sha256,'
Write-Host 'and the launcher refuses a DLL that changed afterwards.'
