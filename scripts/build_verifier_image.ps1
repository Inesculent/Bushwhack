param(
    [string]$ImageName = "verifier-test-env:latest"
)
$ErrorActionPreference = "Stop"
Write-Host "Building verifier test environment image: $ImageName"
docker build --tag $ImageName --file Dockerfile.verifier .
Write-Host "Built $ImageName"
Write-Host ""
Write-Host "Smoke test:"
Write-Host "  docker run --rm $ImageName python --version"
