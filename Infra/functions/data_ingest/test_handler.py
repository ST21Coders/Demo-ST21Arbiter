"""Offline tests for the data-ingest worker's job state machine (no AWS).

Mocks S3 download + the DynamoDB jobs table + arbiter_rag.ingest, and asserts the
QUEUED -> RUNNING -> SUCCEEDED/FAILED transitions and job-type routing.

Run:  rag_src/.venv/bin/python Infra/functions/data_ingest/test_handler.py
      (or via pytest)
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
# arbiter_rag must import before handler.py loads it; the worker COPYs it in the image.
sys.path.insert(0, str(_REPO / "rag_src"))
sys.path.insert(0, str(_HERE))

import handler  # noqa: E402


class _FakeJobsTable:
    def __init__(self):
        self.updates = []

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        # Record the flattened field->value the worker set (strip the ':' alias prefix).
        self.updates.append({k[1:]: v for k, v in ExpressionAttributeValues.items()})


def _install(monkey_files: int, ingest_result=None, ingest_error=None):
    """Wire fakes onto the handler module; return the jobs-table recorder + a call log."""
    table = _FakeJobsTable()
    handler.jobs_table = table
    calls = {"unstructured": 0, "tabular": 0}

    def fake_download(bucket, prefix, dest):
        for i in range(monkey_files):
            (Path(dest) / f"{i}.txt").write_text("x")
        return monkey_files

    def fake_unstructured(*a, **k):
        calls["unstructured"] += 1
        if ingest_error:
            raise ingest_error
        return ingest_result or {"documents": 1, "chunks": 3, "vectors": 3}

    def fake_tabular(*a, **k):
        calls["tabular"] += 1
        if ingest_error:
            raise ingest_error
        return ingest_result or {"rows": 2, "facts": 2, "vectors": 2}

    handler._download_prefix = fake_download
    handler.ingest.ingest_unstructured = fake_unstructured
    handler.ingest.ingest_tabular = fake_tabular
    return table, calls


def _event(job_type="docusearch"):
    return {
        "job_id": "job-1", "created_at": "2026-07-14T00:00:00Z", "job_type": job_type,
        "source_bucket": "proc", "source_prefix": "projects/p/g/",
        "vector_bucket": "dev-st21arbiter-poc-docs-vectors", "vector_index": "p-g",
        "dataset_id": "p-g", "grain": None,
    }


def test_docusearch_success():
    table, calls = _install(monkey_files=2)
    out = handler.handler(_event("docusearch"), None)
    assert out["status"] == "SUCCEEDED", out
    assert calls["unstructured"] == 1 and calls["tabular"] == 0
    statuses = [u.get("status") for u in table.updates if "status" in u]
    assert statuses == ["RUNNING", "SUCCEEDED"], statuses
    assert table.updates[-1]["result"]["files"] == 2


def test_structured_routes_to_tabular():
    table, calls = _install(monkey_files=3)
    out = handler.handler(_event("structured_analytics"), None)
    assert out["status"] == "SUCCEEDED", out
    assert calls["tabular"] == 1 and calls["unstructured"] == 0


def test_empty_prefix_succeeds_without_ingest():
    table, calls = _install(monkey_files=0)
    out = handler.handler(_event("docusearch"), None)
    assert out["status"] == "SUCCEEDED" and out["files"] == 0, out
    assert calls["unstructured"] == 0  # no files → ingest not called
    assert table.updates[-1]["result"]["files"] == 0


def test_failure_records_failed():
    table, calls = _install(monkey_files=1, ingest_error=RuntimeError("boom"))
    out = handler.handler(_event("docusearch"), None)
    assert out["status"] == "FAILED", out
    last = table.updates[-1]
    assert last["status"] == "FAILED" and "boom" in last["error"]


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
