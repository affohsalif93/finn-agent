# FinnDemo: Temporal UI via SSM port forwarding (see ops.py). Extra args are passed through.
uv run --no-project python "$PSScriptRoot\ops.py" tunnel @args
exit $LASTEXITCODE
