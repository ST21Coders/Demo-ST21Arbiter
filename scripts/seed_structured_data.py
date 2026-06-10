"""Seed the STRUCTURED ingestion demo: a zscaler_rules.csv catalogued for Athena.

Writes s3://<env>-<project>-processed/structured/zscaler_rules/zscaler_rules.csv
(SSE-KMS) and starts the Glue crawler so the structured_specialist can query it
via Athena. The master then pulls Zscaler enforcement observations live from
Athena instead of fixtures (STRUCTURED_RUNTIME_ARN must be set on the master).

The rule_ids match the scanner's canonical fixtures (agents/master_orchestrator/
agent.py::_seed_zscaler_observations) so the existing matchers fire unchanged.
The SSL-bypass row ships with registered_exception=TRUE (a registered, compliant
exception) → UC04 stays clear. Because the FIXTURES have it false (UC04 fires),
UC04 being ABSENT after a scan proves the data came from Athena, not fixtures.
Money shot: flip it to false, re-upload, re-crawl, re-scan → UC04 (CRITICAL PCI)
appears, sourced from a live SQL query.

Usage:
  source scripts/.venv/bin/activate
  PROJECT=st21arbiter-poc python3 seed_structured_data.py
  # then: wait ~1-2 min for the crawler, then run a scan (UI "Run AI Scan").
"""
from __future__ import annotations

import csv
import io
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION  = os.environ.get("AWS_REGION", "us-east-1")
ENV     = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")

PREFIX = f"{ENV}-{PROJECT}"
PROCESSED_BUCKET = f"{PREFIX}-processed"
CRAWLER_NAME = f"{PREFIX}-structured-crawler"
S3_KEY = "structured/zscaler_rules/zscaler_rules.csv"

s3 = boto3.client("s3", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)

# (rule_id, action, registered_exception, category). Columns map 1:1 to
# observations.map_zscaler_rows. Only the SSL-bypass row uses registered_exception.
ROWS = [
    ("ZIA-URLCAT-CLOUD-BLK-042",       "BLOCK",          "",      "Cloud Storage"),
    ("ZIA-APP-CTRL-REMOTE-BLOCK-007",  "BLOCK",          "",      "Remote Access"),
    ("ZIA-APP-CTRL-BROWSER-FF-009",    "BLOCK",          "",      "Browser"),
    ("ZIA-SSL-BYPASS-FIN-DOMAINS",     "BYPASS_INSPECT", "true",  "Finance"),  # ← money-shot toggle (true=compliant; flip to false → UC04 fires)
    ("ZPA-AUTHPOL-ADMIN-MFA-ONLY",     "MFA_REQUIRED",   "",      "Auth"),
    ("ZIA-IOT-MONITOR-ONLY-VLAN-19",   "MONITOR",        "",      "IoT"),
    ("ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "BLOCK",          "",      "DLP"),
    ("ZPA-GEO-RESTRICT-INDIA-US-ONLY", "ALLOW",          "",      "Geo"),
    ("ZIA-URLCAT-SOCIAL-BLOCK-ALL",    "BLOCK",          "",      "Social"),
    ("ZIA-URLCAT-ANONYMIZER-BLOCK",    "BLOCK",          "",      "Anonymizer"),
]


def build_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rule_id", "action", "registered_exception", "category"])
    w.writerows(ROWS)
    return buf.getvalue().encode("utf-8")


def main() -> int:
    body = build_csv()
    try:
        s3.put_object(
            Bucket=PROCESSED_BUCKET, Key=S3_KEY, Body=body,
            ContentType="text/csv", ServerSideEncryption="aws:kms",
        )
        print(f"  ✓ uploaded s3://{PROCESSED_BUCKET}/{S3_KEY} ({len(ROWS)} rows)")
    except ClientError as e:
        print(f"  ✗ upload failed: {e}", file=sys.stderr)
        return 1

    try:
        glue.start_crawler(Name=CRAWLER_NAME)
        print(f"  ✓ started Glue crawler {CRAWLER_NAME}")
    except glue.exceptions.CrawlerRunningException:
        print(f"  ✓ Glue crawler {CRAWLER_NAME} already running")
    except ClientError as e:
        print(f"  ⚠ StartCrawler failed ({e}). Start it manually:", file=sys.stderr)
        print(f"      aws glue start-crawler --name {CRAWLER_NAME} --region {REGION}", file=sys.stderr)
        return 1

    print()
    print("Next: wait ~1-2 min for the crawler to finish, then run a scan.")
    print(f"  aws glue get-crawler --name {CRAWLER_NAME} --region {REGION} --query 'Crawler.State'")
    print("  (State READY = done) → UI 'Run AI Scan' or invoke the scanner Lambda.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
