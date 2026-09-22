import json
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.chain import SMALLHOLDER_PROTECTED_SHARE, FreshnessChain

CST = timezone(timedelta(hours=8))

ROOT = Path(__file__).parents[1]
EVENTS = json.loads((ROOT / "data" / "sample_chain.json").read_text(encoding="utf-8"))


class ChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.chain = FreshnessChain(EVENTS)

    # ------------------------------------------------------- 溯源：混装/拆箱

    def test_trace_through_mixed_and_split_batches(self) -> None:
        # B-003 = 40kg 取自 B-001（L-001 48% / L-003 52%）+ 20kg 直接取自 L-003
        parts = {p["lot_id"]: round(p["kg"], 3) for p in self.chain.components("processing_batch", "B-003")}
        self.assertAlmostEqual(parts["L-001"], 40 * 48 / 100, places=3)
        self.assertAlmostEqual(parts["L-003"], 40 * 52 / 100 + 20, places=3)

        traced = {t["lot_id"]: t for t in self.chain.trace("processing_batch", "B-003")}
        self.assertEqual(traced["L-001"]["collector_id"], "C-001")
        self.assertEqual(traced["L-001"]["zone_id"], "Z-NORTH")

    def test_repacked_batch_traces_current_composition_and_keeps_history(self) -> None:
        events = [
            *EVENTS,
            {
                "event_id": "evt-batch-001-v2", "event_type": "BATCH_REPACKED",
                "aggregate_type": "processing_batch", "aggregate_id": "B-001",
                "occurred_at": "2026-09-21T05:30:00+08:00", "version": 2,
                "summary": "拆箱重组：B-001 仅保留 L-003 60kg",
                "sources": [{"ref_type": "foraged_lot", "ref_id": "L-003", "quantity_kg": 60}],
            }
        ]
        chain = FreshnessChain(events)
        parts = {p["lot_id"]: p["kg"] for p in chain.components("processing_batch", "B-001")}
        self.assertEqual(set(parts), {"L-003"})
        self.assertEqual(parts["L-003"], 60)
        # 旧构成仍在历史中留档可审计
        self.assertEqual(len(chain.batches["B-001"]["history"]), 1)
        self.assertEqual(len(chain.batches["B-001"]["history"][0]), 2)

    def test_form_change_keeps_source_traceability(self) -> None:
        events = [
            *EVENTS,
            {
                "event_id": "evt-batch-003-v3", "event_type": "BATCH_FORM_CHANGED",
                "aggregate_type": "processing_batch", "aggregate_id": "B-003",
                "occurred_at": "2026-09-22T05:00:00+08:00", "version": 3,
                "summary": "鲜品改冷冻", "product_form": "FROZEN",
            }
        ]
        chain = FreshnessChain(events)
        self.assertEqual(chain.batches["B-003"]["form"], "FROZEN")
        lots = {p["lot_id"] for p in chain.components("processing_batch", "B-003")}
        self.assertEqual(lots, {"L-001", "L-003"})

    # ------------------------------------------------- 冻结只影响相关数量

    def test_partial_hold_freezes_only_affected_quantity(self) -> None:
        self.assertEqual(self.chain.input_total_kg("processing_batch", "B-004"), 30)
        self.assertEqual(self.chain.available_kg("processing_batch", "B-004"), 20)
        reasons = {h["reason"] for h in self.chain.active_holds("processing_batch", "B-004")}
        self.assertEqual(reasons, {"TEMP_EXCURSION"})

    def test_released_species_hold_restores_full_quantity(self) -> None:
        self.assertEqual(self.chain.available_kg("foraged_lot", "L-002"), 30)
        self.assertEqual(self.chain.lots["L-002"]["species_status"], "CONFIRMED")

    def test_temp_excursion_detected(self) -> None:
        self.assertEqual([x["sensor_id"] for x in self.chain.temp_excursions], ["T-77"])

    # --------------------------------------------------- 目的国规则按出运日

    def test_rule_matched_on_shipment_date(self) -> None:
        # 9 月 22 日出运，匹配 9 月版新规则，而不是 6-8 月旧版
        rule = self.chain.rule_for("JP", datetime(2026, 9, 22).date())
        self.assertEqual(rule["rule_version"], "RV-2026-09")
        old_rule = self.chain.rule_for("JP", datetime(2026, 7, 1).date())
        self.assertEqual(old_rule["rule_version"], "RV-2026-06")

    # ----------------------------------------------------------- 证书版本

    def test_certificate_correction_keeps_old_version(self) -> None:
        old, new = self.chain.certificates["C-002"], self.chain.certificates["C-003"]
        self.assertFalse(old["active"])
        self.assertTrue(new["active"])
        self.assertEqual(new["supersedes"], "C-002")

    def test_customs_view_shows_cert_chain_and_physical_match(self) -> None:
        view = self.chain.customs_view("CL-001")
        self.assertEqual(view["inspection_result"], "PASS")
        self.assertEqual(view["rule_effective_on_ship_date"], "RV-2026-09")
        cert = view["certificates"][0]
        self.assertEqual(cert["certificate_no"], "C-2026-002-R1")
        self.assertEqual(cert["version_chain"], ["C-2026-002", "C-2026-002-R1"])
        self.assertEqual(view["samples"], {"S-001": "PASS"})

    # ------------------------------------------------------- 放行缺口

    def test_clean_shipment_has_no_gaps(self) -> None:
        self.assertEqual(self.chain.clearance_gaps("SH-001"), [])

    def test_stale_rule_certificate_is_a_gap(self) -> None:
        events = [
            e for e in EVENTS if e["aggregate_id"] not in {"C-002", "C-003"}
        ] + [
            {
                "event_id": "evt-cert-old", "event_type": "CERTIFICATE_ISSUED",
                "aggregate_type": "certificate", "aggregate_id": "C-OLD",
                "occurred_at": "2026-09-21T11:00:00+08:00", "version": 1,
                "summary": "误挂旧规则版本", "certificate_id": "C-OLD",
                "certificate_no": "C-OLD-1", "destination_country": "JP",
                "rule_version": "RV-2026-06",
                "sources": [{"ref_type": "processing_batch", "ref_id": "B-003", "quantity_kg": 60}],
            }
        ]
        gaps = {g["type"] for g in FreshnessChain(events).clearance_gaps("SH-001")}
        self.assertIn("CERT_RULE_MISMATCH", gaps)

    def test_missing_certificate_is_a_gap(self) -> None:
        events = [e for e in EVENTS if e["aggregate_type"] != "certificate"]
        gaps = {g["type"] for g in FreshnessChain(events).clearance_gaps("SH-001")}
        self.assertIn("CERT_MISSING", gaps)

    def test_downgrade_suggested_for_fresh_window_breach(self) -> None:
        events = [
            e for e in EVENTS
            if not (e["aggregate_type"] == "shipment" and e["event_type"] == "SHIPMENT_DISPATCHED")
        ] + [
            {
                "event_id": "evt-shipment-late", "event_type": "SHIPMENT_DISPATCHED",
                "aggregate_type": "shipment", "aggregate_id": "SH-LATE",
                "occurred_at": "2026-09-24T10:00:00+08:00", "version": 1,
                "summary": "错过 72 小时保鲜窗口才出运",
                "shipment_id": "SH-LATE", "order_id": "O-001", "booking_id": "BK-001",
                "shipped_at": "2026-09-24T10:00:00+08:00",
                "sources": [{"ref_type": "processing_batch", "ref_id": "B-003", "quantity_kg": 60}],
            }
        ]
        chain = FreshnessChain(events)
        gaps = {g["type"] for g in chain.clearance_gaps("SH-LATE")}
        self.assertIn("FRESH_WINDOW_EXCEEDED", gaps)
        forms = {o["product_form"] for o in chain.downgrade_options("SH-LATE")}
        self.assertEqual(forms, {"FROZEN", "DRIED"})

    # ----------------------------------------------------- 订舱公平重排

    def test_ranking_uses_freshness_then_tier(self) -> None:
        candidates = [
            {"booking_id": "BIG", "order_id": "O-BIG", "order_tier": "STANDARD",
             "smallholder": False, "quantity_kg": 100,
             "deadline": datetime(2026, 9, 25), "slot_at": datetime(2026, 9, 23),
             "transport_mode": "AIR"},
            {"booking_id": "SMALL", "order_id": "O-SMALL", "order_tier": "STANDARD",
             "smallholder": True, "quantity_kg": 30,
             "deadline": datetime(2026, 9, 22, 22), "slot_at": datetime(2026, 9, 23, 8),
             "transport_mode": "LAND"},
        ]
        ranking = FreshnessChain.rank_bookings(candidates, capacity_kg=100)
        placed = [r["booking_id"] for r in ranking if r["rank"] is not None]
        self.assertEqual(placed.index("SMALL"), 0)

    def test_smallholder_protected_even_when_loose_freshness_pressure(self) -> None:
        # 大户批次保鲜更紧、数量占满舱位；小农仍受 30% 保护份额
        big = {"booking_id": "BIG", "order_id": "O-BIG", "order_tier": "PREMIUM",
               "smallholder": False, "quantity_kg": 100,
               "deadline": datetime(2026, 9, 22, 20), "slot_at": datetime(2026, 9, 22, 18),
               "transport_mode": "AIR"}
        small = {"booking_id": "SMALL", "order_id": "O-SMALL", "order_tier": "STANDARD",
                 "smallholder": True, "quantity_kg": 30,
                 "deadline": datetime(2026, 9, 23, 8), "slot_at": datetime(2026, 9, 23, 6),
                 "transport_mode": "LAND"}
        ranking = FreshnessChain.rank_bookings([big, small], capacity_kg=100)
        by_id = {r["booking_id"]: r for r in ranking}
        self.assertTrue(by_id["SMALL"]["rank"])
        self.assertTrue(by_id["SMALL"]["protected"])
        # 大户无法整批装入，剩余部分被标记为超舱位
        self.assertIsNone(by_id["BIG"]["rank"])
        self.assertEqual(by_id["BIG"]["reason"], "CAPACITY_EXCEEDED")
        self.assertAlmostEqual(SMALLHOLDER_PROTECTED_SHARE, 0.30)

    def test_sample_booking_candidate_flags_smallholder(self) -> None:
        by_id = {c["booking_id"]: c for c in self.chain.booking_candidates()}
        self.assertTrue(by_id["BK-002"]["smallholder"])   # B-004 全由小农 L-002 构成
        self.assertFalse(by_id["BK-001"]["smallholder"])  # B-003 含庄园货
        # BK-002 预约修订后的时刻已反映
        self.assertEqual(by_id["BK-002"]["slot_at"], datetime(2026, 9, 23, 8, tzinfo=CST))

    # ------------------------------------------------------------- 结算

    def test_settlement_uses_final_accepted_weight_and_reasons(self) -> None:
        settlements = self.chain.compute_settlements(
            grade_price={"A": 700, "B": 500},
            reason_deductions={"PHYSICAL_DAMAGE": 50},
        )
        by_collector = {s["collector_id"]: s for s in settlements}
        smallholder = by_collector["C-001"]
        self.assertAlmostEqual(smallholder["accepted_quantity_kg"], 18.56, places=2)
        self.assertEqual(smallholder["quality_reasons"], ["PHYSICAL_DAMAGE"])
        self.assertAlmostEqual(smallholder["amount_cny"], 18.56 * (700 - 50), places=2)
        estate = by_collector["C-003"]
        self.assertAlmostEqual(estate["accepted_quantity_kg"], 39.44, places=2)

    # ------------------------------------------------------------- 客户视图

    def test_customer_view_hides_collection_points(self) -> None:
        view = self.chain.customer_view("SH-001")
        self.assertEqual(view["origin_public_zones"], ["香格里拉北部产区"])
        rendered = json.dumps(view, ensure_ascii=False)
        for secret in ("C-001", "L-001", "L-003", "Z-NORTH", "收购点"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(view["accepted_quantity_kg"], 58)
        self.assertEqual(view["quality_reasons"], ["PHYSICAL_DAMAGE"])
        # 客户只能看到本出运节点的温控（不含 B-004 的超限记录）
        self.assertEqual([c["stage"] for c in view["cold_chain"]], ["COLD_STORAGE"])
        self.assertTrue(all(c["temp_c"] <= 8 for c in view["cold_chain"]))
        self.assertEqual(view["certificates"][0]["certificate_no"], "C-2026-002-R1")


if __name__ == "__main__":
    unittest.main()
