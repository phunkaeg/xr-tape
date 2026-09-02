# End-to-end verification for xr-tape.
#
#   1. the falsification matrix - every check proven able to fail
#   2. tape every OpenXR client found below -CatalogRoot
#   3. check each resulting trace
#
# A client that is not built is reported as SKIP, and a client that bypasses the
# Khronos loader is reported as SKIP with that named as the reason - never as a
# pass. A green run that silently covered nothing is the failure this fleet keeps
# rediscovering.
[CmdletBinding()]
param(
    [string]$CatalogRoot,
    [ValidateSet("x86", "x64")][string]$Architecture = 'x64',
    [string]$RuntimeJson
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $CatalogRoot) { $CatalogRoot = Split-Path -Parent $repo }
$python = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }

# Clients this fleet already builds. Each is a real OpenXR application; none is
# a game, so nothing here launches one.
#
# Known is for findings that are TRUE and EXPECTED for that client. They are
# reported as XFAIL with the reason, never suppressed - and an expectation that
# stops firing is reported too, because a stale expectation quietly hides a
# regression the day the client changes.
$clients = @(
    @{ Id = 'preyvr';  Path = 'PreyVR\build\headless\Release\preyvr_xr_session_probe.exe'
       Args = @('30'); Known = @{} },
    @{ Id = 'somavr';  Path = 'SOMAVR\build\Release\somavr_xrsim_smoke.exe'
       Args = @('--frames', '60'); Known = @{} },
    @{ Id = 'ss2vr';   Path = 'ss2vr-work\build\Release\xr_hello64.exe'
       Args = @()
       Known = @{ never_submits_zero_layers =
                  'by design - xr_hello64 pumps empty frames to prove session health only' } },
    @{ Id = 'sims4vr'; Path = 'Sims4VR\tools\xrprobe\bin\sims4vr_xrprobe.exe'
       Args = @(); Known = @{} }
)

function Test-UsesKhronosLoader([string]$Path) {
    # A client that hand-loads a runtime DLL and calls its dispatch table
    # directly never creates a loader instance, so there is no loader for an API
    # layer to be inserted into. That is a deliberate design in some probes -
    # it isolates runtime defects from manifest and loader defects - and it puts
    # them permanently out of xr-tape's reach.
    $dir = Split-Path -Parent $Path
    if (Test-Path (Join-Path $dir 'openxr_loader.dll')) { return $true }
    $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($Path))
    return -not ($text.Contains('xrNegotiateLoaderRuntimeInterface'))
}

Write-Host "=== 1. falsification matrix ==="
& $python (Join-Path $PSScriptRoot 'xrtape_selftest.py') | Out-Host
$selftest = $LASTEXITCODE
Write-Host ""

if (-not $RuntimeJson) {
    $candidate = Join-Path $env:LOCALAPPDATA "xr-tape\xrsim-runtime\xrsim-$Architecture.json"
    if (Test-Path $candidate) { $RuntimeJson = $candidate }
}

Write-Host "=== 2. tape and check clients ($Architecture) ==="
$rows = @()
foreach ($client in $clients) {
    $path = Join-Path $CatalogRoot $client.Path
    if (-not (Test-Path -LiteralPath $path)) {
        $rows += [pscustomobject]@{ Client = $client.Id; Status = 'SKIP'; Detail = 'not built' }
        continue
    }
    if (-not (Test-UsesKhronosLoader $path)) {
        $rows += [pscustomobject]@{ Client = $client.Id; Status = 'SKIP'
                                    Detail = 'hand-loads the runtime; no loader to layer into' }
        continue
    }
    try {
        $invokeArgs = @{ Executable = $path; Arguments = $client.Args; TimeoutSeconds = 120 }
        if ($RuntimeJson) { $invokeArgs.RuntimeJson = $RuntimeJson }
        $run = & (Join-Path $PSScriptRoot 'Invoke-XrTape.ps1') @invokeArgs 2>&1 |
            Where-Object { $_ -is [pscustomobject] } | Select-Object -Last 1
        if (-not $run -or -not $run.Trace) { throw 'no trace returned' }

        $report = & $python (Join-Path $PSScriptRoot 'xrtape_check.py') $run.Trace --json |
            ConvertFrom-Json

        $unexpected = @()
        $expected = @()
        foreach ($result in $report.results) {
            if ($result.status -ne 'FAIL') { continue }
            if ($client.Known.ContainsKey($result.name)) {
                $expected += "$($result.name) ($($client.Known[$result.name]))"
            } else {
                $unexpected += "$($result.name): $($result.detail)"
            }
        }
        # An expectation that stopped firing is a finding of its own.
        $stale = @($client.Known.Keys | Where-Object { $n = $_
            -not ($report.results | Where-Object { $_.name -eq $n -and $_.status -eq 'FAIL' }) })

        $status = if ($unexpected.Count -gt 0) { 'FAIL' }
                  elseif ($stale.Count -gt 0)  { 'STALE' }
                  elseif ($expected.Count -gt 0) { 'XFAIL' }
                  else { 'PASS' }

        Write-Host ("  {0,-8} {1,-6} {2} passed / {3} failed / {4} skipped   [{5}, {6}]" -f `
            $client.Id, $status, $report.passed, $report.failed, $report.skipped,
            $report.runtime, (Split-Path -Leaf $run.Trace))
        foreach ($line in $expected)   { Write-Host "             XFAIL $line" }
        foreach ($line in $unexpected) { Write-Host "             FAIL  $line" }
        foreach ($line in $stale) {
            Write-Host "             STALE expectation no longer fires: $line"
        }

        $rows += [pscustomobject]@{
            Client = $client.Id; Status = $status
            Detail = "$($report.passed)P/$($report.failed)F/$($report.skipped)S"
        }
    } catch {
        $rows += [pscustomobject]@{ Client = $client.Id; Status = 'ERROR'; Detail = "$_" }
        Write-Host ("  {0,-8} ERROR  {1}" -f $client.Id, $_)
    }
}

Write-Host ""
$rows | Format-Table -AutoSize | Out-Host

$bad = @($rows | Where-Object { $_.Status -in @('FAIL', 'ERROR', 'STALE') })
$taped = @($rows | Where-Object { $_.Status -in @('PASS', 'XFAIL') })
$skipped = @($rows | Where-Object { $_.Status -eq 'SKIP' })

Write-Host ("selftest {0}; {1} client(s) taped, {2} skipped, {3} failed" -f `
    $(if ($selftest -eq 0) { 'PASS' } else { 'FAIL' }), $taped.Count, $skipped.Count, $bad.Count)

if ($selftest -ne 0 -or $bad.Count -gt 0) { exit 1 }
if ($taped.Count -eq 0) {
    Write-Host 'NOTHING WAS TAPED - a green run that covered no client proves nothing.'
    exit 1
}
exit 0
