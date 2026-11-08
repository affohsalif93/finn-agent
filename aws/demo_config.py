"""Names shared by the CDK app and the stdlib-only helper scripts (no CDK import here)."""

STACK = "FinnDemo"
REGION = "eu-central-1"  # Frankfurt
ENV_PARAM = "/finn/demo/env"  # SecureString with the .env, written by put_env.py, never by CDK
ASSET_URL_PARAM = "/finn/demo/asset-url"  # String, written by the stack
APP_DIR = "/opt/finn"  # Finn's directory on the instance
