"""Assertions on the synthesized FinnDemo template (no AWS credentials needed)."""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from finn_stack import ASSET_URL_PARAM, ENV_PARAM, FinnDemoStack


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> tuple[Template, Path]:
    outdir = tmp_path_factory.mktemp("cdk.out")
    app = cdk.App(outdir=str(outdir))
    stack = FinnDemoStack(app, "FinnDemo", env=cdk.Environment(region="eu-central-1"))
    template = Template.from_stack(stack)
    app.synth()
    return template, outdir


@pytest.fixture(scope="module")
def template(synth: tuple[Template, Path]) -> Template:
    return synth[0]


def _flatten(value: object) -> str:
    """Render a CloudFormation value (strings, Fn::Join, Fn::Base64) as plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        if "Fn::Join" in value:
            sep, parts = value["Fn::Join"]
            return sep.join(_flatten(p) for p in parts)
        if "Ref" in value:
            return f"<{value['Ref']}>"
        if "Fn::Base64" in value:
            return _flatten(value["Fn::Base64"])
        return json.dumps(value)
    return str(value)


def _user_data(template: Template) -> str:
    lts = template.find_resources("AWS::EC2::LaunchTemplate")
    instances = template.find_resources("AWS::EC2::Instance")
    (instance,) = instances.values()
    props = instance["Properties"]
    if "UserData" in props:
        return _flatten(props["UserData"])
    (lt,) = lts.values()
    return _flatten(lt["Properties"]["LaunchTemplateData"]["UserData"])


def test_instance_type_volume_and_public_ip(template: Template) -> None:
    template.resource_count_is("AWS::EC2::Instance", 1)
    template.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "InstanceType": "t3.small",
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": 25,
                        "VolumeType": "gp3",
                        "Encrypted": True,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            "NetworkInterfaces": [Match.object_like({"AssociatePublicIpAddress": True})],
        },
    )
    template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_imdsv2_required(template: Template) -> None:
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {"LaunchTemplateData": {"MetadataOptions": {"HttpTokens": "required"}}},
    )


def test_security_group_has_no_inbound_rules(template: Template) -> None:
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 0)
    for sg in template.find_resources("AWS::EC2::SecurityGroup").values():
        assert "SecurityGroupIngress" not in sg["Properties"]
        assert sg["Properties"]["SecurityGroupEgress"][0]["CidrIp"] == "0.0.0.0/0"


def test_instance_role_is_minimal(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {"Statement": [Match.object_like({"Principal": {"Service": "ec2.amazonaws.com"}})]}
            ),
            "ManagedPolicyArns": [
                Match.object_like(
                    {
                        "Fn::Join": [
                            "",
                            Match.array_with([":iam::aws:policy/AmazonSSMManagedInstanceCore"]),
                        ]
                    }
                )
            ],
        },
    )
    policies = template.find_resources("AWS::IAM::Policy")
    instance_policy = next(p for name, p in policies.items() if name.startswith("InstanceRole"))
    statements = instance_policy["Properties"]["PolicyDocument"]["Statement"]
    actions = sorted(s["Action"] for s in statements)
    assert actions == ["s3:GetObject", "ssm:GetParameter"]
    ssm_stmt = next(s for s in statements if s["Action"] == "ssm:GetParameter")
    resources = _flatten(ssm_stmt["Resource"])
    assert f":parameter{ENV_PARAM}" in resources
    assert "*" not in resources
    s3_stmt = next(s for s in statements if s["Action"] == "s3:GetObject")
    assert _flatten(s3_stmt["Resource"]).endswith("/*")
    assert "cdk-hnb659fds-assets" in _flatten(s3_stmt["Resource"])


def test_no_secret_parameter_in_template(template: Template) -> None:
    params = template.find_resources("AWS::SSM::Parameter")
    names = [p["Properties"]["Name"] for p in params.values()]
    assert names == [ASSET_URL_PARAM]  # the SecureString is created by put_env.py, not by CDK
    assert all(p["Properties"]["Type"] == "String" for p in params.values())


def test_user_data_steps(template: Template) -> None:
    ud = _user_data(template)
    expected_in_order = [
        "/var/log/finn-bootstrap.log",
        "docker-ce",
        "docker-buildx-plugin docker-compose-plugin",
        "systemctl enable --now docker",
        "awscli-exe-linux-x86_64.zip",
        "cat > /usr/local/bin/finn-deploy",
        f"aws ssm get-parameter --name {ASSET_URL_PARAM}",
        "aws s3 cp",
        f"aws ssm get-parameter --name {ENV_PARAM} --with-decryption",
        "chmod 600 /opt/finn/.env",
        "docker compose up -d --build temporal finn-worker finn-poller",
        "/usr/local/bin/finn-deploy\n",
        "bootstrap done",
    ]
    pos = 0
    for step in expected_in_order:
        found = ud.find(step, pos)
        assert found >= 0, f"missing or out of order: {step!r}"
        pos = found
    assert "testmail" not in ud
    assert "set -x" not in ud  # would echo the .env content into the log


def test_redeploy_runs_ssm_command_on_update(template: Template) -> None:
    (res,) = template.find_resources("Custom::AWS").values()
    update = json.loads(_flatten(res["Properties"]["Update"]))
    assert update["service"] == "SSM"
    assert update["action"] == "sendCommand"
    assert update["parameters"]["Parameters"] == {"commands": ["/usr/local/bin/finn-deploy"]}
    assert update["parameters"]["Comment"].startswith("finn redeploy ")
    create = json.loads(_flatten(res["Properties"]["Create"]))
    assert create["action"] == "describeInstanceInformation"


def test_outputs(template: Template) -> None:
    outputs = template.find_outputs("*")
    assert set(outputs) == {"InstanceId", "SsmSessionCommand", "TemporalUiPortForward"}
    pf = _flatten(outputs["TemporalUiPortForward"]["Value"])
    assert "AWS-StartPortForwardingSession" in pf
    assert "portNumber=8233,localPortNumber=8233" in pf


def test_nothing_is_retained(template: Template) -> None:
    for name, res in template.to_json()["Resources"].items():
        assert res.get("DeletionPolicy", "Delete") == "Delete", name


def test_asset_has_code_and_no_secrets(synth: tuple[Template, Path]) -> None:
    _, outdir = synth
    code_dirs = [d for d in outdir.glob("asset.*") if (d / "pyproject.toml").is_file()]
    assert len(code_dirs) == 1
    root = code_dirs[0]
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    for required in [
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "src/finn/cli.py",
        "material/Wirtschaftsplan_2027.xlsx",
        "data/contract_profiles/V1.json",
    ]:
        assert required in files, required
    env_files = sorted(f for f in files if f.rsplit("/", 1)[-1].startswith(".env"))
    assert env_files == [".env.example"]
    forbidden_prefixes = (".git/", ".venv/", "runs/", "runs-testmail/", "out/", "aws/", "tests/")
    assert not [f for f in files if f.startswith(forbidden_prefixes)]
    assert not [f for f in files if "__pycache__" in f or ("/." in f and "cache" in f)]
