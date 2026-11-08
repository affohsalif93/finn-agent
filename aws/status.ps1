# FinnDemo: status via SSM Run Command (see ops.py). Extra args are passed through.
uv run --no-project python "$PSScriptRoot\ops.py" status @args
exit $LASTEXITCODE
