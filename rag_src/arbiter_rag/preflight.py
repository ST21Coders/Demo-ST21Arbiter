"""Preflight checks — run this FIRST in every notebook. Do not skip it.

Verifies, without mutating anything:
  1. boto3 is new enough to expose the s3vectors client
  2. AWS credentials resolve
  3. Region is set and reasonable
  4. The s3vectors client can be constructed and the account is reachable
  5. Bedrock model access for the configured embedding + generation models

Each check returns a (ok, detail) tuple; ``run_preflight`` prints a table and
returns True only if every *required* check passes.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings, get_settings


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _check_boto3_version() -> Check:
    ok = hasattr(boto3, "__version__")
    try:
        boto3.client("s3vectors", region_name="us-east-1")
        return Check("boto3 / s3vectors client", True, f"boto3 {boto3.__version__}")
    except Exception as exc:  # noqa: BLE001 - report any construction failure
        return Check(
            "boto3 / s3vectors client",
            False,
            f"boto3 {getattr(boto3, '__version__', '?')} lacks s3vectors: {exc}. "
            "Upgrade: pip install -U boto3",
        )
    finally:
        del ok


def _check_credentials(session: boto3.Session, settings: Settings) -> Check:
    creds = session.get_credentials()
    if creds is None:
        return Check(
            "AWS credentials",
            False,
            "No credentials in the default chain. Run `aws login` or set AWS_PROFILE.",
        )
    try:
        ident = session.client("sts").get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        return Check("AWS credentials", False, f"STS failed: {exc}")
    account, who = ident["Account"], ident["Arn"].split("/")[-1]
    # Safety guard: if an expected account is configured, refuse to run against any other one.
    if settings.expected_account_id and account != settings.expected_account_id:
        return Check(
            "AWS credentials",
            False,
            f"WRONG ACCOUNT {account} ({who}); expected {settings.expected_account_id}. "
            "Set AWS_PROFILE to the correct profile.",
        )
    return Check("AWS credentials", True, f"account {account} / {who}")


def _check_region(settings: Settings) -> Check:
    if not settings.region:
        return Check("Region", False, "No region set. Export AWS_REGION.")
    return Check("Region", True, settings.region)


def _check_s3vectors(session: boto3.Session, settings: Settings) -> Check:
    try:
        client = session.client("s3vectors", region_name=settings.region)
        client.list_vector_buckets(maxResults=1)
        return Check("S3 Vectors access", True, "list_vector_buckets ok")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied"):
            return Check("S3 Vectors access", False, "Denied — attach s3vectors:ListVectorBuckets")
        return Check("S3 Vectors access", False, f"{code}: {exc}")
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        return Check("S3 Vectors access", False, str(exc))


def _check_bedrock_models(session: boto3.Session, settings: Settings) -> Check:
    try:
        bedrock = session.client("bedrock", region_name=settings.region)
        resp = bedrock.list_foundation_models()
        available = {m["modelId"] for m in resp.get("modelSummaries", [])}
    except (ClientError, BotoCoreError) as exc:
        return Check(
            "Bedrock model access",
            False,
            f"Could not list models: {exc}",
            required=False,
        )
    # Generation id may be an inference-profile ("us.<base>"); check the base id.
    gen_base = settings.generation_model_id.split(".", 1)[-1] if settings.generation_model_id.startswith(
        ("us.", "eu.", "apac.")
    ) else settings.generation_model_id
    missing = [
        mid
        for mid in (settings.embedding_model_id, gen_base)
        if mid not in available
    ]
    if missing:
        return Check(
            "Bedrock model access",
            False,
            f"Not enabled/visible in {settings.region}: {missing}. Enable in Bedrock console > Model access.",
            required=False,
        )
    return Check("Bedrock model access", True, "embedding + generation models visible")


def run_preflight(settings: Settings | None = None, *, verbose: bool = True) -> bool:
    """Run all checks; print a table; return True if every required check passed."""
    settings = settings or get_settings()
    session = boto3.Session(region_name=settings.region)

    checks = [
        _check_boto3_version(),
        _check_credentials(session, settings),
        _check_region(settings),
        _check_s3vectors(session, settings),
        _check_bedrock_models(session, settings),
    ]

    if verbose:
        print(f"Preflight for env='{settings.env}' region='{settings.region}'\n" + "-" * 62)
        for c in checks:
            mark = "PASS" if c.ok else ("WARN" if not c.required else "FAIL")
            print(f"  [{mark:4}] {c.name:26} {c.detail}")
        print("-" * 62)

    required_ok = all(c.ok for c in checks if c.required)
    if verbose:
        print("Preflight:", "READY" if required_ok else "NOT READY (fix FAIL rows above)")
    return required_ok
