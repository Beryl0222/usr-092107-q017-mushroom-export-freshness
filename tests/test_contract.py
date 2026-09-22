import json
import unittest
from pathlib import Path

from src.validator import validate_event, validate_stream

ROOT = Path(__file__).parents[1]


def load(name: str) -> list[dict] | dict:
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


class EnvelopeTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        self.assertEqual(validate_event(load("sample.json")), [])

    def test_full_chain_stream_is_valid(self) -> None:
        self.assertEqual(validate_stream(load("sample_chain.json")), [])

    def test_unknown_event_type_rejected(self) -> None:
        sample = dict(load("sample.json"), event_type="NOT_A_THING")
        self.assertTrue(validate_event(sample))


class StreamRuleTest(unittest.TestCase):
    BASE = {
        "event_id": "e1", "aggregate_type": "foraged_lot", "aggregate_id": "L",
        "occurred_at": "2026-09-20T18:00:00+08:00", "version": 1, "summary": "x",
    }

    def _chain(self, *events: dict) -> list[dict]:
        return list(events)

    def test_duplicate_event_id_rejected(self) -> None:
        e1 = dict(self.BASE, event_type="LOT_RECEIVED",
                  collector_id="C", collector_tier="SMALLHOLDER", zone_id="Z",
                  species_code="MATSU", quantity_kg=1)
        e2 = dict(e1, version=2, event_type="LOT_GRADED", grade="A",
                  accepted_quantity_kg=1, occurred_at="2026-09-20T19:00:00+08:00")
        errors = validate_stream(self._chain(e1, e2))
        self.assertTrue(any("event_id 重复" in m for m in errors))

    def test_duplicate_idempotency_key_rejected(self) -> None:
        e1 = dict(self.BASE, event_id="a", idempotency_key="k", event_type="LOT_RECEIVED",
                  collector_id="C", collector_tier="SMALLHOLDER", zone_id="Z",
                  species_code="MATSU", quantity_kg=1)
        e2 = dict(e1, event_id="b", aggregate_id="L2", idempotency_key="k")
        errors = validate_stream(self._chain(e1, e2))
        self.assertTrue(any("idempotency_key 重复" in m for m in errors))

    def test_version_must_be_consecutive_per_aggregate(self) -> None:
        e1 = dict(self.BASE, event_type="LOT_RECEIVED",
                  collector_id="C", collector_tier="SMALLHOLDER", zone_id="Z",
                  species_code="MATSU", quantity_kg=1)
        e2 = dict(e1, event_id="e2", version=3, event_type="LOT_GRADED", grade="A",
                  accepted_quantity_kg=1, occurred_at="2026-09-20T19:00:00+08:00")
        errors = validate_stream(self._chain(e1, e2))
        self.assertTrue(any("连续递增" in m for m in errors))

    def test_certificate_correction_must_reference_old_and_use_new_id(self) -> None:
        issued = dict(self.BASE, event_id="c1", aggregate_type="certificate",
                      aggregate_id="C-1", event_type="CERTIFICATE_ISSUED",
                      certificate_id="C-1", certificate_no="N-1",
                      destination_country="JP", rule_version="RV-1")
        bad = dict(issued, event_id="c2", aggregate_id="C-2",
                   event_type="CERTIFICATE_CORRECTED", certificate_id="C-1",
                   certificate_no="N-2", supersedes_certificate_id="C-1",
                   occurred_at="2026-09-20T20:00:00+08:00")
        errors = validate_stream(self._chain(issued, bad))
        self.assertTrue(any("不得覆盖旧版" in m for m in errors))

    def test_correction_without_supersedes_rejected(self) -> None:
        issued = dict(self.BASE, event_id="c1", aggregate_type="certificate",
                      aggregate_id="C-1", event_type="CERTIFICATE_ISSUED",
                      certificate_id="C-1", certificate_no="N-1",
                      destination_country="JP", rule_version="RV-1")
        bad = dict(issued, event_id="c2", aggregate_id="C-2",
                   event_type="CERTIFICATE_CORRECTED", certificate_id="C-2",
                   certificate_no="N-2",
                   occurred_at="2026-09-20T20:00:00+08:00")
        errors = validate_stream(self._chain(issued, bad))
        self.assertTrue(any("必须通过 supersedes_certificate_id 引用旧证" in m for m in errors))


if __name__ == "__main__":
    unittest.main()
