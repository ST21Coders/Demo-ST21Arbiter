"""Safety invariants for the adversarial harness (AC25).

The harness must never modify deployed infrastructure, agent source, UI source,
or per-env parameter files during a run. The only AWS-side surface it touches
is Cognito (for IdToken minting via `boto3.client("cognito-idp")`) and the
deployed app's HTTP endpoints. Everything else — CloudFormation, IAM, KMS, EC2,
S3, direct `aws cloudformation ...` shell-outs, and any write to
`Infra/params/dev.json` — is forbidden.

This module scans the harness source tree for forbidden patterns. It is a
trip-wire, not a logic test. If a future contributor adds a tempting shortcut
(e.g. "let me just patch the IAM policy from a fixture"), this test fails the
PR before the contributor can run it once.

Allowed exception: `boto3.client("cognito-idp")` — used by
`src/identity/cognito_auth.py` to fetch demo-user IdTokens.

Spec mapping: AC25 ("the harness does not modify Infra/, agents/, ui/, or any
AWS resource during a run").
"""
from __future__ import annotations

from pathlib import Path


_HARNESS_ROOT = Path(__file__).resolve().parent.parent

# Directories under tests-adversarial/ that hold source code we need to scan.
# `tests/` is included (this very file lives there) but the safety-invariant
# scanner intentionally does NOT skip its own module — the literal patterns
# below appear here as string literals inside _FORBIDDEN_PATTERNS, which the
# scanner treats as quoted occurrences (it matches the raw substring). To keep
# the test self-consistent, this module is whitelisted by basename.
_SCAN_DIRS = ("src", "scripts", "e2e", "fuzz", "auth", "llm", "tests")

# Patterns the harness must NEVER contain. Each is a raw substring match
# against the file contents.
#
# `boto3.client("cognito-idp")` is the ONE permitted boto3 client and is not
# listed here. Any other boto3 client is forbidden.
_FORBIDDEN_PATTERNS = (
    'boto3.client("cloudformation"',
    "boto3.client('cloudformation'",
    'boto3.client("iam"',
    "boto3.client('iam'",
    'boto3.client("kms"',
    "boto3.client('kms'",
    'boto3.client("ec2"',
    "boto3.client('ec2'",
    'boto3.client("s3"',
    "boto3.client('s3'",
    "aws cloudformation ",
)

# Files exempt from the forbidden-pattern scan. The scanner itself defines
# these strings as data, so it would otherwise flag itself. Path is relative
# to `_HARNESS_ROOT`.
_SCANNER_WHITELIST = frozenset(
    {
        "tests/test_safety_invariants.py",
    }
)


def _iter_python_files():
    """Yield (relative_path, content) for every Python file under _SCAN_DIRS."""
    for scan_dir in _SCAN_DIRS:
        root = _HARNESS_ROOT / scan_dir
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            # Skip caches.
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_HARNESS_ROOT).as_posix()
            if rel in _SCANNER_WHITELIST:
                continue
            yield rel, path.read_text(encoding="utf-8")


def test_no_forbidden_boto3_clients_in_harness_sources():
    """No file under the harness may construct a forbidden boto3 client.

    Only `boto3.client("cognito-idp")` is allowed and is used by
    `src/identity/cognito_auth.py`. Any other client (cloudformation, iam,
    kms, ec2, s3, ...) would let a test mutate deployed infrastructure or
    write to a logging bucket. The harness is read-only against AWS apart
    from Cognito token minting.
    """
    offenders: list[tuple[str, str, int]] = []
    for rel, content in _iter_python_files():
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern in content:
                # Capture the 1-indexed line number of the first match for
                # a helpful failure message.
                idx = content.find(pattern)
                line_no = content.count("\n", 0, idx) + 1
                offenders.append((rel, pattern, line_no))

    assert not offenders, (
        "Forbidden infrastructure-mutating pattern(s) found in harness sources. "
        "The harness must never modify deployed AWS resources. "
        "Hits:\n  "
        + "\n  ".join(f"{rel}:{line} contains {pattern!r}" for rel, pattern, line in offenders)
    )


def test_no_writes_to_infra_params_dev_json():
    """No harness file may write to `Infra/params/dev.json`.

    Reading the file is fine (e.g. to look up `ProjectName`). Writing to it
    would retarget infrastructure on the next deploy — a side effect the
    harness must never cause.

    A write-shaped reference is: the literal string `Infra/params/dev.json`
    appearing near an `open(..., "w")` call, a `.write_text(` call, or a
    `json.dump(` call. We approximate by requiring that any file containing
    the literal path string does NOT also contain a write verb in the same
    file.
    """
    write_verbs = ('open(', '.write_text(', '.write(', 'json.dump(')
    offenders: list[str] = []

    for rel, content in _iter_python_files():
        if "Infra/params/dev.json" not in content:
            continue
        # The string is allowed in docstrings/comments. Only flag if the file
        # ALSO contains a write verb (heuristic; sufficient for tripwire).
        if any(verb in content for verb in write_verbs):
            # Confirm the verb actually appears within 200 chars of the path
            # to drop docstring-only hits.
            idx = content.find("Infra/params/dev.json")
            window = content[max(0, idx - 200) : idx + 200]
            if any(verb in window for verb in write_verbs):
                offenders.append(rel)

    assert not offenders, (
        "Harness file(s) appear to write to Infra/params/dev.json, which is "
        "off-limits per CLAUDE.md. Files:\n  " + "\n  ".join(offenders)
    )


def test_no_shell_invocations_of_aws_cloudformation():
    """No harness file may shell out to `aws cloudformation ...`.

    Even read-only CFN calls (e.g. `list-exports`) are off-limits inside the
    harness — they would couple the harness to operator-state files. CFN
    awareness belongs in `Infra/deploy.sh`, not here.
    """
    offenders: list[tuple[str, int]] = []
    for rel, content in _iter_python_files():
        if "aws cloudformation " in content:
            idx = content.find("aws cloudformation ")
            line_no = content.count("\n", 0, idx) + 1
            offenders.append((rel, line_no))

    assert not offenders, (
        "Harness file(s) shell out to `aws cloudformation`, which is "
        "forbidden. Hits:\n  " + "\n  ".join(f"{rel}:{line}" for rel, line in offenders)
    )


def test_scanner_actually_finds_python_files():
    """Guard against the scanner being a no-op (e.g. globs broke).

    If `_iter_python_files()` yields nothing, the three tests above pass
    vacuously and we lose the tripwire. Assert we see a meaningful number
    of files (the harness has dozens of .py files across the scan dirs).
    """
    files = list(_iter_python_files())
    assert len(files) >= 30, (
        f"safety scanner only found {len(files)} Python files; expected >= 30. "
        "Did the _SCAN_DIRS list break, or was the harness moved?"
    )


def test_only_permitted_boto3_client_is_cognito_idp():
    """Exhaustive sweep: every `boto3.client(` call in the harness must be
    against `cognito-idp`.

    This catches any future boto3 client we forgot to put on the forbidden
    list (e.g. `dynamodb`, `lambda`, `apigateway`, ...).
    """
    import re

    # Match boto3.client("X") or boto3.client('X')
    pattern = re.compile(r"""boto3\.client\(\s*['"]([a-z0-9-]+)['"]""")
    offenders: list[tuple[str, str, int]] = []
    for rel, content in _iter_python_files():
        for match in pattern.finditer(content):
            service = match.group(1)
            if service != "cognito-idp":
                line_no = content.count("\n", 0, match.start()) + 1
                offenders.append((rel, service, line_no))

    assert not offenders, (
        "Found boto3.client() call(s) against a service other than `cognito-idp`. "
        "Only Cognito access is permitted. Hits:\n  "
        + "\n  ".join(f"{rel}:{line} uses boto3.client({svc!r})" for rel, svc, line in offenders)
    )
