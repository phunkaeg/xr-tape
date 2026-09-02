# Install the xr-tape layer into a directory the OpenXR loader can be pointed at
# with XR_API_LAYER_PATH.
#
# Nothing is written to the registry. Layer selection is per process, exactly as
# runtime selection is, so the machine's own OpenXR configuration is untouched
# and a connected headset keeps working while a taped run is in progress.
[CmdletBinding()]
param(
    [ValidateSet("x86", "x64")][string]$Architecture = "x64",
    [ValidateSet("Debug", "RelWithDebInfo", "Release")][string]$Configuration = "RelWithDebInfo",
    [string]$BuildDir,
    [string]$Dir
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $BuildDir) { $BuildDir = Join-Path $repo "build\$Architecture-vs" }
if (-not $Dir) { $Dir = Join-Path $env:LOCALAPPDATA "xr-tape\layer-$Architecture" }

$source = Join-Path $BuildDir "bin\$Configuration\xrtape_layer.dll"
if (-not (Test-Path -LiteralPath $source)) {
    throw "layer not built: $source`nRun tools\build.ps1 -Architecture $Architecture first."
}

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$target = Join-Path $Dir 'xrtape_layer.dll'
try {
    Copy-Item -LiteralPath $source -Destination $target -Force
} catch {
    # A locked DLL means some process still has the layer loaded. Forcing the
    # copy would leave a half-written binary; saying so is more useful.
    throw "could not replace $target - a process may still have the layer loaded. $_"
}

# The manifest is written here, next to the DLL it names, with an absolute path.
# Generating it from CMake as well would create a second source of truth and a
# stale manifest pointing at yesterday's build.
$manifest = Join-Path $Dir 'xrtape_api_layer.json'
$payload = [ordered]@{
    file_format_version = '1.0.0'
    api_layer = [ordered]@{
        name                   = 'XR_APILAYER_XRTAPE_recorder'
        library_path           = $target
        api_version            = '1.0'
        implementation_version = '1'
        description            = 'xr-tape - records what the application submits to OpenXR'
    }
}
$payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifest -Encoding UTF8

[pscustomobject]@{
    Architecture = $Architecture
    Dll          = $target
    Manifest     = $manifest
    Dir          = $Dir
    LayerName    = 'XR_APILAYER_XRTAPE_recorder'
}
