# Run an OpenXR application with the xr-tape layer attached, then verify a trace
# actually appeared.
#
# Three ways this fails SILENTLY, each turned into a thrown error. They are the
# same three the fleet's runtime launchers already learned, because the loader
# treats layer selection and runtime selection the same way:
#
#   1. An ELEVATED shell makes the loader ignore XR_API_LAYER_PATH and
#      XR_ENABLE_API_LAYERS (secure-environment path), so the app runs with no
#      layer and nothing says so.
#   2. A layer whose PE MACHINE does not match the application is skipped by the
#      loader without an error.
#   3. Either of the above leaves the application running perfectly - and every
#      later conclusion attributed to a trace that was never written.
#
# Guard 3 is the point of the script: it requires a NEW trace file with a header
# record. A check that only prints is not a check.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Executable,
    [string[]]$Arguments = @(),
    [ValidateSet("x86", "x64", "auto")][string]$Architecture = "auto",
    [string]$LayerDir,
    [string]$TraceDir,
    [string]$RuntimeJson,
    [string]$StampFile,
    [int]$MaxFrames = 0,
    [int]$TimeoutSeconds = 180,
    [switch]$Check,
    [string]$ExpectRuntime
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Get-PeMachine([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $reader = New-Object IO.BinaryReader($stream)
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) { throw "not a PE image: $Path" }
        return $reader.ReadUInt16()
    } finally {
        $stream.Dispose()
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run xr-tape from a normal PowerShell window; the OpenXR loader ignores layer selection for elevated processes, so the run would silently record nothing.'
}

$Executable = [IO.Path]::GetFullPath($Executable)
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "executable not found: $Executable"
}

$exeMachine = Get-PeMachine $Executable
$exeArch = switch ($exeMachine) {
    0x014C { 'x86' }
    0x8664 { 'x64' }
    default { throw ("unsupported machine type 0x{0:X4} in {1}" -f $exeMachine, $Executable) }
}
if ($Architecture -eq 'auto') { $Architecture = $exeArch }
if ($Architecture -ne $exeArch) {
    throw "requested $Architecture but $Executable is $exeArch; the loader would skip the layer."
}

if (-not $LayerDir) { $LayerDir = Join-Path $env:LOCALAPPDATA "xr-tape\layer-$Architecture" }
$manifest = Join-Path $LayerDir 'xrtape_api_layer.json'
$layerDll = Join-Path $LayerDir 'xrtape_layer.dll'
if (-not (Test-Path -LiteralPath $manifest) -or -not (Test-Path -LiteralPath $layerDll)) {
    throw "xr-tape is not installed for $Architecture. Run: tools\Install-XrTape.ps1 -Architecture $Architecture"
}

$layerMachine = Get-PeMachine $layerDll
if ($layerMachine -ne $exeMachine) {
    throw ("layer is 0x{0:X4} but the application is 0x{1:X4}; the loader would skip it silently." -f $layerMachine, $exeMachine)
}

# Per-run trace directory by default. Several projects on this machine already
# share %LOCALAPPDATA% roots and read each other's state; that collision is
# cheaper to design out than to diagnose.
if (-not $TraceDir) {
    $leaf = [IO.Path]::GetFileNameWithoutExtension($Executable)
    $TraceDir = Join-Path $env:LOCALAPPDATA "xr-tape\$leaf"
}
New-Item -ItemType Directory -Force -Path $TraceDir | Out-Null
$before = @(Get-ChildItem -LiteralPath $TraceDir -Filter '*.ndjson' -ErrorAction SilentlyContinue |
    ForEach-Object { $_.FullName })

$saved = @{
    XR_API_LAYER_PATH   = $env:XR_API_LAYER_PATH
    XR_ENABLE_API_LAYERS = $env:XR_ENABLE_API_LAYERS
    XRTAPE_DIR          = $env:XRTAPE_DIR
    XRTAPE_MAX_FRAMES   = $env:XRTAPE_MAX_FRAMES
    XRTAPE_STAMP_FILE   = $env:XRTAPE_STAMP_FILE
    XR_RUNTIME_JSON     = $env:XR_RUNTIME_JSON
}
try {
    $env:XR_API_LAYER_PATH = $LayerDir
    $env:XR_ENABLE_API_LAYERS = 'XR_APILAYER_XRTAPE_recorder'
    $env:XRTAPE_DIR = $TraceDir
    if ($MaxFrames -gt 0) { $env:XRTAPE_MAX_FRAMES = "$MaxFrames" }
    if ($StampFile) { $env:XRTAPE_STAMP_FILE = [IO.Path]::GetFullPath($StampFile) }
    if ($RuntimeJson) { $env:XR_RUNTIME_JSON = [IO.Path]::GetFullPath($RuntimeJson) }

    Write-Host "xr-tape: $([IO.Path]::GetFileName($Executable)) ($Architecture) -> $TraceDir"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -NoNewWindow
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill()
        throw "application did not exit within $TimeoutSeconds seconds"
    }
    $exitCode = $process.ExitCode
} finally {
    foreach ($name in $saved.Keys) {
        Set-Item -Path "Env:$name" -Value $saved[$name] -ErrorAction SilentlyContinue
        if (-not $saved[$name]) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
    }
}

$after = @(Get-ChildItem -LiteralPath $TraceDir -Filter '*.ndjson' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending)
$fresh = @($after | Where-Object { $before -notcontains $_.FullName })
if ($fresh.Count -eq 0) {
    throw "the application exited $exitCode but wrote NO trace. The layer did not load: check that $manifest is valid and that this shell is not elevated."
}
$trace = $fresh[0].FullName

# Read the answer out of the trace rather than trusting that the environment
# took. A run against the wrong runtime looks identical to a correct one.
$header = Get-Content -LiteralPath $trace -TotalCount 1 | ConvertFrom-Json
if (-not $header -or $header.r -ne 'header') {
    throw "trace $trace has no header record; the layer wrote a file it did not initialise."
}

$result = [pscustomobject]@{
    Trace      = $trace
    Exe        = $Executable
    ExitCode   = $exitCode
    LayerDir   = $LayerDir
    TraceDir   = $TraceDir
}
Write-Host "xr-tape: exit $exitCode, trace $trace"

if ($Check) {
    $python = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }
    $checker = Join-Path $PSScriptRoot 'xrtape_check.py'
    $checkArgs = @($checker, $trace)
    if ($ExpectRuntime) { $checkArgs += @('--expect-runtime', $ExpectRuntime) }
    & $python @checkArgs
    $result | Add-Member -NotePropertyName CheckExitCode -NotePropertyValue $LASTEXITCODE
}

$result
