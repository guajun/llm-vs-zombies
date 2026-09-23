<#
.SYNOPSIS
Prepare one standalone checkout so builds, tests and evaluation plans work inside
it, then prove it with an in-checkout self-check.

.DESCRIPTION
`docs/并行实验约定.md` §2.1 requires one checkout per parallel world. A fresh
clone is missing everything that is deliberately git-ignored, so the four gaps
below have to be filled *inside that checkout* (never by reusing another
checkout's build/, third_party/ or avz/runtime/):

1. `work/`          - `evaluation plan` writes `work/<name>.json` and fails with
                      FileNotFoundError when the directory is absent.
2. `avz/framework`  - submodule content at the commit pinned by
                      dependencies.lock.json (`git submodule update --init --depth 1`).
3. `avz/runtime`    - the AvZ injector unpacked from the pinned env2 archive.
4. `third_party/llvm-mingw-20260908-ucrt-x86_64`
                    - the SHA256-pinned compiler archive, copied from a sibling
                      checkout that already has it so two parallel worlds do not
                      download 190 MB each.

Before building, the script verifies the AvZ sources that
`runtime/avz_overlay.cmake` pins. A missing submodule makes that overlay report
`Pinned AvZ hook/script source changed` even though nothing was changed (see
§2.5 of the conventions), so the report keeps the normalized SHA256 of every
pinned file next to its expected value and fails with the real reason first.

Unless -SkipSelfCheck is used it then runs what the evaluation runner's
`build_and_tests` step runs (native build, launcher, Python tests, ctest) plus a
plan-write smoke test. None of these steps start a game, so the self-check also
works in a game-less CI checkout.

.PARAMETER VerifyOnly
Verify only: no download, no extraction, no build, no `git submodule update`.
The only write is a self-cleaning probe file inside an existing `work/` that
proves the plan directory is writable. Exits non-zero when something is missing
and prints the exact command or flag that fixes it.

.PARAMETER SkipSelfCheck
Prepare the checkout but do not build or test it.

.PARAMETER Offline
Never fetch from the network: the toolchain archive and the AvZ submodule must
come from local sources (-ToolchainSource / -AvzSource or a sibling checkout).

.PARAMETER ToolchainSource
Path to `llvm-mingw-20260908.zip` (a directory containing it is accepted).

.PARAMETER AvzSource
Local AvZ git repository used instead of the network, e.g.
`-AvzSource F:\llm-vs-zombies\avz\framework`. The pinned commit is still verified.

.PARAMETER Jobs
Parallelism for the native build (default 2, the same value `build_and_tests` uses).

.EXAMPLE
PS> git clone --depth 1 https://github.com/guajun/llm-vs-zombies.git work\lvz-w1
PS> cd work\lvz-w1
PS> ./tools/prepare-checkout.ps1

.EXAMPLE
PS> ./tools/prepare-checkout.ps1 -VerifyOnly
#>
[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$SkipSelfCheck,
    [switch]$Offline,
    [string]$ToolchainSource,
    [string]$AvzSource,
    [int]$Jobs = 2
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

$toolchainDirectoryName = 'llvm-mingw-20260908-ucrt-x86_64'
$toolchainArchiveName = 'llvm-mingw-20260908.zip'
$avzSubmodulePath = 'avz/framework'
$referenceSavePath = 'experiments/scenarios/liangyi/game1_13.dat'
$injectorPath = 'avz/runtime/bin/injector.exe'
$selfCheckPlan = 'work/prepare-checkout-plan.json'

$lockPath = Join-Path $projectRoot 'dependencies.lock.json'
$workDirectory = Join-Path $projectRoot 'work'
$thirdPartyDirectory = Join-Path $projectRoot 'third_party'
$toolchainDirectory = Join-Path $thirdPartyDirectory $toolchainDirectoryName
$compilerPath = Join-Path $toolchainDirectory 'bin\i686-w64-mingw32-clang++.exe'
$avzDirectory = Join-Path $projectRoot ($avzSubmodulePath -replace '/', '\')
$runtimeDirectory = Join-Path $projectRoot 'avz\runtime'
$reportPath = Join-Path $workDirectory 'prepare-checkout-report.json'
$selfCheckLog = Join-Path $workDirectory 'prepare-checkout-selfcheck.log'

$steps = New-Object 'System.Collections.Generic.List[object]'
$warnings = New-Object 'System.Collections.Generic.List[string]'
$script:stepFailed = $false
$script:lastStepResult = $null
$failureDetail = $null
$overlayEvidence = $null
$checkoutIdentity = $null

if (!(Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "dependencies.lock.json not found in $projectRoot; run this script from tools/ inside the checkout"
}
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$toolchainArchiveSha256 = ([string]$lock.toolchain.sha256).ToLowerInvariant()
$toolchainUrl = [string]$lock.toolchain.archive
$pinnedAvzCommit = ([string]$lock.avz_commit).ToLowerInvariant()

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    $started = Get-Date
    Write-Host "== $Name"
    try {
        $value = & $Action
    } catch {
        $message = $_.Exception.Message
        if (!$message) { $message = ($_ | Out-String).Trim() }
        $steps.Add([pscustomobject]@{
            step    = $Name
            status  = 'fail'
            seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
            detail  = $message
        })
        throw
    }
    $script:lastStepResult = $value
    $steps.Add([pscustomobject]@{
        step    = $Name
        status  = 'pass'
        seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
        detail  = $null
        result  = $value
    })
    return $value
}

function Add-SkippedStep {
    param([string]$Name, [string]$Reason)
    Write-Host "== $Name (skipped: $Reason)"
    $steps.Add([pscustomobject]@{ step = $Name; status = 'skipped'; seconds = 0; detail = $Reason })
}

function Invoke-NativeChecked {
    param(
        [string]$Label,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$LogPath,
        [string]$WorkingDirectory
    )
    Write-Host ("   > " + $FilePath + " " + ($Arguments -join ' '))
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        if ($WorkingDirectory) { Pop-Location }
    }
    if ($LogPath) {
        Add-Content -LiteralPath $LogPath -Value ($output + @("exit code: $code", "")) -Encoding utf8
    }
    foreach ($line in $output) { Write-Host ("   " + $line) }
    if ($code -ne 0) {
        throw "$Label failed with exit code $code (log: $LogPath)"
    }
    return $output
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# runtime/avz_overlay.cmake reads each file with file(READ), replaces CRLF with
# LF and only then hashes it. The evidence below must use the very same steps:
# a hand-rolled "normalized SHA256" is easy to get wrong (dropping the LF while
# replacing the pair, or decoding bytes differently) and then reports a content
# change that never happened, which is the failure mode §2.5 warns about.
# Get-CMakeOverlayHashes therefore asks CMake itself to hash the files, and
# Get-SourceStats only adds byte counts for the report.
function Get-SourceStats([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $index = 0
    $crlfPairs = 0
    $loneCr = 0
    $normalizedLength = 0
    while ($index -lt $bytes.Length) {
        $current = $bytes[$index]
        if ($current -eq 13 -and ($index + 1) -lt $bytes.Length -and $bytes[$index + 1] -eq 10) {
            $crlfPairs++
            $normalizedLength++
            $index += 2
            continue
        }
        if ($current -eq 13) { $loneCr++ }
        $normalizedLength++
        $index++
    }
    return [pscustomobject]@{
        bytes             = $bytes.Length
        normalized_bytes  = $normalizedLength
        crlf_pairs        = $crlfPairs
        lone_cr           = $loneCr
    }
}

# Fallback for machines without cmake: the same three steps as the overlay.
function Get-NormalizedSha256([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $normalized = New-Object 'System.Collections.Generic.List[byte]' ($bytes.Length)
    $index = 0
    while ($index -lt $bytes.Length) {
        if ($bytes[$index] -eq 13 -and ($index + 1) -lt $bytes.Length -and $bytes[$index + 1] -eq 10) {
            $normalized.Add([byte]10)
            $index += 2
            continue
        }
        $normalized.Add($bytes[$index])
        $index++
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($normalized.ToArray())
    } finally {
        $sha.Dispose()
    }
    return (-join ($digest | ForEach-Object { $_.ToString('x2') }))
}

function Get-CMakeOverlayHashes {
    param([string]$Root, $Pins)
    $cmake = Get-Command cmake -ErrorAction SilentlyContinue
    if (!$cmake) { return $null }
    $lines = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in $Pins.Keys) {
        $full = (Join-Path $Root ($Pins[$name].path -replace '/', '\')).Replace('\', '/')
        $lines.Add("set(source_$name `"$full`")")
    }
    $lines.Add('set(pinned_sources ' + (($Pins.Keys | ForEach-Object { $_ }) -join ' ') + ')')
    $lines.Add('foreach(source_name ${pinned_sources})')
    $lines.Add('  if(NOT EXISTS "${source_${source_name}}")')
    $lines.Add('    message(STATUS "pin ${source_name} missing")')
    $lines.Add('    continue()')
    $lines.Add('  endif()')
    $lines.Add('  file(READ "${source_${source_name}}" ${source_name}_source)')
    $lines.Add('  string(REPLACE "\r\n" "\n" ${source_name}_source "${${source_name}_source}")')
    $lines.Add('  string(SHA256 ${source_name}_hash "${${source_name}_source}")')
    $lines.Add('  message(STATUS "pin ${source_name} ${${source_name}_hash}")')
    $lines.Add('endforeach()')
    $probe = Join-Path ([IO.Path]::GetTempPath()) ('lvz-overlay-hash-' + [guid]::NewGuid().ToString('N') + '.cmake')
    Set-Content -LiteralPath $probe -Value ($lines -join "`n") -Encoding utf8
    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $cmake.Source -P $probe 2>&1 | ForEach-Object { $_.ToString() })
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) { return $null }
    $hashes = @{}
    foreach ($line in $output) {
        if ($line -match '^--\s+pin\s+([a-z_]+)\s+([0-9a-f]{64})\s*$') {
            $hashes[$matches[1]] = $matches[2]
        }
    }
    if ($hashes.Count -eq 0) { return $null }
    return [pscustomobject]@{ method = 'cmake'; hashes = $hashes }
}

function Get-AvzOverlayPins {
    param([string]$Root)
    $overlay = Join-Path $Root 'runtime/avz_overlay.cmake'
    if (!(Test-Path -LiteralPath $overlay -PathType Leaf)) {
        throw "runtime/avz_overlay.cmake not found in $Root"
    }
    $text = Get-Content -LiteralPath $overlay -Raw
    $sources = [ordered]@{}
    foreach ($match in [regex]::Matches($text, 'set\(AVZ_([A-Za-z0-9_]+)\s+"\$\{CMAKE_SOURCE_DIR\}/([^"]+)"\)')) {
        $sources[$match.Groups[1].Value.ToLowerInvariant()] = $match.Groups[2].Value
    }
    $pins = [ordered]@{}
    foreach ($match in [regex]::Matches($text, '([A-Za-z_][A-Za-z0-9_]*)_hash\s+STREQUAL\s+"([0-9a-f]{64})"')) {
        $name = $match.Groups[1].Value
        if (!$sources.Contains($name)) { continue }
        $pins[$name] = [pscustomobject]@{ path = $sources[$name]; expected = $match.Groups[2].Value }
    }
    if ($pins.Count -eq 0) {
        throw ("runtime/avz_overlay.cmake in $Root exposes no pinned AvZ source hashes; " +
            'refusing to prepare a checkout whose overlay cannot be verified')
    }
    return $pins
}

# Verifying the pins here is the answer to the misleading failure recorded in
# docs/并行实验约定.md §2.5: `file(READ)` on a missing file only reports a
# non-fatal error and leaves the variable empty, so an uninitialised submodule
# produces the very same "Pinned AvZ hook/script source changed" fatal error as
# a real content change. Report which of the two happened before CMake runs.
function Get-AvzOverlayEvidence {
    param([string]$Root)
    $pins = Get-AvzOverlayPins -Root $Root
    $digest = Get-CMakeOverlayHashes -Root $Root -Pins $pins
    $method = 'powershell'
    if ($digest) { $method = $digest.method }
    $items = New-Object 'System.Collections.Generic.List[object]'
    foreach ($name in $pins.Keys) {
        $relative = $pins[$name].path
        $path = Join-Path $Root ($relative -replace '/', '\')
        $item = [ordered]@{
            name            = $relative
            pin             = $name
            expected        = $pins[$name].expected
            present         = $false
            match           = $false
            sha256_normalized = $null
        }
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $item.present = $true
            $stats = Get-SourceStats -Path $path
            $item.bytes = $stats.bytes
            $item.normalized_bytes = $stats.normalized_bytes
            $item.crlf_pairs = $stats.crlf_pairs
            $item.lone_cr = $stats.lone_cr
            if ($digest) {
                $item.sha256_normalized = $digest.hashes[$name]
            } else {
                $item.sha256_normalized = Get-NormalizedSha256 -Path $path
            }
            $item.match = ($item.sha256_normalized -eq $pins[$name].expected)
        }
        $items.Add([pscustomobject]$item)
    }
    return [pscustomobject]@{
        overlay      = 'runtime/avz_overlay.cmake'
        hash_method  = $method
        items        = $items.ToArray()
    }
}

function Assert-AvzOverlayPins {
    param([string]$Root)
    $evidence = Get-AvzOverlayEvidence -Root $Root
    $missing = @($evidence.items | Where-Object { -not $_.present })
    if ($missing.Count -gt 0) {
        $names = ($missing | ForEach-Object { $_.name }) -join ', '
        throw ("The AvZ submodule content is missing ($names). runtime/avz_overlay.cmake reports this as " +
            "'Pinned AvZ hook/script source changed' at line 17 because file(READ) leaves the variable empty, " +
            "not because the content changed. Fix: git -C `"$Root`" submodule update --init --depth 1 $avzSubmodulePath")
    }
    $changed = @($evidence.items | Where-Object { -not $_.match })
    if ($changed.Count -gt 0) {
        $lines = $changed | ForEach-Object {
            ("{0}: expected {1} got {2} (bytes={3} normalized_bytes={4} crlf_pairs={5} lone_cr={6})" -f `
                    $_.name, $_.expected, $_.sha256_normalized, $_.bytes, $_.normalized_bytes, $_.crlf_pairs, $_.lone_cr)
        }
        throw ("Pinned AvZ sources really differ from runtime/avz_overlay.cmake " +
            "(hashes computed the way the overlay does, $($evidence.hash_method)):`n  " + ($lines -join "`n  "))
    }
    return $evidence
}

function Resolve-ToolchainArchive {
    param([string]$Root, [string]$Explicit)
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit -PathType Container) {
            $candidates.Add((Join-Path $Explicit $toolchainArchiveName))
        } else {
            $candidates.Add($Explicit)
        }
    }
    if ($env:LVZ_TOOLCHAIN_ZIP) { $candidates.Add($env:LVZ_TOOLCHAIN_ZIP) }
    $candidates.Add((Join-Path $Root ('work/' + $toolchainArchiveName)))
    # Linked worktrees keep their git directory inside the main checkout.
    $commonDirectory = & git -C $Root rev-parse --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $commonDirectory) {
        $common = $commonDirectory.Trim()
        if (!$common.StartsWith('/') -and !$common.Contains(':')) {
            $common = Join-Path $Root $common
        }
        $mainCheckout = Split-Path -Parent $common
        if ($mainCheckout -and $mainCheckout.TrimEnd('\') -ne $Root.TrimEnd('\')) {
            $candidates.Add((Join-Path $mainCheckout ('work/' + $toolchainArchiveName)))
        }
    }
    # Clones created next to the main checkout (the layout in §5.1:
    # <root>\work\<world>).
    foreach ($level in 1, 2) {
        $base = $Root
        for ($index = 0; $index -lt $level; $index++) { $base = Split-Path -Parent $base }
        if (!$base) { continue }
        $candidates.Add((Join-Path $base $toolchainArchiveName))
        $candidates.Add((Join-Path $base ('work/' + $toolchainArchiveName)))
        foreach ($sibling in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
            $candidates.Add((Join-Path $sibling.FullName ('work/' + $toolchainArchiveName)))
        }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $hash = Get-FileSha256 -Path $candidate
            if ($hash -eq $toolchainArchiveSha256) { return [pscustomobject]@{ path = $candidate; sha256 = $hash } }
        }
    }
    return $null
}

function Get-LockedFileHash {
    param([string]$Relative)
    foreach ($entry in $lock.files) {
        if (([string]$entry.path).Replace('\', '/') -eq $Relative.Replace('\', '/')) {
            return ([string]$entry.sha256).ToLowerInvariant()
        }
    }
    return $null
}

function Write-PrepareCheckoutReport {
    if (!(Test-Path -LiteralPath $workDirectory -PathType Container)) { return $null }
    $mode = 'prepare'
    if ($VerifyOnly) { $mode = 'verify-only' }
    $result = 'pass'
    if ($script:stepFailed) { $result = 'fail' }
    $report = [ordered]@{
        schema      = 'lvz.prepare-checkout.v1'
        created_at  = (Get-Date).ToUniversalTime().ToString('o')
        mode        = $mode
        result      = $result
        failure     = $failureDetail
        checkout    = $checkoutIdentity
        pins        = [ordered]@{
            avz_commit        = $pinnedAvzCommit
            toolchain_sha256  = $toolchainArchiveSha256
            toolchain_archive = $toolchainUrl
        }
        avz_overlay = $overlayEvidence
        steps       = $steps.ToArray()
        warnings    = $warnings.ToArray()
    }
    $json = $report | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath $reportPath -Value $json -Encoding utf8
    return $reportPath
}

try {
    $checkoutIdentity = Invoke-Step 'checkout identity' {
        $head = (& git -C $projectRoot rev-parse HEAD).Trim()
        $branch = (& git -C $projectRoot rev-parse --abbrev-ref HEAD).Trim()
        $dirty = @(& git -C $projectRoot status --porcelain).Count -gt 0
        $shallow = ((& git -C $projectRoot rev-parse --is-shallow-repository).Trim() -eq 'true')
        Write-Host "   $head ($branch) dirty=$dirty shallow=$shallow"
        [pscustomobject]@{ root = $projectRoot; head = $head; branch = $branch; dirty = $dirty; shallow = $shallow }
    }

    Invoke-Step 'work directory' {
        if ($VerifyOnly) {
            if (!(Test-Path -LiteralPath $workDirectory -PathType Container)) {
                throw ("work/ is missing; 'evaluation plan work\<name>.json' fails with FileNotFoundError " +
                    'without it. Re-run without -VerifyOnly.')
            }
        } else {
            New-Item -ItemType Directory -Force -Path $workDirectory, $thirdPartyDirectory | Out-Null
        }
        $probe = Join-Path $workDirectory '.prepare-checkout-write-probe'
        try {
            Set-Content -LiteralPath $probe -Value 'probe' -Encoding utf8
        } catch {
            throw "work/ exists but is not writable: $($_.Exception.Message)"
        } finally {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
        Write-Host "   $workDirectory is writable"
        [pscustomobject]@{ work_directory = $workDirectory; writable = $true }
    }

    Invoke-Step 'avz submodule' {
        $gitlink = ((& git -C $projectRoot ls-tree HEAD -- $avzSubmodulePath) -split '\s+')[2]
        if ($gitlink -and $gitlink.ToLowerInvariant() -ne $pinnedAvzCommit) {
            $warnings.Add("HEAD records AvZ commit $gitlink but dependencies.lock.json pins $pinnedAvzCommit; the overlay pins follow the recorded commit")
        }
        $head = $null
        if (Test-Path -LiteralPath (Join-Path $avzDirectory '.git')) {
            $head = (& git -C $avzDirectory rev-parse HEAD).Trim()
        }
        $action = 'none'
        $mirror = $null
        if ($head -ne $pinnedAvzCommit) {
            if ($VerifyOnly) {
                $state = 'not initialised'
                if ($head) { $state = "at $head" }
                throw "$avzSubmodulePath is $state but dependencies.lock.json pins $pinnedAvzCommit; re-run without -VerifyOnly"
            }
            if ($Offline -and !$AvzSource) {
                throw 'the AvZ submodule needs a checkout: drop -Offline, or pass -AvzSource <local AvZ repository>'
            }
            $arguments = @('-C', $projectRoot)
            if ($AvzSource) {
                $mirror = (Resolve-Path -LiteralPath $AvzSource).Path
                $arguments += @('-c', 'protocol.file.allow=always', '-c', "submodule.$avzSubmodulePath.url=$mirror")
                $action = "submodule update from local mirror $mirror"
            } else {
                $action = 'submodule update from the .gitmodules URL'
            }
            $arguments += @('submodule', 'update', '--init', '--depth', '1', $avzSubmodulePath)
            Invoke-NativeChecked -Label 'git submodule update' -FilePath 'git' -Arguments $arguments
            $head = (& git -C $avzDirectory rev-parse HEAD).Trim()
        }
        # Without an entry in the superproject config, `git submodule status`
        # prints '-' for a perfectly good checkout and a later
        # `git submodule update --init` stops with "A git directory for
        # 'avz/framework' is found locally". Record how this checkout fetches
        # the submodule: the mirror that was actually used, or .gitmodules.
        $configured = & git -C $projectRoot config --get "submodule.$avzSubmodulePath.url"
        if (!$configured) {
            if ($VerifyOnly) {
                $warnings.Add("$avzSubmodulePath is not registered in $projectRoot/.git/config; git submodule status prints '-' and a later 'git submodule update --init' can stop with 'A git directory for ... is found locally'. Re-run without -VerifyOnly to record the fetch URL.")
            } elseif ($mirror) {
                Invoke-NativeChecked -Label 'git config submodule url' -FilePath 'git' `
                    -Arguments @('-C', $projectRoot, 'config', "submodule.$avzSubmodulePath.url", $mirror)
                $configured = $mirror
            } else {
                Invoke-NativeChecked -Label 'git submodule sync' -FilePath 'git' `
                    -Arguments @('-C', $projectRoot, 'submodule', 'sync', '--', $avzSubmodulePath)
                $configured = (& git -C $projectRoot config --get "submodule.$avzSubmodulePath.url")
            }
        }
        if ($head -ne $pinnedAvzCommit) {
            throw "$avzSubmodulePath is at $head, dependencies.lock.json pins $pinnedAvzCommit"
        }
        $porcelain = @(& git -C $avzDirectory status --porcelain)
        if ($porcelain.Count -gt 0) {
            throw ("$avzSubmodulePath has local modifications and the runtime overlay requires the pristine submodule. " +
                "Review them, then discard with: git -C `"$avzDirectory`" checkout -- .")
        }
        Write-Host "   $avzSubmodulePath at $head (pristine), action: $action"
        [pscustomobject]@{ path = $avzSubmodulePath; head = $head; pristine = $true; action = $action; fetch_url = $configured }
    }

    Invoke-Step 'avz runtime injector' {
        $injector = Join-Path $projectRoot ($injectorPath -replace '/', '\')
        $expected = Get-LockedFileHash $injectorPath
        if (!(Test-Path -LiteralPath $injector -PathType Leaf)) {
            if ($VerifyOnly) { throw "$injectorPath is missing; re-run without -VerifyOnly" }
            $archive = Join-Path $projectRoot ("$avzSubmodulePath/release/env2/$($lock.avz_runtime_release).zip")
            if (!(Test-Path -LiteralPath $archive -PathType Leaf)) {
                throw "AvZ runtime archive not found: $archive (initialise the submodule first)"
            }
            New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
            Expand-Archive -LiteralPath $archive -DestinationPath $runtimeDirectory -Force
        }
        if (!(Test-Path -LiteralPath $injector -PathType Leaf)) {
            throw "$injectorPath still missing after extraction"
        }
        $actual = Get-FileSha256 -Path $injector
        if ($expected -and $actual -ne $expected) {
            throw "$injectorPath hash mismatch: expected $expected got $actual"
        }
        Write-Host "   $injectorPath $actual"
        [pscustomobject]@{ path = $injectorPath; sha256 = $actual; expected = $expected; match = ($actual -eq $expected) }
    }

    Invoke-Step 'reference save' {
        $save = Join-Path $projectRoot ($referenceSavePath -replace '/', '\')
        if (!(Test-Path -LiteralPath $save -PathType Leaf)) {
            if ($VerifyOnly) { throw "$referenceSavePath is missing; re-run without -VerifyOnly" }
            $tutorial = Join-Path $avzDirectory 'tutorial\scripts\liang_yi\game1_13.dat'
            if (!(Test-Path -LiteralPath $tutorial -PathType Leaf)) {
                throw "tutorial save not found: $tutorial (initialise the submodule first)"
            }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $save) | Out-Null
            Copy-Item -LiteralPath $tutorial -Destination $save
        }
        $expected = Get-LockedFileHash $referenceSavePath
        $actual = Get-FileSha256 -Path $save
        if ($expected -and $actual -ne $expected) {
            throw "$referenceSavePath hash mismatch: expected $expected got $actual"
        }
        Write-Host "   $referenceSavePath $actual"
        [pscustomobject]@{ path = $referenceSavePath; sha256 = $actual; expected = $expected; match = ($actual -eq $expected) }
    }

    Invoke-Step 'jingdian12 save' {
        $relative = 'experiments/scenarios/jingdian12/game1_13.dat'
        $save = Join-Path $projectRoot ($relative -replace '/', '\')
        if (!(Test-Path -LiteralPath $save -PathType Leaf)) {
            if ($VerifyOnly) { throw "$relative is missing; re-run without -VerifyOnly" }
            $tutorial = Join-Path $avzDirectory 'tutorial\scripts\jing_dian_12\game1_13.dat'
            if (!(Test-Path -LiteralPath $tutorial -PathType Leaf)) {
                throw "tutorial save not found: $tutorial (initialise the submodule first)"
            }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $save) | Out-Null
            Copy-Item -LiteralPath $tutorial -Destination $save
        }
        $expected = Get-LockedFileHash $relative
        $actual = Get-FileSha256 -Path $save
        if ($expected -and $actual -ne $expected) {
            throw "$relative hash mismatch: expected $expected got $actual"
        }
        Write-Host "   $relative $actual"
        [pscustomobject]@{ path = $relative; sha256 = $actual; expected = $expected; match = ($actual -eq $expected) }
    }

    Invoke-Step 'toolchain' {
        if (Test-Path -LiteralPath $compilerPath -PathType Leaf) {
            Write-Host "   compiler already present: $compilerPath"
            return [pscustomobject]@{ compiler = $compilerPath; extracted = $false; archive = $null; archive_sha256 = $null }
        }
        if ($VerifyOnly) { throw "compiler missing: $compilerPath; re-run without -VerifyOnly" }
        $archive = Join-Path $workDirectory $toolchainArchiveName
        if (!(Test-Path -LiteralPath $archive -PathType Leaf)) {
            $source = Resolve-ToolchainArchive -Root $projectRoot -Explicit $ToolchainSource
            if ($source) {
                Copy-Item -LiteralPath $source.path -Destination $archive -Force
                Write-Host "   copied $toolchainArchiveName from $($source.path)"
            } elseif ($Offline) {
                throw "-Offline was requested but no local $toolchainArchiveName was found; pass -ToolchainSource <path> or copy it into $workDirectory"
            } else {
                Write-Host "   downloading $toolchainUrl"
                Invoke-WebRequest -Uri $toolchainUrl -OutFile $archive
            }
        }
        $actual = Get-FileSha256 -Path $archive
        if ($actual -ne $toolchainArchiveSha256) {
            throw "compiler archive hash mismatch: expected $toolchainArchiveSha256 got $actual ($archive). Delete the file and re-run."
        }
        New-Item -ItemType Directory -Force -Path $thirdPartyDirectory | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $thirdPartyDirectory -Force
        if (!(Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
            throw "compiler still missing after extraction: $compilerPath"
        }
        Write-Host "   extracted to $toolchainDirectory"
        [pscustomobject]@{ compiler = $compilerPath; extracted = $true; archive = $archive; archive_sha256 = $actual }
    }

    $overlayEvidence = Invoke-Step 'avz overlay pins' {
        $evidence = Assert-AvzOverlayPins -Root $projectRoot
        foreach ($item in $evidence.items) {
            Write-Host ("   {0} -> {1} (expected {2}, crlf_pairs={3}, lone_cr={4})" -f `
                    $item.name, $item.sha256_normalized, $item.expected, $item.crlf_pairs, $item.lone_cr)
        }
        $evidence
    }

    Invoke-Step 'game files (informational)' {
        $present = @()
        foreach ($entry in $lock.files) {
            $relative = ([string]$entry.path).Replace('\', '/')
            if ($relative.StartsWith('game/')) {
                $path = Join-Path $projectRoot ($relative -replace '/', '\')
                if (Test-Path -LiteralPath $path -PathType Leaf) { $present += $relative }
            }
        }
        if ($present.Count -eq 0) {
            $warnings.Add('no local game files found; the self-check still passes, but private_launch/scenario runs need game/original and game/local-engine')
            Write-Host '   none present (fine for the self-check)'
        } else {
            Write-Host "   $($present.Count) locked game files present"
        }
        [pscustomobject]@{ present = $present }
    }

    if ($SkipSelfCheck) {
        Add-SkippedStep 'self-check' '-SkipSelfCheck'
    } elseif ($VerifyOnly) {
        Add-SkippedStep 'self-check' '-VerifyOnly'
    } else {
        Invoke-Step 'self-check' {
            Set-Content -LiteralPath $selfCheckLog -Value ("prepare-checkout self-check " + (Get-Date).ToUniversalTime().ToString('o')) -Encoding utf8
            $python = Get-Command python -ErrorAction SilentlyContinue
            $ctest = Get-Command ctest -ErrorAction SilentlyContinue
            $powershell = Get-Command pwsh -ErrorAction SilentlyContinue
            if (!$powershell) { $powershell = Get-Command powershell -ErrorAction SilentlyContinue }
            if (!$powershell) { throw 'neither pwsh nor powershell found on PATH' }

            # 1) `evaluation plan` writes work/<name>.json. This is the check that
            #    catches the missing work/ directory before a run does.
            $planFile = Join-Path $projectRoot ($selfCheckPlan -replace '/', '\')
            if (Test-Path -LiteralPath $planFile -PathType Leaf) {
                $existing = Get-Content -LiteralPath $planFile -Raw | ConvertFrom-Json
                if ($existing.schema -ne 'lvz.evaluation-plan.v2' -and $existing.schema -ne 'lvz.evaluation-plan.v1') {
                    throw "$selfCheckPlan exists but is not an evaluation plan (schema: $($existing.schema))"
                }
                Write-Host "   $selfCheckPlan already exists (schema $($existing.schema)); kept as is"
            } elseif (!$python) {
                $warnings.Add('python not found on PATH; skipped the plan-write smoke test and the Python unit tests')
                Write-Host '   python not found; skipped plan-write smoke test'
            } else {
                $env:PYTHONPATH = Join-Path $projectRoot 'src'
                Invoke-NativeChecked -Label 'evaluation plan' -FilePath $python.Source -LogPath $selfCheckLog `
                    -WorkingDirectory $projectRoot `
                    -Arguments @('-m', 'llm_vs_zombies.evaluation', 'plan', $selfCheckPlan,
                        '--tier', 'smoke', '--seeds', '42', '--cold-starts', '1', '--tick-budget', '2000',
                        '--audio-mode', 'sound_effects_allocation_none_v1',
                        '--strategy', 'examples/liangyi_gargantuar_a.py')
            }

            # 2..5) the exact commands the evaluation runner's build_and_tests step
            #    runs, in the same order (src/llm_vs_zombies/evaluation.py build_checks).
            Invoke-NativeChecked -Label 'native build' -LogPath $selfCheckLog -WorkingDirectory $projectRoot `
                -FilePath $powershell.Source `
                -Arguments @('-NoProfile', '-File', (Join-Path $projectRoot 'tools/build-avz.ps1'), '-Jobs', "$Jobs")
            Invoke-NativeChecked -Label 'launcher build' -LogPath $selfCheckLog -WorkingDirectory $projectRoot `
                -FilePath $powershell.Source `
                -Arguments @('-NoProfile', '-File', (Join-Path $projectRoot 'launcher/build.ps1'))
            if ($python) {
                $env:PYTHONPATH = Join-Path $projectRoot 'src'
                Invoke-NativeChecked -Label 'python tests' -FilePath $python.Source -LogPath $selfCheckLog `
                    -WorkingDirectory $projectRoot `
                    -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
            }
            if (!$ctest) { throw 'ctest not found on PATH' }
            Invoke-NativeChecked -Label 'ctest' -FilePath $ctest.Source -LogPath $selfCheckLog `
                -WorkingDirectory $projectRoot `
                -Arguments @('--test-dir', 'build/cmake', '--output-on-failure')

            Write-Host "   log: $selfCheckLog"
            [pscustomobject]@{ log = $selfCheckLog; plan = $selfCheckPlan }
        }
    }
} catch {
    $script:stepFailed = $true
    $failureDetail = $_.Exception.Message
    if (!$failureDetail) { $failureDetail = ($_ | Out-String).Trim() }
} finally {
    $written = Write-PrepareCheckoutReport
    # The summary stays on the output stream so `pwsh -File tools/prepare-checkout.ps1 > log`
    # and CI logs keep the machine-relevant lines; per-step progress stays on the host.
    Write-Output ''
    Write-Output 'prepare-checkout summary'
    $steps | Format-Table -Property step, status, seconds, detail -AutoSize | Out-String | Write-Output
    foreach ($warning in $warnings) { Write-Output "warning: $warning" }
    if ($checkoutIdentity) {
        Write-Output "checkout: $($checkoutIdentity.root) @ $($checkoutIdentity.head) ($($checkoutIdentity.branch))"
    }
    if ($written) { Write-Output "evidence: $written" }
    if ($script:stepFailed) {
        Write-Output "FAILED: $failureDetail"
    } else {
        Write-Output 'Checkout ready.'
        if (!$SkipSelfCheck -and !$VerifyOnly) {
            Write-Output ''
            Write-Output 'Next steps (see docs/并行实验约定.md §5):'
            Write-Output "  `$env:PYTHONPATH = 'src'"
            Write-Output ('  python -m llm_vs_zombies.evaluation plan work\<world>-plan.json --tier smoke --seeds 42 ' +
                '--cold-starts 1 --tick-budget 2000 --audio-mode sound_effects_allocation_none_v1 ' +
                '--strategy examples\liangyi_gargantuar_a.py')
            Write-Output '  python -m llm_vs_zombies.evaluation run work\<world>-plan.json --output experiments\runs\<world>-s42-c0'
        }
    }
}
if ($script:stepFailed) { exit 1 }
exit 0
