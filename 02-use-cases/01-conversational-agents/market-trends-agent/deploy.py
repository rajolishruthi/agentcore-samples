#!/usr/bin/env python3
"""
Complete Market Trends Agent Deployment Script
Handles IAM role creation, permissions, direct-code packaging, and agent setup

IAM Role Setup
--------------
The execution role trusts bedrock-agentcore.amazonaws.com with a condition
scoped to Runtime resources in your account:

    "Condition": {
        "StringEquals": {"aws:SourceAccount": "<account-id>"},
        "ArnLike":      {"aws:SourceArn": "arn:aws:bedrock-agentcore:*:<account-id>:runtime/*"}
    }

The permissions policy uses explicit least-privilege statements:

  BedrockModelInvocation     — bedrock:InvokeModel* scoped to foundation models
  DirectCodeArtifactAccess — s3:GetObject scoped to this sample's deployment bucket
  CloudWatch Logs (runtime)  — CreateLogGroup/Stream, PutLogEvents scoped to runtimes/*
  XRay                       — PutTraceSegments, PutTelemetryRecords, GetSamplingRules/Targets
  GetAgentAccessToken        — GetWorkloadAccessToken* scoped to workload identity
  BedrockAgentCoreMemory     — memory CRUD scoped to memory/*
  BedrockAgentCoreBrowser    — browser session ops scoped to browser resources
  SSMParameterAccess         — GetParameter/PutParameter scoped to market-trends-agent/*
  InvokeAgentRuntime         — bedrock-agentcore:InvokeAgentRuntime scoped to runtime/*
                               (required when gateway forwards requests via GATEWAY_IAM_ROLE)
  ABTestAgentCoreResources   — GetGateway, GetGatewayTarget, ListGatewayTargets,
                               CreateGatewayRule, UpdateGatewayRule, GetGatewayRule,
                               DeleteGatewayRule, ListGatewayRules,
                               GetOnlineEvaluationConfig, GetEvaluator,
                               GetConfigurationBundle, GetConfigurationBundleVersion,
                               ListConfigurationBundleVersions
                               scoped to account ARNs with aws:ResourceAccount condition
  ABTestCloudWatchLogs       — CreateLogGroup, CreateLogStream, PutLogEvents,
                               DescribeLogGroups/Streams, DescribeIndexPolicies, PutIndexPolicy,
                               StartQuery, GetQueryResults, StopQuery, FilterLogEvents, GetLogEvents
                               scoped to evaluations/* and runtimes/* log groups
"""

import argparse
import json
import logging
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class MarketTrendsAgentDeployer:
    """Complete deployer for Market Trends Agent"""

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region
        self.iam_client = boto3.client("iam", region_name=region)
        self.ssm_client = boto3.client("ssm", region_name=region)

    def create_execution_role(self, role_name: str) -> str:
        """Create IAM execution role with least-privilege permissions.

        Trust policy: bedrock-agentcore.amazonaws.com, conditioned on
        aws:SourceAccount and aws:SourceArn scoped to runtime/* resources.

        Permissions: explicit statements for runtime, memory, browser, SSM,
        A/B test gateway/eval/bundle reads, and CloudWatch Logs score aggregation.
        See module docstring for the full statement breakdown.
        """

        # Get account ID for trust policy and resource ARNs
        account_id = boto3.client("sts").get_caller_identity()["Account"]

        # Trust policy for Bedrock AgentCore
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {
                            "aws:SourceAccount": account_id,
                        },
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:bedrock-agentcore:*:{account_id}:runtime/*",
                        },
                    },
                }
            ],
        }

        # Comprehensive execution policy with least privilege permissions
        execution_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "BedrockModelInvocation",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    "Resource": [
                        "arn:aws:bedrock:*::foundation-model/*",
                        f"arn:aws:bedrock:{self.region}:{account_id}:*",
                    ],
                },
                {
                    "Sid": "DirectCodeArtifactAccess",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::bedrock-agentcore-code-{account_id}-{self.region}/*"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                    "Resource": [
                        f"arn:aws:logs:{self.region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["logs:DescribeLogGroups"],
                    "Resource": [f"arn:aws:logs:{self.region}:{account_id}:log-group:*"],
                },
                {
                    "Sid": "ManageRuntimeTelemetryPolicy",
                    "Effect": "Allow",
                    "Action": ["logs:PutResourcePolicy"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": [
                        f"arn:aws:logs:{self.region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                        "xray:GetSamplingRules",
                        "xray:GetSamplingTargets",
                    ],
                    "Resource": ["*"],
                },
                {
                    "Effect": "Allow",
                    "Resource": "*",
                    "Action": "cloudwatch:PutMetricData",
                    "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
                },
                {
                    "Sid": "GetAgentAccessToken",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    ],
                    "Resource": [
                        f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:workload-identity-directory/default/workload-identity/market-trends-agent-*",
                    ],
                },
                {
                    "Sid": "BedrockAgentCoreMemoryOperations",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:ListMemories",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:CreateEvent",
                        "bedrock-agentcore:RetrieveMemories",
                        "bedrock-agentcore:GetMemoryStrategies",
                        "bedrock-agentcore:DeleteMemory",
                        "bedrock-agentcore:GetMemory",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                    ],
                    "Resource": [f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:memory/*"],
                },
                {
                    "Sid": "BedrockAgentCoreBrowserOperations",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetBrowserSession",
                        "bedrock-agentcore:StartBrowserSession",
                        "bedrock-agentcore:StopBrowserSession",
                        "bedrock-agentcore:CreateBrowserSession",
                        "bedrock-agentcore:DeleteBrowserSession",
                        "bedrock-agentcore:ConnectBrowserAutomationStream",
                    ],
                    "Resource": [
                        f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:browser-custom/*",
                        "arn:aws:bedrock-agentcore:*:aws:browser/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "ssm:GetParameter",
                        "ssm:PutParameter",
                        "ssm:DeleteParameter",
                    ],
                    "Resource": f"arn:aws:ssm:{self.region}:{account_id}:parameter/bedrock-agentcore/market-trends-agent/*",
                    "Sid": "SSMParameterAccess",
                },
                {
                    "Sid": "InvokeAgentRuntime",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
                    "Resource": [f"arn:aws:bedrock-agentcore:{self.region}:{account_id}:runtime/*"],
                },
                {
                    "Sid": "ABTestAgentCoreResources",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetGateway",
                        "bedrock-agentcore:GetGatewayTarget",
                        "bedrock-agentcore:ListGatewayTargets",
                        "bedrock-agentcore:CreateGatewayRule",
                        "bedrock-agentcore:UpdateGatewayRule",
                        "bedrock-agentcore:GetGatewayRule",
                        "bedrock-agentcore:DeleteGatewayRule",
                        "bedrock-agentcore:ListGatewayRules",
                        "bedrock-agentcore:GetOnlineEvaluationConfig",
                        "bedrock-agentcore:GetEvaluator",
                        "bedrock-agentcore:GetConfigurationBundle",
                        "bedrock-agentcore:GetConfigurationBundleVersion",
                        "bedrock-agentcore:ListConfigurationBundleVersions",
                    ],
                    "Resource": f"arn:aws:bedrock-agentcore:*:{account_id}:*",
                    "Condition": {
                        "StringEquals": {
                            "aws:ResourceAccount": account_id,
                        }
                    },
                },
                {
                    "Sid": "ABTestCloudWatchLogs",
                    "Effect": "Allow",
                    "Action": [
                        "logs:DescribeLogGroups",
                        "logs:DescribeIndexPolicies",
                        "logs:PutIndexPolicy",
                        "logs:StartQuery",
                        "logs:GetQueryResults",
                        "logs:StopQuery",
                        "logs:FilterLogEvents",
                        "logs:GetLogEvents",
                    ],
                    "Resource": [
                        f"arn:aws:logs:*:{account_id}:log-group:/aws/bedrock-agentcore/evaluations/*",
                        f"arn:aws:logs:*:{account_id}:log-group:/aws/bedrock-agentcore/evaluations/*:*",
                        f"arn:aws:logs:*:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
                        f"arn:aws:logs:*:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:*",
                    ],
                },
            ],
        }

        try:
            # Create the role
            logger.info(f"🔐 Creating IAM role: {role_name}")
            role_response = self.iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Execution role for Market Trends Agent with comprehensive permissions",
            )

            # Attach the comprehensive execution policy
            logger.info(f"📋 Attaching comprehensive execution policy to role: {role_name}")
            self.iam_client.put_role_policy(
                RoleName=role_name,
                PolicyName="MarketTrendsAgentComprehensivePolicy",
                PolicyDocument=json.dumps(execution_policy),
            )

            role_arn = str(role_response["Role"]["Arn"])
            logger.info(f"✅ Created IAM role with ARN: {role_arn}")

            # Wait for role to propagate
            logger.info("⏳ Waiting for role to propagate...")
            time.sleep(10)

            return role_arn

        except self.iam_client.exceptions.EntityAlreadyExistsException:
            logger.info(f"📋 IAM role {role_name} already exists, using existing role")

            # Reconcile both trust and permissions for an existing role.
            logger.info("📋 Updating existing role trust and permissions...")
            self.iam_client.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy),
            )
            self.iam_client.put_role_policy(
                RoleName=role_name,
                PolicyName="MarketTrendsAgentComprehensivePolicy",
                PolicyDocument=json.dumps(execution_policy),
            )

            role_response = self.iam_client.get_role(RoleName=role_name)
            return str(role_response["Role"]["Arn"])

        except Exception as e:
            logger.error(f"❌ Failed to create IAM role: {e}")
            raise

    def create_agentcore_memory(self) -> str:
        """Create AgentCore Memory and store ARN in SSM Parameter Store"""
        try:
            from bedrock_agentcore.memory import MemoryClient
            from bedrock_agentcore.memory.constants import StrategyType

            memory_name = "MarketTrendsAgentMultiStrategy"
            memory_client = MemoryClient(region_name=self.region)

            # Check if memory ARN already exists in SSM
            param_name = "/bedrock-agentcore/market-trends-agent/memory-arn"
            try:
                response = self.ssm_client.get_parameter(Name=param_name)
                existing_memory_arn = str(response["Parameter"]["Value"])
                logger.info(f"✅ Found existing memory ARN in SSM: {existing_memory_arn}")
                return existing_memory_arn
            except self.ssm_client.exceptions.ParameterNotFound:
                logger.info("No existing memory ARN found in SSM, creating new memory...")

            # Check if memory exists by name
            try:
                memories = memory_client.list_memories()
                for memory in memories:
                    if memory.get("name") == memory_name and memory.get("status") == "ACTIVE":
                        memory_arn = str(memory["arn"])
                        logger.info(f"✅ Found existing active memory: {memory_arn}")

                        # Store in SSM for future use
                        self.ssm_client.put_parameter(
                            Name=param_name,
                            Value=memory_arn,
                            Type="String",
                            Overwrite=True,
                            Description="Memory ARN for Market Trends Agent",
                        )
                        logger.info("💾 Stored existing memory ARN in SSM")
                        return memory_arn
            except Exception as error:  # noqa: BLE001 - discovery failure falls back to create
                logger.warning("Error checking existing memories: %s", error)

            # Create new memory
            logger.info("🧠 Creating new AgentCore Memory...")

            strategies = [
                {
                    StrategyType.USER_PREFERENCE.value: {
                        "name": "BrokerPreferences",
                        "description": "Captures broker preferences, risk tolerance, and investment styles",
                        "namespaces": ["market-trends/broker/{actorId}/preferences"],
                    }
                },
                {
                    StrategyType.SEMANTIC.value: {
                        "name": "MarketTrendsSemantic",
                        "description": "Stores financial facts, market analysis, and investment insights",
                        "namespaces": ["market-trends/broker/{actorId}/semantic"],
                    }
                },
            ]

            memory = memory_client.create_memory_and_wait(
                name=memory_name,
                description="Market Trends Agent with multi-strategy memory for broker financial interests",
                strategies=strategies,
                event_expiry_days=90,
                max_wait=300,
                poll_interval=10,
            )

            memory_arn = str(memory["arn"])
            logger.info(f"✅ Memory created successfully: {memory_arn}")

            # Store memory ARN in SSM Parameter Store
            self.ssm_client.put_parameter(
                Name=param_name,
                Value=memory_arn,
                Type="String",
                Overwrite=True,
                Description="Memory ARN for Market Trends Agent",
            )
            logger.info("💾 Memory ARN stored in SSM Parameter Store")

            return memory_arn

        except Exception as e:
            logger.error(f"❌ Failed to create memory: {e}")
            raise

    def _build_direct_code_package(
        self,
        entrypoint: str,
        requirements_file: str,
    ) -> Path:
        """Build a Python 3.13 ARM64 package for AgentCore direct deploy."""
        build_dir = Path(".langgraph-agent-build")
        package_dir = build_dir / "package"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        package_dir.mkdir(parents=True)

        command = [
            "uv",
            "pip",
            "install",
            "--python-platform",
            "aarch64-manylinux2014",
            "--python-version",
            "3.13",
            "--target",
            str(package_dir),
            "--only-binary=:all:",
            "--no-compile",
            "-r",
            requirements_file,
        ]
        logger.info("Installing ARM64 dependencies for direct code deployment...")
        subprocess.run(command, check=True)

        shutil.copy2(entrypoint, package_dir / Path(entrypoint).name)
        shutil.copytree("tools", package_dir / "tools", dirs_exist_ok=True)
        shutil.copytree("skills", package_dir / "skills", dirs_exist_ok=True)

        # Console scripts installed into a target directory inherit the build
        # interpreter's absolute shebang. AgentCore does not have that local
        # virtualenv path, so make the OTEL launcher portable before archiving.
        otel_launcher = package_dir / "bin" / "opentelemetry-instrument"
        if not otel_launcher.is_file():
            raise RuntimeError("ARM64 package is missing bin/opentelemetry-instrument")
        launcher_lines = otel_launcher.read_text(encoding="utf-8").splitlines()
        if not launcher_lines:
            raise RuntimeError("bin/opentelemetry-instrument is empty")
        launcher_lines[0] = "#!/usr/bin/env python3"
        otel_launcher.write_text("\n".join(launcher_lines) + "\n", encoding="utf-8")
        otel_launcher.chmod(0o755)

        for cache_dir in package_dir.rglob("__pycache__"):
            shutil.rmtree(cache_dir)
        for bytecode_file in package_dir.rglob("*.py[co]"):
            bytecode_file.unlink()

        files = [path for path in package_dir.rglob("*") if path.is_file()]
        uncompressed_size = sum(path.stat().st_size for path in files)
        if uncompressed_size > 750 * 1024 * 1024:
            raise RuntimeError("Direct-code package exceeds the 750 MB uncompressed limit")

        archive = build_dir / "market_trends_langgraph_agent.zip"
        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as package:
            for path in files:
                package.write(path, path.relative_to(package_dir))

        if archive.stat().st_size > 250 * 1024 * 1024:
            raise RuntimeError("Direct-code package exceeds the 250 MB compressed limit")
        logger.info(
            "Built direct-code package: %s (%.1f MB compressed, %.1f MB uncompressed)",
            archive,
            archive.stat().st_size / (1024 * 1024),
            uncompressed_size / (1024 * 1024),
        )
        return archive

    def _upload_direct_code_package(
        self,
        agent_name: str,
        archive: Path,
    ) -> tuple[str, str]:
        """Create a private artifact bucket if needed and upload the package."""
        account_id = boto3.client("sts").get_caller_identity()["Account"]
        bucket = f"bedrock-agentcore-code-{account_id}-{self.region}"
        key = f"{agent_name}/{archive.name}"
        s3 = boto3.client("s3", region_name=self.region)

        try:
            s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account_id)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            create_args: dict[str, Any] = {"Bucket": bucket}
            if self.region != "us-east-1":
                create_args["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            s3.create_bucket(**create_args)
            s3.put_public_access_block(
                Bucket=bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            s3.put_bucket_encryption(
                Bucket=bucket,
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )

        with archive.open("rb") as package:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=package,
                ExpectedBucketOwner=account_id,
                ServerSideEncryption="AES256",
            )
        logger.info("Uploaded direct-code package to s3://%s/%s", bucket, key)
        return bucket, key

    def _wait_until_ready(
        self,
        control: Any,
        runtime_id: str,
    ) -> dict[str, Any]:
        """Wait for a runtime create or update to reach READY."""
        terminal_statuses = {"CREATE_FAILED", "UPDATE_FAILED", "FAILED"}
        for _ in range(40):
            runtime: dict[str, Any] = control.get_agent_runtime(agentRuntimeId=runtime_id)
            status = runtime.get("status")
            logger.info("Runtime status: %s", status)
            if status == "READY":
                return runtime
            if status in terminal_statuses:
                raise RuntimeError(runtime.get("failureReason", f"Runtime reached {status}"))
            time.sleep(15)
        raise TimeoutError("Runtime did not reach READY within 10 minutes")

    def _ensure_runtime(
        self,
        agent_name: str,
        execution_role_arn: str,
        s3_bucket: str,
        s3_key: str,
        entrypoint: str,
    ) -> dict[str, Any]:
        """Create or update a direct-code runtime with unified telemetry."""
        control = boto3.client("bedrock-agentcore-control", region_name=self.region)
        artifact = {
            "codeConfiguration": {
                "code": {"s3": {"bucket": s3_bucket, "prefix": s3_key}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["opentelemetry-instrument", Path(entrypoint).name],
            }
        }
        environment = {
            "AWS_REGION": self.region,
            "UNIFIED_TRACES_DESTINATION_ENABLED": "true",
        }
        lifecycle = {
            "idleRuntimeSessionTimeout": 300,
            "maxLifetime": 1800,
        }

        paginator = control.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for runtime in page.get("agentRuntimes", []):
                if runtime.get("agentRuntimeName") != agent_name:
                    continue
                runtime_id = runtime["agentRuntimeId"]
                logger.info("Updating existing runtime: %s", runtime_id)
                control.update_agent_runtime(
                    agentRuntimeId=runtime_id,
                    agentRuntimeArtifact=artifact,
                    roleArn=execution_role_arn,
                    networkConfiguration={"networkMode": "PUBLIC"},
                    protocolConfiguration={"serverProtocol": "HTTP"},
                    lifecycleConfiguration=lifecycle,
                    environmentVariables=environment,
                )
                return self._wait_until_ready(control, runtime_id)

        logger.info("Creating new AgentCore runtime: %s", agent_name)
        created = control.create_agent_runtime(
            agentRuntimeName=agent_name,
            agentRuntimeArtifact=artifact,
            roleArn=execution_role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "HTTP"},
            lifecycleConfiguration=lifecycle,
            environmentVariables=environment,
        )
        return self._wait_until_ready(control, created["agentRuntimeId"])

    def deploy_agent(
        self,
        agent_name: str,
        role_name: str = "MarketTrendsAgentRole",
        entrypoint: str = "market_trends_agent.py",
        requirements_file: str = "requirements-langgraph.txt",
    ) -> str | None:
        """Package and deploy the LangGraph Market Trends Agent."""
        try:
            logger.info("Starting Market Trends Agent direct-code deployment")
            logger.info("  Agent Name : %s", agent_name)
            logger.info("  Region     : %s", self.region)
            logger.info("  Entrypoint : %s", entrypoint)

            memory_arn = self.create_agentcore_memory()
            execution_role_arn = self.create_execution_role(role_name)
            archive = self._build_direct_code_package(entrypoint, requirements_file)
            s3_bucket, s3_key = self._upload_direct_code_package(
                agent_name,
                archive,
            )
            runtime = self._ensure_runtime(
                agent_name,
                execution_role_arn,
                s3_bucket,
                s3_key,
                entrypoint,
            )
            runtime_arn = str(runtime["agentRuntimeArn"])
            agent_id = str(runtime["agentRuntimeId"])
            runtime_name = str(runtime["agentRuntimeName"])
            log_group = f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"

            arn_file = Path(".agent_arn")
            arn_file.write_text(runtime_arn, encoding="utf-8")
            config_file = Path("langgraph_skill_agent_config.json")
            config_file.write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "agent_arn": runtime_arn,
                        "cw_log_group": log_group,
                        "service_name": f"{runtime_name}.DEFAULT",
                        "region": self.region,
                        "role_name": role_name,
                        "policy_name": "MarketTrendsAgentComprehensivePolicy",
                        "s3_bucket": s3_bucket,
                        "s3_key": s3_key,
                        "memory_arn": memory_arn,
                        "skills": [
                            "earnings-snapshot",
                            "portfolio-risk",
                            "sector-rotation",
                            "trend-analysis",
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            logger.info("Market Trends Agent deployed successfully")
            logger.info("  Runtime ARN : %s", runtime_arn)
            logger.info("  Memory ARN  : %s", memory_arn)
            logger.info("  Exec Role   : %s", execution_role_arn)
            logger.info("  Eval config : %s", config_file)
            logger.info("  CW Logs     : %s", log_group)
            logger.info(
                "  Evaluate    : uv run python evaluators/scripts/evaluate_skills.py "
                "--config %s --results "
                "evaluators/results/langgraph_skill_evaluation_results.json",
                config_file,
            )
            return runtime_arn
        except Exception:
            logger.exception("Deployment failed")
            return None


def check_prerequisites(requirements_file: str) -> bool:
    """Check local files, uv, and AWS credentials before deployment."""
    required_files = [
        "market_trends_agent.py",
        requirements_file,
        "tools/browser_tool.py",
        "tools/broker_card_tools.py",
        "tools/memory_tools.py",
        "tools/skill_tools.py",
        "tools/__init__.py",
        "skills/trend-analysis/SKILL.md",
        "skills/sector-rotation/SKILL.md",
        "skills/earnings-snapshot/SKILL.md",
        "skills/portfolio-risk/SKILL.md",
    ]
    missing_files = [file for file in required_files if not Path(file).is_file()]
    if missing_files:
        logger.error("Missing required files: %s", missing_files)
        return False
    if shutil.which("uv") is None:
        logger.error("uv is required to build the ARM64 deployment package")
        return False
    try:
        identity = boto3.client("sts").get_caller_identity()
    except Exception as error:  # noqa: BLE001
        logger.error("AWS credentials are not configured: %s", error)
        return False
    logger.info("AWS credentials configured for account %s", identity["Account"])
    logger.info("Direct-code deployment does not require Docker or CodeBuild")
    return True


def main() -> int:
    """Deploy the LangGraph Market Trends Agent."""
    parser = argparse.ArgumentParser(description="Deploy Market Trends Agent to Amazon Bedrock AgentCore Runtime")
    parser.add_argument(
        "--agent-name",
        default="market_trends_langgraph_skills",
        help="Runtime name (default: market_trends_langgraph_skills)",
    )
    parser.add_argument(
        "--role-name",
        default="MarketTrendsLangGraphSkillAgentRole",
        help="IAM role name (default: MarketTrendsLangGraphSkillAgentRole)",
    )
    parser.add_argument(
        "--requirements-file",
        default="requirements-langgraph.txt",
        help="Pinned direct-code dependency file",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip prerequisite checks",
    )
    args = parser.parse_args()

    if not args.skip_checks and not check_prerequisites(args.requirements_file):
        logger.error("Prerequisites not met. Fix issues above or use --skip-checks")
        return 1

    deployer = MarketTrendsAgentDeployer(region=args.region)
    runtime_arn = deployer.deploy_agent(
        agent_name=args.agent_name,
        role_name=args.role_name,
        requirements_file=args.requirements_file,
    )
    if not runtime_arn:
        return 1
    logger.info("Deployment completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
