# Build the xr-tape API layer for one or both architectures.
#
# The bitness matters more here than it does for most tools: the OpenXR loader
# silently SKIPS a layer whose machine type does not match the host process, and
# a skipped layer produces no trace and no error.
[CmdletBinding()]
param(
    [ValidateSet("all", "x86", "x64")][string]$Architecture = "all",
    [ValidateSet("Debug", "RelWithDebInfo", "Release")][string]$Configuration = "RelWithDebInfo"
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$architectures = if ($Architecture -eq "all") { @("x86", "x64") } else { @($Architecture) }

Push-Location $repo
try {
    foreach ($arch in $architectures) {
        cmake --preset $arch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "CMake configure failed for $arch" }
        cmake --build --preset "$arch-release" --config $Configuration | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "CMake build failed for $arch" }

        $dll = Join-Path $repo "build\$arch-vs\bin\$Configuration\xrtape_layer.dll"
        if (-not (Test-Path -LiteralPath $dll)) { throw "expected output missing: $dll" }
        Write-Host "built $arch -> $dll"
    }
} finally {
    Pop-Location
}
