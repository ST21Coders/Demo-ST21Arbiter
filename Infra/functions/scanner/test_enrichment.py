"""Unit tests for the ownership enrichment (pure, no AWS).

Run: python3 -m unittest Infra.functions.scanner.test_enrichment
 or: cd Infra/functions/scanner && python3 -m unittest test_enrichment
"""
import unittest

from enrichment import enrich_findings, _rule_matches


def _rule(rule_id, priority, match, owner, consumer="", platform="", tags=None):
    return {
        "rule_id": rule_id, "priority": priority, "match": match,
        "owner_team": owner, "consumer_team": consumer,
        "platform_team": platform, "tags": tags or [],
    }


class RuleMatchTests(unittest.TestCase):
    def test_empty_match_is_wildcard(self):
        self.assertTrue(_rule_matches({}, {"rule_key": "UC01"}))

    def test_all_predicates_must_equal(self):
        f = {"rule_key": "UC04", "domain": "NETWORK_SECURITY"}
        self.assertTrue(_rule_matches({"domain": "NETWORK_SECURITY"}, f))
        self.assertTrue(_rule_matches({"rule_key": "UC04", "domain": "NETWORK_SECURITY"}, f))
        self.assertFalse(_rule_matches({"rule_key": "UC05"}, f))
        self.assertFalse(_rule_matches({"domain": "CLOUD_SECURITY"}, f))


class EnrichTests(unittest.TestCase):
    def test_specific_rule_key_beats_domain(self):
        rules = [
            _rule("uc04", 10, {"rule_key": "UC04"}, "platform-security", tags=["network"]),
            _rule("net", 50, {"domain": "NETWORK_SECURITY"}, "network-eng"),
            _rule("default", 999, {}, "unassigned"),
        ]
        f = {"rule_key": "UC04", "domain": "NETWORK_SECURITY"}
        enrich_findings([f], rules)
        self.assertEqual(f["owner_team"], "platform-security")
        self.assertEqual(f["tags"], ["network"])

    def test_priority_order_independent_of_list_order(self):
        # Domain rule listed first but with higher priority number must lose.
        rules = [
            _rule("net", 50, {"domain": "NETWORK_SECURITY"}, "network-eng"),
            _rule("uc04", 10, {"rule_key": "UC04"}, "platform-security"),
        ]
        f = {"rule_key": "UC04", "domain": "NETWORK_SECURITY"}
        enrich_findings([f], rules)
        self.assertEqual(f["owner_team"], "platform-security")

    def test_wildcard_default_catches_unmatched(self):
        rules = [
            _rule("uc01", 10, {"rule_key": "UC01"}, "data-governance"),
            _rule("default", 999, {}, "unassigned", tags=["untriaged"]),
        ]
        f = {"rule_key": "UC99", "domain": "VENDOR_MGMT"}
        enrich_findings([f], rules)
        self.assertEqual(f["owner_team"], "unassigned")
        self.assertEqual(f["tags"], ["untriaged"])

    def test_no_rules_leaves_blank_ownership(self):
        f = {"rule_key": "UC01"}
        enrich_findings([f], [])
        self.assertEqual(f["owner_team"], "")
        self.assertEqual(f["consumer_team"], "")
        self.assertEqual(f["platform_team"], "")
        self.assertEqual(f["tags"], [])

    def test_all_team_axes_stamped(self):
        rules = [_rule("uc01", 10, {"rule_key": "UC01"},
                       "data-governance", "app-dev", "network-eng", ["application", "network"])]
        f = {"rule_key": "UC01"}
        enrich_findings([f], rules)
        self.assertEqual(f["owner_team"], "data-governance")
        self.assertEqual(f["consumer_team"], "app-dev")
        self.assertEqual(f["platform_team"], "network-eng")
        self.assertEqual(f["tags"], ["application", "network"])


if __name__ == "__main__":
    unittest.main()
