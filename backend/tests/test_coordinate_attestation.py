import unittest

from app.coordinate_attestation import (
    issue_coordinate_attestation,
    strip_untrusted_coordinate,
    trusted_coordinate_evidence,
)


class CoordinateAttestationTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "title": "喜茶(中街店)",
            "branch_name": "中街店",
            "address": "中街路1号",
            "lat": 41.8012,
            "lng": 123.4521,
            "coordinate_source": "360map_embedded_poi",
            "coordinate_source_url": "https://m.map.360.cn/branch",
            "coordinate_external_id": "poi-1",
            "confidence": 0.88,
            "status": "located",
        }

    def test_signed_coordinate_survives_a_later_turn(self):
        signed = issue_coordinate_attestation(self.candidate, secret="test-secret")

        evidence = trusted_coordinate_evidence(signed, secret="test-secret")

        self.assertIsNotNone(evidence)
        self.assertEqual((evidence["lat"], evidence["lng"]), (41.8012, 123.4521))
        self.assertEqual(evidence["branch_name"], "中街店")

    def test_coordinate_or_evidence_tampering_invalidates_signature(self):
        signed = issue_coordinate_attestation(self.candidate, secret="test-secret")
        moved = {**signed, "lat": 39.9, "lng": 116.4}
        changed_evidence = dict(signed)
        changed_evidence["coordinate_attestation"] = dict(signed["coordinate_attestation"])
        changed_evidence["coordinate_attestation"]["evidence"] = {
            **signed["coordinate_attestation"]["evidence"],
            "branch_name": "大悦城店",
        }

        self.assertIsNone(trusted_coordinate_evidence(moved, secret="test-secret"))
        self.assertIsNone(trusted_coordinate_evidence(changed_evidence, secret="test-secret"))

    def test_unsigned_legacy_coordinate_is_stripped(self):
        result = strip_untrusted_coordinate(self.candidate)

        self.assertNotIn("lat", result)
        self.assertNotIn("lng", result)
        self.assertNotIn("coordinate_source", result)
        self.assertEqual(result["status"], "location_needed")


if __name__ == "__main__":
    unittest.main()
