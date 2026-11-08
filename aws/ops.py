"""Operate the FinnDemo instance via SSM Run Command (no SSH, no inbound port).

    uv run --no-project python aws/ops.py status            compose ps + last poller/worker logs
    uv run --no-project python aws/ops.py logs [SERVICE] [--tail N]
    uv run --no-project python aws/ops.py redeploy          fetch current asset + .env, rebuild
    uv run --no-project python aws/ops.py run "COMMAND"     any shell command (as root, /opt/finn)

Wrappers: aws/status|logs|redeploy(.sh|.ps1). Stdlib only; uses the AWS CLI login.
"""

import argparse
import json
import subprocess
import sys
import time
from typing import Any

STACK = "FinnDemo"
REGION = "eu-central-1"
APP_DIR = "/opt/finn"


def aws(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        ["aws", *args, "--region", REGION, "--output", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def instance_id() -> str:
    stacks = aws("cloudformation", "describe-stacks", "--stack-name", STACK)["Stacks"]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    return str(outputs["InstanceId"])


def run_remote(script: str, timeout_s: int = 600) -> int:
    target = instance_id()
    params = json.dumps({"commands": [f"cd {APP_DIR} 2>/dev/null || true", script]})
    cmd = aws(
        "ssm", "send-command",
        "--instance-ids", target,
        "--document-name", "AWS-RunShellScript",
        "--comment", "finn ops",
        "--timeout-seconds", str(timeout_s),
        "--parameters", params,
    )["Command"]  # fmt: skip
    command_id = cmd["CommandId"]
    deadline = time.monotonic() + timeout_s + 60
    while True:
        time.sleep(2)
        try:
            inv = aws(
                "ssm", "get-command-invocation",
                "--command-id", command_id,
                "--instance-id", target,
            )  # fmt: skip
        except RuntimeError as exc:
            if "InvocationDoesNotExist" in str(exc) and time.monotonic() < deadline:
                continue
            raise
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
        if time.monotonic() > deadline:
            print(f"still running: command {command_id}", file=sys.stderr)
            return 1
    sys.stdout.write(inv.get("StandardOutputContent", ""))
    err = inv.get("StandardErrorContent", "")
    if err:
        sys.stderr.write(err)
    print(f"[{inv['Status']}] {target} command {command_id}", file=sys.stderr)
    return 0 if inv["Status"] == "Success" else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="FinnDemo operations via SSM")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    logs = sub.add_parser("logs")
    logs.add_argument("service", nargs="?", default="")
    logs.add_argument("--tail", type=int, default=100)
    sub.add_parser("redeploy")
    run = sub.add_parser("run")
    run.add_argument("command")
    args = parser.parse_args()

    if args.cmd == "status":
        return run_remote(
            "tail -n 3 /var/log/finn-bootstrap.log; echo; docker compose ps; echo;"
            " docker compose logs --no-color --tail 15 finn-poller finn-worker"
        )
    if args.cmd == "logs":
        return run_remote(f"docker compose logs --no-color --tail {args.tail} {args.service}")
    if args.cmd == "redeploy":
        return run_remote("/usr/local/bin/finn-deploy", timeout_s=1800)
    return run_remote(args.command)


if __name__ == "__main__":
    sys.exit(main())
