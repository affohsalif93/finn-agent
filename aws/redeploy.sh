#!/usr/bin/env bash
# FinnDemo: redeploy via SSM Run Command (see ops.py). Extra args are passed through.
exec uv run --no-project python "$(dirname "$0")/ops.py" redeploy "$@"
