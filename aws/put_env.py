"""Store the local .env as ONE SSM SecureString (/finn/demo/env) in eu-central-1.

    uv run --no-project python aws/put_env.py [--env-file .env]

Never prints values: only the key names, the size and the parameter version. Uses the AWS CLI
(same login as `cdk`). The instance reads the parameter on every (re)deploy; after a change run
`aws/redeploy` to apply it. Standard tier up to 4 KB, Advanced (paid) only above.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PARAM = "/finn/demo/env"
REGION = "eu-central-1"
STANDARD_LIMIT = 4096
REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    args = parser.parse_args()

    text = args.env_file.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    keys = [
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    size = len(text.encode("utf-8"))
    tier = "Standard" if size <= STANDARD_LIMIT else "Advanced"
    print(f"{args.env_file.name}: {len(keys)} keys, {size} bytes -> {PARAM} ({tier}, {REGION})")
    print("keys:", ", ".join(keys))
    if "COMPOSE_PROFILES" in keys:
        print("warning: COMPOSE_PROFILES is set; the instance starts only the default services")

    # The CLI reads the value from a file (file://), so it never appears on a command line.
    fd, tmp = tempfile.mkstemp(prefix="finn-env-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        result = subprocess.run(
            [
                "aws", "ssm", "put-parameter",
                "--name", PARAM,
                "--type", "SecureString",
                "--tier", tier,
                "--overwrite",
                "--value", f"file://{tmp}",
                "--region", REGION,
                "--output", "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )  # fmt: skip
    finally:
        Path(tmp).unlink(missing_ok=True)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    out = json.loads(result.stdout)
    print(f"stored: version {out.get('Version')}, tier {out.get('Tier')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
