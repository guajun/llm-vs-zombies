# The preparation steps (submodule content, AvZ injector, pinned toolchain, work/)
# now live in tools/prepare-checkout.ps1 so a fresh checkout has exactly one
# preparation path. This wrapper keeps the old entry point working: it runs the
# same steps and skips the build self-check that ci.ps1 and build-avz.ps1 do
# separately.
#
# Extra arguments are forwarded, so tools/bootstrap.ps1 -VerifyOnly,
# -ToolchainSource <zip>, -AvzSource <repo> and -Offline all work through here.
param([Parameter(ValueFromRemainingArguments = $true)]$Passthrough)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'prepare-checkout.ps1') -SkipSelfCheck @Passthrough
$code = $LASTEXITCODE
if ($code -ne 0) { throw "prepare-checkout.ps1 failed with exit code $code" }
Write-Output 'Dependencies ready. Game files are local and are not downloaded by this script.'
