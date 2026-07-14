"""Seed the Hawaii sales table for the SQL (Athena) path of the Sales_Specialist.

Concatenates the per-branch sales CSVs into one `hawaii_sales.csv`, uploads it to
s3://<env>-<project>-processed/structured/hawaii_sales/hawaii_sales.csv (SSE-KMS), and
starts the existing structured Glue crawler so a `hawaii_sales` table appears in the
`<env>_<project>_structured` Glue database. The agent's query_sales_sql tool (and the
sales_rag_lab notebook with RUN_ATHENA=True) then run read-only Athena over it.

The table name `hawaii_sales` must match GLUE_TABLE on the deployed agent (set by
scripts/deploy_agents.py env_overrides) — it is the SQL allowlist target.

Usage:
  source scripts/.venv/bin/activate        # or: pip install -e rag_src[data]
  AWS_REGION=us-east-1 PROJECT=st21arbiter-poc python3 scripts/seed_sales_structured.py
  #   DATASET=large|sample   (default large)
  # then wait ~1-2 min for the crawler:
  #   aws glue get-crawler --name <env>-<project>-structured-crawler --query 'Crawler.State'
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
_RAG_SRC = REPO_ROOT / "rag_src"
if (_RAG_SRC / "arbiter_rag").is_dir() and str(_RAG_SRC) not in sys.path:
    sys.path.insert(0, str(_RAG_SRC))

from arbiter_rag import loaders  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
PREFIX = f"{ENV}-{PROJECT}"
PROCESSED_BUCKET = f"{PREFIX}-processed"
CRAWLER_NAME = f"{PREFIX}-structured-crawler"
S3_KEY = "structured/hawaii_sales/hawaii_sales.csv"

DATASET = os.environ.get("DATASET", "large").lower()
DATASET_DIR = REPO_ROOT / "data" / ("Hawaii_Electronics_100" if DATASET == "large" else "Hawaii_Sample_Sales")


def main() -> int:
    if not DATASET_DIR.is_dir():
        print(f"✗ dataset dir not found: {DATASET_DIR}", file=sys.stderr)
        return 1
    df = loaders.load_hawaii_sales(DATASET_DIR)
    body = df.to_csv(index=False).encode("utf-8")
    print(f"combined {len(df):,} rows from {DATASET_DIR.name} → {len(body):,} bytes")

    s3 = boto3.client("s3", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)
    try:
        s3.put_object(Bucket=PROCESSED_BUCKET, Key=S3_KEY, Body=body,
                      ContentType="text/csv", ServerSideEncryption="aws:kms")
        print(f"✓ uploaded s3://{PROCESSED_BUCKET}/{S3_KEY}")
    except ClientError as e:
        print(f"✗ upload failed: {e}", file=sys.stderr)
        return 1

    try:
        glue.start_crawler(Name=CRAWLER_NAME)
        print(f"✓ started Glue crawler {CRAWLER_NAME}")
    except glue.exceptions.CrawlerRunningException:
        print(f"✓ Glue crawler {CRAWLER_NAME} already running")
    except ClientError as e:
        print(f"⚠ StartCrawler failed ({e}). Start it manually:", file=sys.stderr)
        print(f"    aws glue start-crawler --name {CRAWLER_NAME} --region {REGION}", file=sys.stderr)
        return 1

    glue_db = f"{ENV}_{PROJECT}_structured".replace("-", "_")
    print()
    print("Next: wait ~1-2 min for the crawler, then confirm the table exists:")
    print(f"  aws glue get-crawler --name {CRAWLER_NAME} --region {REGION} --query 'Crawler.State'")
    print(f"  aws glue get-table --database-name {glue_db} --name hawaii_sales "
          f"--region {REGION} --query 'Table.Name'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
