# Runs the Yordamchi personal bot in this terminal (scripts\windows\run.py). Ctrl+C stops it.
# Exit 3 means the runner updated itself from GitHub: start the new one.
Set-Location (Join-Path $PSScriptRoot "..\..")
do {
    uv run python scripts\windows\run.py @args
    $code = $LASTEXITCODE
} while ($code -eq 3)
exit $code
