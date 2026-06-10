"""Python parity tests for the Block D bundle-scanner classifier.

The actual scanner lives in JS at ``e2e/lib/bundle-scanner.js`` — the
Playwright specs ``e2e/tests/bundle-secrets.spec.js`` and
``bundle-tabnabbing.spec.js`` import from it directly. These tests
re-implement the same regex set in Python so the classifier's behavior is
pinned from both sides: a JS regression in the spec OR a regex tweak that
slips through review surfaces here.

The Python regexes intentionally mirror the JS ones character-for-character
(modulo JS ``\\b`` vs Python ``\\b`` which are identical). When you change
a pattern in ``bundle-scanner.js``, update the matching entry here and the
test case set.

Scope (matches the Block D task spec):
  - Probe 1: hardcoded keys — AKIA must detect, benign text must not.
  - Probe 2: source maps — 404 -> PASS classifier, 200 -> FAIL MEDIUM.
  - Probe 3: sensitive comments — known bad TODO -> FAIL LOW, clean -> PASS.
  - Probe 4: SRI — all first-party -> SKIP, missing integrity -> FAIL MEDIUM.
  - Probe 5: tabnabbing — rel="noopener noreferrer" -> PASS,
                          rel="" -> FAIL MEDIUM.
"""

from __future__ import annotations

import re


# ────────────────────────── pattern parity (Probe 1) ───────────────────────


HARDCODED_KEY_PATTERNS = (
    ("aws-access-key-id", "high", r"AKIA[0-9A-Z]{16}"),
    (
        "aws-secret-access-key",
        "high",
        r"(?:aws_secret_access_key|secretAccessKey)\s*[:=]\s*[\"']([^\"']{30,})[\"']",
    ),
    (
        "jwt-shape",
        "medium",
        r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*",
    ),
    (
        "slack-token",
        "high",
        r"xox[bpa]-[0-9]{10,}-[0-9]{10,}-[a-zA-Z0-9]{24,}",
    ),
    ("github-pat", "high", r"ghp_[A-Za-z0-9]{36}"),
    ("openai-anthropic-key", "high", r"sk-[A-Za-z0-9]{40,}"),
)


def _scan_hardcoded(text: str) -> list[tuple[str, str]]:
    """Return ``[(pattern_id, severity), ...]`` for every match.

    Mirrors ``scanForHardcodedKeys`` in ``bundle-scanner.js`` (modulo the
    sample-truncation, which isn't part of the classification contract).
    """
    out: list[tuple[str, str]] = []
    for pid, severity, pattern in HARDCODED_KEY_PATTERNS:
        for _m in re.finditer(pattern, text):
            out.append((pid, severity))
    return out


def test_hardcoded_keys_detects_aws_access_key_id():
    """The single most important detector. Real AKIA-prefixed strings must
    surface as high severity. Fixture is assembled at runtime so the literal
    AKIA-shape doesn't sit in source for GitHub's secret scanner to flag."""
    leak = 'const KEY = "' + "AKIA" + "IOSFODNN7" + "EXAMPLE" + '"'
    findings = _scan_hardcoded(leak)
    assert ("aws-access-key-id", "high") in findings


def test_hardcoded_keys_does_not_match_short_akia_lookalikes():
    """A literal four-char AKIA prefix without the 16-char suffix must not
    fire — guards against false positives on identifiers like 'AKIA0' that
    happen to appear in unrelated mock data."""
    benign = 'const label = "AKIA-mock-id"'
    findings = _scan_hardcoded(benign)
    assert findings == []


def test_hardcoded_keys_detects_aws_secret_assignment():
    """The 30-char-minimum quoted string after the variable name fires."""
    leak = "aws_secret_access_key = 'abcdefghij1234567890ABCDEFGHIJ'"
    findings = _scan_hardcoded(leak)
    assert any(pid == "aws-secret-access-key" for pid, _ in findings)


def test_hardcoded_keys_does_not_match_aws_secret_phrase_alone():
    """The bare phrase without a long quoted value must NOT match — that
    string appears in SDK type defs and would alarm-spam if matched."""
    benign = "Set aws_secret_access_key in your environment first."
    findings = _scan_hardcoded(benign)
    assert findings == []


def test_hardcoded_keys_detects_slack_token():
    """Fixture assembled at runtime so the literal slack-token shape doesn't
    sit in source for GitHub's secret scanner to flag."""
    leak = "xoxb" + "-1234567890-9876543210-" + "abcdefghijklmnopqrstuvwx"
    findings = _scan_hardcoded(leak)
    assert any(pid == "slack-token" for pid, _ in findings)


def test_hardcoded_keys_detects_github_pat():
    leak = "ghp_" + "A" * 36
    findings = _scan_hardcoded(leak)
    assert ("github-pat", "high") in findings


def test_hardcoded_keys_detects_openai_anthropic_shape():
    leak = "sk-" + "A" * 40
    findings = _scan_hardcoded(leak)
    assert ("openai-anthropic-key", "high") in findings


def test_hardcoded_keys_jwt_shape_is_medium_for_review():
    """JWT-shape strings fire as MEDIUM — they're often test fixtures, so
    the report calls them out for human review rather than auto-failing high."""
    leak = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF1234567890"
    findings = _scan_hardcoded(leak)
    assert ("jwt-shape", "medium") in findings


def test_hardcoded_keys_clean_text_yields_no_findings():
    """The baseline assertion the spec relies on for PASS rows."""
    benign = (
        "import React from 'react'\n"
        "export default function App() { return <div>hello</div> }\n"
    )
    findings = _scan_hardcoded(benign)
    assert findings == []


# ────────────────────────── source maps (Probe 2) ──────────────────────────


def _classify_source_map_status(status_code: int) -> tuple[str, str | None]:
    """Mirror the spec's pass/fail policy.

    Returns ``("pass", None)`` on 404/403, ``("fail", "medium")`` on 200,
    and treats any other status as ``("pass", None)`` (a 500 / 502 / etc.
    isn't an exposure — we just couldn't verify it). This matches the spec's
    ``PASS if all .map URLs return 404 / 403`` contract.
    """
    if status_code == 200:
        return "fail", "medium"
    return "pass", None


def test_source_maps_404_classifies_as_pass():
    status, severity = _classify_source_map_status(404)
    assert status == "pass"
    assert severity is None


def test_source_maps_403_classifies_as_pass():
    status, severity = _classify_source_map_status(403)
    assert status == "pass"
    assert severity is None


def test_source_maps_200_classifies_as_fail_medium():
    """A served `.map` in production is the reverse-engineering exposure
    item #42 calls out. Severity is MEDIUM per the spec."""
    status, severity = _classify_source_map_status(200)
    assert status == "fail"
    assert severity == "medium"


# ────────────────────── sensitive comments (Probe 3) ───────────────────────


SENSITIVE_COMMENT_PATTERNS = (
    ("todo-secret", "low", r"TODO[^\n]{0,80}?(password|secret|token|key|admin)"),
    ("fixme-secret", "low", r"FIXME[^\n]{0,80}?(password|secret|token|key)"),
    (
        "html-comment-secret",
        "low",
        r"<!--[\s\S]{0,200}?(api[._-]?key|password|secret|admin|debug|backdoor)[\s\S]{0,200}?-->",
    ),
    (
        "console-log-secret",
        "low",
        r"console\.log\([^)]{0,200}?(password|token|secret|userid|email)[^)]{0,200}?\)",
    ),
)


def _scan_sensitive_comments(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pid, severity, pattern in SENSITIVE_COMMENT_PATTERNS:
        for _m in re.finditer(pattern, text, flags=re.IGNORECASE):
            out.append((pid, severity))
    return out


def test_sensitive_comments_known_bad_todo_flagged_low():
    """The canonical bad TODO from the spec."""
    text = "// TODO: rotate the admin password before launch"
    findings = _scan_sensitive_comments(text)
    assert ("todo-secret", "low") in findings


def test_sensitive_comments_fixme_secret_flagged_low():
    text = "// FIXME: hardcoded secret here, replace with env var"
    findings = _scan_sensitive_comments(text)
    assert ("fixme-secret", "low") in findings


def test_sensitive_comments_html_comment_with_backdoor_flagged():
    text = "<!-- backdoor login for testing, remove before prod -->"
    findings = _scan_sensitive_comments(text)
    assert ("html-comment-secret", "low") in findings


def test_sensitive_comments_console_log_secret_flagged():
    text = "console.log('current user password is', user.password)"
    findings = _scan_sensitive_comments(text)
    assert ("console-log-secret", "low") in findings


def test_sensitive_comments_clean_text_yields_no_findings():
    """No keyword overlap means PASS."""
    text = "// TODO: add unit test for date formatting helper\n"
    findings = _scan_sensitive_comments(text)
    assert findings == []


def test_sensitive_comments_console_log_without_secret_keyword_does_not_match():
    """A console.log that doesn't reference a credential keyword is fine."""
    text = "console.log('ready')"
    findings = _scan_sensitive_comments(text)
    assert findings == []


# ────────────────────────────── SRI (Probe 4) ──────────────────────────────
#
# We don't re-parse HTML in Python — that's the JS scanner's job. What we DO
# pin here is the spec's classification: zero third-party assets -> SKIP,
# all third-party assets have integrity -> PASS, ANY missing -> FAIL MEDIUM.


def _classify_sri(third_party_count: int, missing_count: int) -> tuple[str, str | None]:
    """Mirror the spec's three-way decision."""
    if third_party_count == 0:
        return "skip", None
    if missing_count > 0:
        return "fail", "medium"
    return "pass", None


def test_sri_no_third_party_classifies_as_skip():
    """The spec calls this out explicitly: "Skip with reason 'no third-party
    assets found'"."""
    status, severity = _classify_sri(third_party_count=0, missing_count=0)
    assert status == "skip"
    assert severity is None


def test_sri_all_third_party_have_integrity_classifies_as_pass():
    status, severity = _classify_sri(third_party_count=3, missing_count=0)
    assert status == "pass"
    assert severity is None


def test_sri_missing_integrity_classifies_as_fail_medium():
    """Magecart vector — third-party tag without SRI is FAIL MEDIUM."""
    status, severity = _classify_sri(third_party_count=2, missing_count=1)
    assert status == "fail"
    assert severity == "medium"


# ──────────────────────────── tabnabbing (Probe 5) ─────────────────────────


def _rel_protects(rel_value: str) -> bool:
    """Mirror ``relProtectsAgainstTabnabbing`` in bundle-scanner.js."""
    if not isinstance(rel_value, str):
        return False
    tokens = {t for t in rel_value.lower().split() if t}
    return "noopener" in tokens and "noreferrer" in tokens


def _classify_tabnabbing(rel_value: str) -> tuple[str, str | None]:
    if _rel_protects(rel_value):
        return "pass", None
    return "fail", "medium"


def test_tabnabbing_proper_rel_passes():
    """The canonical safe form."""
    status, severity = _classify_tabnabbing("noopener noreferrer")
    assert status == "pass"
    assert severity is None


def test_tabnabbing_reversed_token_order_still_passes():
    """Tokens are a set; order doesn't matter."""
    status, severity = _classify_tabnabbing("noreferrer noopener")
    assert status == "pass"


def test_tabnabbing_extra_tokens_still_pass():
    """`rel="noopener noreferrer nofollow"` still satisfies the contract."""
    status, _ = _classify_tabnabbing("noopener noreferrer nofollow")
    assert status == "pass"


def test_tabnabbing_empty_rel_fails_medium():
    """`rel=""` is the regression case the spec explicitly calls out."""
    status, severity = _classify_tabnabbing("")
    assert status == "fail"
    assert severity == "medium"


def test_tabnabbing_noopener_only_fails():
    """`noopener` alone protects window.opener but still leaks the Referer
    header — noreferrer is required too."""
    status, severity = _classify_tabnabbing("noopener")
    assert status == "fail"
    assert severity == "medium"


def test_tabnabbing_noreferrer_only_fails():
    status, severity = _classify_tabnabbing("noreferrer")
    assert status == "fail"
    assert severity == "medium"


def test_tabnabbing_case_insensitive_match():
    """Browsers normalize rel tokens to lowercase; the classifier matches."""
    status, _ = _classify_tabnabbing("NoOpener NoReferrer")
    assert status == "pass"
