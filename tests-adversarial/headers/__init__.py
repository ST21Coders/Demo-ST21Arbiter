"""tests-adversarial/headers/ — security headers + transport security mini-layer.

This is the Block-B layer (per docs/security_compliance_coverage.md) covering
checklist items #23 (plaintext transmission), #24 (weak crypto algorithms),
#31 (security headers), #35 (CORS), #55 (CSRF), and #56 (clickjacking).

The layer mirrors the conftest/fixture shape of `auth/` and `fuzz/`: every test
takes a `results_writer` fixture, every emitted row matches
`src/coverage/builder.py::TestResult`, and the writer drains to
`${RUN_DIR}/headers/results.json` at session end.

Classifiers (`classifiers.py`) are pure functions so unit tests can exercise
their logic without hitting the network. Live tests call into them with real
HTTP responses.
"""
