"""Import the large Hawaiian-electronics sales dataset into the repo.

Copies the 100 per-branch CSVs from an external source directory into
`data/Hawaii_Electronics_100/` so the notebook (`DATASET="large"`), the vector
ingest, and the evaluation harness can all use it. Pure local file copy — no AWS.

Usage:
  python3 scripts/import_sales_data.py
  python3 scripts/import_sales_data.py --src /path/to/Hawaiian_Electronics_100_CSV_Flat

The default source is the location the dataset was delivered to on this workstation.
These are mock CSVs (not secrets); commit them or add data/Hawaii_Electronics_100/ to
.gitignore per your preference — they are ~1.3 MB total.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = Path(
    "/Users/sridharpchome/Documents/Claude/Demo_arbiter_RAG_files/Hawaiian_Electronics_100_CSV_Flat"
)
DEST = REPO_ROOT / "data" / "Hawaii_Electronics_100"


def main() -> int:
    ap = argparse.ArgumentParser(description="Import the 100-branch Hawaii sales dataset")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="source directory of the CSVs")
    ap.add_argument("--dest", default=str(DEST), help="destination directory in the repo")
    args = ap.parse_args()

    src, dest = Path(args.src), Path(args.dest)
    if not src.is_dir():
        print(f"✗ source directory not found: {src}", file=sys.stderr)
        return 1
    csvs = sorted(src.glob("*.csv"))
    if not csvs:
        print(f"✗ no CSV files in {src}", file=sys.stderr)
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    for p in csvs:
        shutil.copy2(p, dest / p.name)
    print(f"✓ copied {len(csvs)} CSV files → {dest}")
    print("  Next: run the notebook with DATASET='large', or ingest with scripts/ingest_sales_vectors.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
