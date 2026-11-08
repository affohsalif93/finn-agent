#!/usr/bin/env bash
# FinnDemo: Temporal UI via SSM port forwarding (see ops.py). Extra args are passed through.
exec uv run --no-project python "$(dirname "$0")/ops.py" tunnel "$@"
