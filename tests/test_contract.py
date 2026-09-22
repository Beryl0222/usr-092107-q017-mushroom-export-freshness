import copy
import json
import unittest
from pathlib import Path

from src.validator import validate_event, validate_stream

DATA_DIR = Path(__file__).parents[1] / "data"


def load_chain() -> list[dict]:
    return json.loads((DATA_DIR / "chain_sample.json").read_text(encoding="utf-8"))


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_chain_sample_events_are_valid(self) -> None:
        for record in load_chain():
            with self.subTest(event_id=record["event_id"]):
                self.assertEqual(validate_event(record), [])

    def test_chain_sample_stream_is_valid(self) -> None:
        self.assertEqual(validate_stream(load_chain()), [])


class EventInvariantTest(unittest.TestCase):
    def base_event(self, **overrides) -> dict:
        event = {
            "event_id": "evt-test-1",
            "event_type": "LOT_RECEIVED",
            "aggregate_type": "foraged_lot",
            "aggregate_id": "lot-test",
            "occurred_at": "2026-09-20T06:30:00+08:00",
            "version": 1,
            "summary": "测试事件",
        }
        event.update(overrides)
        return event

    def test_event_must_match_aggregate(self) -> None:
        event = self.base_event(event_type="LOT_RECEIVED", aggregate_type="processing_batch")
        self.assertTrue(any("不能登记在" in error for error in validate_event(event)))

    def test_hold_requires_quantity_and_reason(self) -> None:
        event = self.base_event(event_type="HOLD_PLACED", aggregate_type="processing_batch")
        errors = validate_event(event)
        self.assertTrue(any("quantity" in error for error in errors))
        self.assertTrue(any("hold_reason" in error for error in errors))

    def test_batch_created_requires_inputs(self) -> None:
        event = self.base_event(event_type="BATCH_CREATED", aggregate_type="processing_batch")
        self.assertTrue(any("inputs" in error for error in validate_event(event)))

    def test_certificate_correction_requires_supersedes(self) -> None:
        event = self.base_event(event_type="CERTIFICATE_CORRECTED", aggregate_type="export_clearance")
        self.assertTrue(any("supersedes" in error for error in validate_event(event)))

    def test_customer_visibility_hides_collection_point(self) -> None:
        event = self.base_event(
            event_type="DELIVERY_ACCEPTED",
            aggregate_type="shipment",
            visibility="customer",
            collection_point="格咱乡3号采集点",
        )
        self.assertTrue(any("采集点" in error for error in validate_event(event)))

    def test_internal_visibility_may_keep_collection_point(self) -> None:
        event = self.base_event(collection_point="格咱乡3号采集点")
        self.assertEqual(validate_event(event), [])

    def test_quantity_must_be_positive_with_unit(self) -> None:
        event = self.base_event(quantity={"value": 0, "unit": "kg"})
        self.assertTrue(any("quantity.value" in error for error in validate_event(event)))
        event = self.base_event(quantity={"value": 5, "unit": "吨"})
        self.assertTrue(any("quantity.unit" in error for error in validate_event(event)))

    def test_occurred_at_must_be_iso8601(self) -> None:
        event = self.base_event(occurred_at="2026年9月20日")
        self.assertTrue(any("occurred_at" in error for error in validate_event(event)))


class StreamInvariantTest(unittest.TestCase):
    def test_duplicate_event_id_is_rejected(self) -> None:
        chain = load_chain()
        chain.append(copy.deepcopy(chain[0]))
        self.assertTrue(any("重复登记" in error for error in validate_stream(chain)))

    def test_version_must_increase_within_aggregate(self) -> None:
        chain = load_chain()
        replay = copy.deepcopy(chain[2])
        replay["event_id"] = "evt-test-replay"
        replay["version"] = 1
        chain.append(replay)
        self.assertTrue(any("version 必须递增" in error for error in validate_stream(chain)))

    def test_correction_must_reference_existing_certificate(self) -> None:
        chain = load_chain()
        correction = copy.deepcopy(chain[15])
        correction["event_id"] = "evt-test-correction"
        correction["version"] = 7
        correction["supersedes"] = "evt-不存在"
        chain.append(correction)
        self.assertTrue(any("引用的证书事件不存在" in error for error in validate_stream(chain)))

    def test_correction_must_stay_on_same_clearance(self) -> None:
        chain = load_chain()
        correction = copy.deepcopy(chain[15])
        correction["event_id"] = "evt-test-correction-2"
        correction["aggregate_id"] = "clearance-另一票"
        correction["version"] = 1
        chain.append(correction)
        self.assertTrue(any("同一聚合" in error for error in validate_stream(chain)))


if __name__ == "__main__":
    unittest.main()
