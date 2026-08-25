param(
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Join-Path $repositoryRoot "strathmark\v3\native\optimizer_kernel.rs"
$installedBinary = Join-Path $repositoryRoot "strathmark\v3\native\strathmark_v3_optimizer_kernel.dll"
$temporaryDirectory = Join-Path $repositoryRoot ".tmp\optimizer-kernel-build"
$temporaryBinary = Join-Path $temporaryDirectory "strathmark_v3_optimizer_kernel.dll"

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Optimizer kernel source is missing: $source"
}
New-Item -ItemType Directory -Force -Path $temporaryDirectory | Out-Null

& rustc $source `
    --crate-name strathmark_v3_optimizer_kernel `
    --crate-type cdylib `
    -C opt-level=3 `
    -C codegen-units=1 `
    -C target-cpu=x86-64-v2 `
    -C lto=fat `
    -C panic=abort `
    -C metadata=strathmark-v3-optimizer-kernel-v1 `
    -C link-arg=/Brepro `
    -o $temporaryBinary
if ($LASTEXITCODE -ne 0) {
    throw "Rust optimizer kernel compilation failed with exit code $LASTEXITCODE"
}

$sourceDigest = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
$binaryDigest = (Get-FileHash -LiteralPath $temporaryBinary -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "source_sha256=$sourceDigest"
Write-Output "binary_sha256=$binaryDigest"

if ($Install) {
    Copy-Item -LiteralPath $temporaryBinary -Destination $installedBinary -Force
    Write-Output "installed_binary=$installedBinary"
    Write-Warning "Update optimizer_kernel_manifest.json and rerun the parity suite before committing."
} else {
    Write-Output "candidate_binary=$temporaryBinary"
    Write-Output "Pass -Install only after reviewing the reported artifact identity."
}
