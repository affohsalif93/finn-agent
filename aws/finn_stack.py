"""Stack FinnDemo: one EC2 instance that runs Finn's compose stack (ARCHITECTURE §13.4).

Only outbound connections: no inbound rule, no SSH key; access via SSM Session Manager.
Code: an S3 asset of the repo; its URL lives in the (non-secret) SSM parameter ASSET_URL_PARAM.
Secrets: the SecureString ENV_PARAM is written by `put_env.py`, never by CDK, so no secret value
ever enters CloudFormation; the stack only grants read access to it by name.
Redeploy: a changed asset updates ASSET_URL_PARAM and an SSM Run Command (custom resource) runs
`finn-deploy` on the instance; the instance itself is not replaced.
"""

from pathlib import Path
from typing import Any

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Fn,
    IgnoreMode,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PARAM = "/finn/demo/env"  # SecureString, created by put_env.py
ASSET_URL_PARAM = "/finn/demo/asset-url"  # String, created by this stack
UBUNTU_AMI_PARAM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
)
APP_DIR = "/opt/finn"
LOG_FILE = "/var/log/finn-bootstrap.log"

# Glob patterns (gitignore-like) for the code asset. Secrets, runtime data, caches and
# everything the instance does not need stay out (docs/tests changes then trigger no redeploy).
ASSET_EXCLUDE = [
    ".git",
    ".github",
    ".venv",
    ".idea",
    ".vscode",
    ".claude",
    ".agents",
    ".env*",
    "!.env.example",
    "runs",
    "runs-*",
    "out",
    "data/.cache",
    "tests",
    "docs",
    "scripts",
    "aws",
    "node_modules",
    "__pycache__",
    "*.py[cod]",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "*.log",
    "Aufgabenblatt*.pdf",  # the task sheet, not needed at runtime
    "skills-lock.json",
]

# Run on every deploy (boot and redeploy): fetch code + .env, (re)build and start the default
# compose profile (temporal, finn-worker, finn-poller). Never prints the .env content.
FINN_DEPLOY = f"""#!/bin/bash
set -euo pipefail
exec 9>/var/lock/finn-deploy.lock
flock 9
exec > >(tee -a {LOG_FILE}) 2>&1
log() {{ echo "$(date -Is) finn-deploy: $*"; }}
export PATH=/usr/local/bin:$PATH
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \\
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
export AWS_DEFAULT_REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \\
  http://169.254.169.254/latest/meta-data/placement/region)
ASSET_URL=$(aws ssm get-parameter --name {ASSET_URL_PARAM} \\
  --query Parameter.Value --output text)
log "code asset $ASSET_URL"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
aws s3 cp --only-show-errors "$ASSET_URL" "$TMP/code.zip"
unzip -q "$TMP/code.zip" -d "$TMP/code"
mkdir -p {APP_DIR}
rsync -a --delete --exclude /runs/ --exclude /.env "$TMP/code/" {APP_DIR}/
log "writing {APP_DIR}/.env from SSM {ENV_PARAM}"
(
  umask 077
  aws ssm get-parameter --name {ENV_PARAM} --with-decryption --output json \\
    | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["Parameter"]["Value"])' \\
    > {APP_DIR}/.env.new
)
mv {APP_DIR}/.env.new {APP_DIR}/.env
chmod 600 {APP_DIR}/.env
mkdir -p {APP_DIR}/runs
chown 1000:1000 {APP_DIR}/runs   # uid of the non-root user "finn" in the image
cd {APP_DIR}
log "docker compose up -d --build (default profile)"
docker compose up -d --build temporal finn-worker finn-poller
docker image prune -f >/dev/null
docker compose ps
log "done"
"""

# Runs once at first boot. Ubuntu 24.04 ships the SSM agent (snap); Docker comes from Docker's
# official apt repo (engine + buildx + compose plugins), the AWS CLI from the official installer.
BOOTSTRAP = [
    "set -euo pipefail",
    f"exec > >(tee -a {LOG_FILE}) 2>&1",
    'echo "$(date -Is) bootstrap start"',
    "export DEBIAN_FRONTEND=noninteractive",
    # 2 GB swap: headroom for the image build on a 2 GB instance.
    "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile"
    " && mkswap /swapfile && swapon /swapfile"
    " && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi",
    "apt-get update -q",
    "apt-get install -y -q ca-certificates curl unzip rsync python3",
    "install -m 0755 -d /etc/apt/keyrings",
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc",
    "chmod a+r /etc/apt/keyrings/docker.asc",
    'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]'
    " https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME)"
    ' stable" > /etc/apt/sources.list.d/docker.list',
    "apt-get update -q",
    "apt-get install -y -q docker-ce docker-ce-cli containerd.io"
    " docker-buildx-plugin docker-compose-plugin",
    "systemctl enable --now docker",  # with restart: unless-stopped the stack survives a reboot
    "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip",
    "unzip -q -o /tmp/awscliv2.zip -d /tmp && /tmp/aws/install --update && rm -rf /tmp/aws*",
    "cat > /usr/local/bin/finn-deploy <<'FINN_DEPLOY'\n" + FINN_DEPLOY + "FINN_DEPLOY",
    "chmod 755 /usr/local/bin/finn-deploy",
    "/usr/local/bin/finn-deploy",
    'echo "$(date -Is) bootstrap done"',
]


class FinnDemoStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, pin_ami: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Own minimal VPC (one public subnet, no NAT): no lookup, so `cdk synth` needs no
        # credentials. The instance needs a public IP for outbound traffic; no inbound rule.
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
            restrict_default_security_group=False,  # unused; saves a custom-resource Lambda
        )
        sg = ec2.SecurityGroup(
            self,
            "InstanceSg",
            vpc=vpc,
            description="Finn demo: outbound only, no inbound rules",
            allow_all_outbound=True,
        )

        asset = s3_assets.Asset(
            self,
            "Code",
            path=str(REPO_ROOT),
            exclude=ASSET_EXCLUDE,
            ignore_mode=IgnoreMode.GLOB,
        )
        asset_url = ssm.StringParameter(
            self,
            "AssetUrl",
            parameter_name=ASSET_URL_PARAM,
            string_value=asset.s3_object_url,
            description="S3 URL of the current Finn code asset (not secret)",
        )

        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
            ],
        )
        env_param_arn = self.format_arn(
            service="ssm", resource="parameter", resource_name=ENV_PARAM.lstrip("/")
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[env_param_arn, asset_url.parameter_arn],
            )
        )
        # Objects of the CDK asset bucket only (no List/GetBucket*). Asset keys are content
        # hashes, so a narrower prefix is not possible; the bucket holds only CDK assets.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[asset.bucket.arn_for_objects("*")]
            )
        )

        # Ubuntu 24.04 LTS (Canonical's public SSM parameter). With pin_ami the AMI ID is cached
        # in cdk.context.json, so a new Ubuntu release does not replace the instance on deploy.
        machine_image = ec2.MachineImage.from_ssm_parameter(
            UBUNTU_AMI_PARAM, os=ec2.OperatingSystemType.LINUX, cached_in_context=pin_ami
        )
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(*BOOTSTRAP)

        instance = ec2.Instance(
            self,
            "Instance",
            instance_name="finn-demo",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            associate_public_ip_address=True,
            security_group=sg,
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=machine_image,
            role=role,
            user_data=user_data,
            # User data runs only at first boot: a changed bootstrap needs a new instance.
            # Code changes do not touch the user data (see AssetUrl + Redeploy).
            user_data_causes_replacement=True,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        25,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
        )
        instance.node.add_dependency(asset_url)

        # Redeploy on `cdk deploy`: a new asset hash changes the Comment, so CloudFormation
        # updates this resource, which sends `finn-deploy` via SSM Run Command (async). On create
        # the bootstrap does the first deploy, so the create call only checks the instance.
        run_deploy = cr.AwsSdkCall(
            service="SSM",
            action="sendCommand",
            parameters={
                "DocumentName": "AWS-RunShellScript",
                "InstanceIds": [instance.instance_id],
                "Comment": f"finn redeploy {asset.asset_hash}"[:100],
                "Parameters": {"commands": ["/usr/local/bin/finn-deploy"]},
                "TimeoutSeconds": 1800,
            },
            physical_resource_id=cr.PhysicalResourceId.of("finn-redeploy"),
            output_paths=["Command.CommandId"],
            # A replaced instance may not be registered with SSM yet; its bootstrap deploys.
            ignore_error_codes_matching="InvalidInstanceId",
        )
        check_instance = cr.AwsSdkCall(
            service="SSM",
            action="describeInstanceInformation",
            parameters={"Filters": [{"Key": "InstanceIds", "Values": [instance.instance_id]}]},
            physical_resource_id=cr.PhysicalResourceId.of("finn-redeploy"),
            output_paths=["InstanceInformationList"],
        )
        redeploy = cr.AwsCustomResource(
            self,
            "Redeploy",
            on_create=check_instance,
            on_update=run_deploy,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["ssm:SendCommand"],
                        resources=[
                            self.format_arn(
                                service="ec2",
                                resource="instance",
                                resource_name=instance.instance_id,
                            ),
                            f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}::document/AWS-RunShellScript",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["ssm:DescribeInstanceInformation"], resources=["*"]
                    ),
                ]
            ),
            log_group=logs.LogGroup(
                self,
                "RedeployLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            timeout=Duration.minutes(2),
            install_latest_aws_sdk=False,
        )
        redeploy.node.add_dependency(asset_url)

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(
            self,
            "SsmSessionCommand",
            value=Fn.join(
                "",
                ["aws ssm start-session --region ", Aws.REGION, " --target ", instance.instance_id],
            ),
        )
        CfnOutput(
            self,
            "TemporalUiPortForward",
            value=Fn.join(
                "",
                [
                    "aws ssm start-session --target ",
                    instance.instance_id,
                    " --document-name AWS-StartPortForwardingSession",
                    " --parameters portNumber=8233,localPortNumber=8233 --region ",
                    Aws.REGION,
                ],
            ),
        )
