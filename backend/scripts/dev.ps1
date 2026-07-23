param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"
$BackendRoot = Split-Path -Parent $PSScriptRoot

Push-Location $BackendRoot
try {
    uv run python scripts/dev.py $Command @Args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
