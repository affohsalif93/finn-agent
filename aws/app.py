"""CDK app: stack FinnDemo in eu-central-1 (Frankfurt). Run from aws/ via cdk.json."""

import os

import aws_cdk as cdk
from finn_stack import FinnDemoStack

app = cdk.App()
# With credentials, CDK_DEFAULT_ACCOUNT is set: the AMI ID is then cached in cdk.context.json
# (stable across deploys). Without credentials, `cdk synth` still works (AMI resolved at deploy).
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
FinnDemoStack(
    app,
    "FinnDemo",
    env=cdk.Environment(account=account, region="eu-central-1"),
    pin_ami=account is not None,
    description="Finn demo: EC2 t3.small running the compose stack (outbound only, SSM access)",
)
app.synth()
