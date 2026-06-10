"""Fixture-parity tests for the structured specialist's row→observation mapping.

Proves that zscaler observations derived from CSV rows (as Athena returns them —
all strings) drive the SAME rule-pack conflicts as the master's hardcoded
_seed_zscaler_observations fixtures, and that the registered_exception
type-coercion landmine (UC04) is handled.

Run:  python3 -m unittest test_observations -v   (from this directory)
"""
import os
import sys
import unittest

# Import the pure mapper (no AWS deps) + the rule pack from master_orchestrator.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "master_orchestrator"))

from observations import map_zscaler_rows, as_bool          # noqa: E402
from scan_rule_pack import run_rule_pack                     # noqa: E402


# Minimal SharePoint policy citations needed for the zscaler + paloalto matchers.
SHAREPOINT = [
    {"policy_doc": "MIG-POL-001", "section": "2.1", "text": "Dropbox Business listed as approved."},
    {"policy_doc": "MIG-POL-001", "section": "2.3", "text": "TeamViewer, AnyDesk, BeyondTrust approved."},
    {"policy_doc": "MIG-POL-001", "section": "3",   "text": "exceptions for Marketing, Communications, HR, and Talent Acquisition."},
    {"policy_doc": "MIG-POL-001", "section": "4",   "text": "Chrome, Firefox, Edge, Safari, Brave are permitted."},
    {"policy_doc": "MIG-POL-002", "section": "2.2", "text": "SSL/TLS inspection is mandatory on ALL web traffic."},
    {"policy_doc": "MIG-POL-002", "section": "4.1", "text": "MFA is required for ALL users."},
    {"policy_doc": "MIG-POL-002", "section": "5.1", "text": "Monitoring-only mode is NOT acceptable for IoT external communication."},
    {"policy_doc": "MIG-POL-002", "section": "6",   "text": "Perimeter egress must be default-deny."},
    {"policy_doc": "MIG-POL-003", "section": "2.1", "text": "Authorised actuarial data transfers: Milliman Inc."},
    {"policy_doc": "MIG-POL-003", "section": "4",   "text": "Approved vendor countries: US, India, UK."},
    {"policy_doc": "MIG-POL-005", "section": "5",   "text": "ZTNA restrictions limited to India and US only are non-compliant."},
]
AWSCONFIG = []  # AWS UCs (07/08/09) intentionally excluded — this test targets zscaler/paloalto.
PALOALTO = [
    {"rule_id": "PAN-SEC-EGRESS-ANYANY-ALLOW-001", "action": "ALLOW", "raw": {"action": "allow"}},
    {"rule_id": "PAN-SEC-APP-TOR-ALLOW-022", "action": "ALLOW", "raw": {"action": "allow"}},
]

# The zscaler_rules.csv the demo ships — rows as Athena returns them (strings).
CSV_ROWS = [
    {"rule_id": "ZIA-URLCAT-CLOUD-BLK-042",       "action": "BLOCK",          "registered_exception": "", "category": "Cloud Storage"},
    {"rule_id": "ZIA-APP-CTRL-REMOTE-BLOCK-007",  "action": "BLOCK",          "registered_exception": "", "category": "Remote Access"},
    {"rule_id": "ZIA-APP-CTRL-BROWSER-FF-009",    "action": "BLOCK",          "registered_exception": "", "category": "Browser"},
    {"rule_id": "ZIA-SSL-BYPASS-FIN-DOMAINS",     "action": "BYPASS_INSPECT", "registered_exception": "false", "category": "Finance"},
    {"rule_id": "ZPA-AUTHPOL-ADMIN-MFA-ONLY",     "action": "MFA_REQUIRED",   "registered_exception": "", "category": "Auth"},
    {"rule_id": "ZIA-IOT-MONITOR-ONLY-VLAN-19",   "action": "MONITOR",        "registered_exception": "", "category": "IoT"},
    {"rule_id": "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "action": "BLOCK",          "registered_exception": "", "category": "DLP"},
    {"rule_id": "ZPA-GEO-RESTRICT-INDIA-US-ONLY", "action": "ALLOW",          "registered_exception": "", "category": "Geo"},
    {"rule_id": "ZIA-URLCAT-SOCIAL-BLOCK-ALL",    "action": "BLOCK",          "registered_exception": "", "category": "Social"},
    {"rule_id": "ZIA-URLCAT-ANONYMIZER-BLOCK",    "action": "BLOCK",          "registered_exception": "", "category": "Anonymizer"},
]


def _conflict_keys(zscaler):
    findings = run_rule_pack(SHAREPOINT, zscaler, AWSCONFIG, PALOALTO)
    return sorted({f["rule_key"] for f in findings if not f.get("compliant")})


class ParityTests(unittest.TestCase):
    def test_csv_drives_expected_zscaler_conflicts(self):
        zscaler = map_zscaler_rows(CSV_ROWS)
        keys = _conflict_keys(zscaler)
        # UC01/02/03/04/05/06/10/11/12 (SharePoint+Zscaler) + UC13/UC14 (paloalto).
        for uc in ["UC01", "UC02", "UC03", "UC04", "UC05", "UC06", "UC10", "UC11", "UC12", "UC13", "UC14"]:
            self.assertIn(uc, keys, f"{uc} should fire from CSV-derived observations")

    def test_uc04_fires_when_unregistered(self):
        zscaler = map_zscaler_rows(CSV_ROWS)  # registered_exception='false'
        self.assertIn("UC04", _conflict_keys(zscaler))

    def test_generic_registered_exception_clears_any_conflict(self):
        # Marking a NON-UC04 rule registered_exception=true must clear its conflict
        # too (the universal exception affordance), without affecting others.
        rows = [dict(r) for r in CSV_ROWS]
        for r in rows:
            if r["rule_id"] == "ZPA-AUTHPOL-ADMIN-MFA-ONLY":
                r["registered_exception"] = "true"
        keys = _conflict_keys(map_zscaler_rows(rows))
        self.assertNotIn("UC05", keys, "registered_exception=true on the MFA rule must clear UC05")
        self.assertIn("UC01", keys, "unrelated conflicts must still fire")
        self.assertIn("UC04", keys, "UC04 still fires (its SSL row is unregistered=false)")

    def test_uc04_clears_when_registered(self):
        rows = [dict(r) for r in CSV_ROWS]
        for r in rows:
            if r["rule_id"] == "ZIA-SSL-BYPASS-FIN-DOMAINS":
                r["registered_exception"] = "true"   # the money-shot toggle
        zscaler = map_zscaler_rows(rows)
        self.assertNotIn("UC04", _conflict_keys(zscaler),
                         "UC04 must clear when the SSL bypass is a registered exception")


class CoercionTests(unittest.TestCase):
    def test_string_false_is_false_not_truthy(self):
        # The landmine: Python treats the string 'false' as truthy.
        self.assertIs(as_bool("false"), False)
        self.assertIs(as_bool("true"), True)
        self.assertIsNone(as_bool(""))
        self.assertIsNone(as_bool(None))


if __name__ == "__main__":
    unittest.main()
