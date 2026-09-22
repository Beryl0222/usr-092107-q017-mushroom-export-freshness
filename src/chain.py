"""野生菌出口鲜度链事件投影。

把只追加的领域事件折叠成可查询状态，支撑：

- 混装、拆箱、改加工形态后回溯原料来源（``trace``）；
- 物种存疑或温度超限时只冻结相关数量（``active_holds``、``available_kg``）；
- 目的国规则按出运日期匹配、证书版本链不覆盖（``clearance_gaps``）；
- 预约变化时按剩余保鲜时长、订单等级与舱位重排，并保护小农批次
  （``rank_bookings``）；
- 企业提前看到放行缺口与降级选择（``clearance_gaps``、``downgrade_options``）；
- 海关核对电子证书与实物批次（``customs_view``）；
- 菌农按最终接收重量与质量原因结算（``compute_settlements``）；
- 海外客户获得可验证但不暴露具体采集点的产地与冷链记录（``customer_view``）。
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

# 小农批次在每次舱位分配中受到保护的最低比例
SMALLHOLDER_PROTECTED_SHARE = 0.30


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _as_date(value: str) -> date:
    return _parse_dt(value).date() if "T" in value else date.fromisoformat(value)


class FreshnessChain:
    def __init__(self, events: list[dict]):
        self.events = sorted(events, key=lambda e: e["occurred_at"])
        self.lots: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}          # hold_id -> 状态
        self.samples: dict[str, dict] = {}
        self.certificates: dict[str, dict] = {}
        self.rules: dict[str, list[dict]] = defaultdict(list)
        self.orders: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}
        self.shipments: dict[str, dict] = {}
        self.clearances: dict[str, list[dict]] = defaultdict(list)
        self.deliveries: list[dict] = []
        self.temp_excursions: list[dict] = []
        self._project()

    # ------------------------------------------------------------------ 折叠

    def _project(self) -> None:
        for e in self.events:
            kind, agg, agg_id = e["event_type"], e["aggregate_type"], e["aggregate_id"]

            if kind == "TEMP_READING_RECORDED":
                lo, hi, t = e.get("temp_min_c"), e.get("temp_max_c"), e["temp_c"]
                if (lo is not None and t < lo) or (hi is not None and t > hi):
                    self.temp_excursions.append(
                        {"sensor_id": e["sensor_id"], "stage": e["temp_stage"],
                         "temp_c": t, "at": _parse_dt(e["occurred_at"]),
                         "target": agg_id}
                    )
                continue

            if kind == "ZONE_PERMIT_RECORDED":
                # 区域许可本身不挂在货物聚合上，需要时按 zone_id 查询事件即可
                continue

            if agg == "foraged_lot":
                lot = self.lots.setdefault(agg_id, {"holds": {}})
                if kind == "LOT_RECEIVED":
                    lot.update(
                        collector_id=e["collector_id"],
                        collector_tier=e["collector_tier"],
                        zone_id=e["zone_id"],
                        zone_public_name=e.get("zone_public_name"),
                        species_code=e["species_code"],
                        received_kg=e["quantity_kg"],
                        received_at=_parse_dt(e["occurred_at"]),
                        accepted_kg=0.0,
                        grade=None,
                    )
                elif kind == "LOT_GRADED":
                    lot["grade"] = e["grade"]
                    lot["accepted_kg"] = float(e["accepted_quantity_kg"])
                    lot["rejected_kg"] = float(e.get("rejected_quantity_kg", 0.0))
                elif kind in ("SPECIES_CONFIRMED", "SPECIES_DISPUTED"):
                    lot["species_code"] = e["species_code"]
                    lot["species_status"] = "CONFIRMED" if kind == "SPECIES_CONFIRMED" else "DISPUTED"
                elif kind == "HOLD_PLACED":
                    lot["holds"][e["hold_id"]] = float(e["quantity_kg"])
                    self.holds[e["hold_id"]] = {"target": agg_id, "kg": float(e["quantity_kg"]),
                                                "reason": e["hold_reason"], "active": True}
                elif kind == "HOLD_RELEASED":
                    lot["holds"].pop(e["hold_id"], None)
                    if e["hold_id"] in self.holds:
                        self.holds[e["hold_id"]]["active"] = False

            elif agg == "processing_batch":
                batch = self.batches.setdefault(
                    agg_id, {"sources": [], "holds": {}, "form": None, "history": []}
                )
                if kind == "BATCH_CREATED":
                    batch["sources"] = e["sources"]
                    batch["form"] = e.get("product_form", "FRESH")
                    batch["created_at"] = _parse_dt(e["occurred_at"])
                elif kind == "BATCH_REPACKED":
                    # 拆箱/混装：以最新来源构成替换，旧构成仍留在 history 中可审计
                    batch["history"].append(batch["sources"])
                    batch["sources"] = e["sources"]
                elif kind == "BATCH_FORM_CHANGED":
                    batch["form"] = e["product_form"]
                elif kind == "HOLD_PLACED":
                    batch["holds"][e["hold_id"]] = float(e["quantity_kg"])
                    self.holds[e["hold_id"]] = {"target": agg_id, "kg": float(e["quantity_kg"], ),
                                                "reason": e["hold_reason"], "active": True}
                elif kind == "HOLD_RELEASED":
                    batch["holds"].pop(e["hold_id"], None)
                    if e["hold_id"] in self.holds:
                        self.holds[e["hold_id"]]["active"] = False

            elif kind == "TEMP_READING_RECORDED":
                lo, hi, t = e.get("temp_min_c"), e.get("temp_max_c"), e["temp_c"]
                if (lo is not None and t < lo) or (hi is not None and t > hi):
                    self.temp_excursions.append(
                        {"sensor_id": e["sensor_id"], "stage": e["temp_stage"],
                         "temp_c": t, "at": _parse_dt(e["occurred_at"]),
                         "target": e.get("aggregate_id")}
                    )

            elif kind == "SAMPLE_TAKEN":
                self.samples[e["sample_id"]] = {"result": "PENDING", "sources": e.get("sources", [])}
            elif kind == "SAMPLE_RESULTED":
                self.samples.setdefault(e["sample_id"], {"sources": []})["result"] = e["sample_result"]

            elif agg == "certificate":
                if kind == "CERTIFICATE_ISSUED":
                    self.certificates[agg_id] = {
                        "certificate_id": agg_id,
                        "certificate_no": e["certificate_no"],
                        "country": e["destination_country"],
                        "rule_version": e["rule_version"],
                        "sources": e.get("sources", []),
                        "supersedes": None,
                        "active": True,
                        "issued_at": _parse_dt(e["occurred_at"]),
                    }
                elif kind == "CERTIFICATE_CORRECTED":
                    old = self.certificates.get(e["supersedes_certificate_id"])
                    if old:
                        old["active"] = False
                    self.certificates[agg_id] = {
                        "certificate_id": agg_id,
                        "certificate_no": e["certificate_no"],
                        "country": e.get("destination_country", old["country"] if old else None),
                        "rule_version": e.get("rule_version", old["rule_version"] if old else None),
                        "sources": e.get("sources", old["sources"] if old else []),
                        "supersedes": e["supersedes_certificate_id"],
                        "active": True,
                        "issued_at": _parse_dt(e["occurred_at"]),
                    }

            elif kind == "DESTINATION_RULE_PUBLISHED":
                self.rules[e["country_code"]].append(
                    {"rule_version": e["rule_version"],
                     "effective_from": _as_date(e["effective_from"]),
                     "effective_to": _as_date(e["effective_to"]) if e.get("effective_to") else None,
                     "requirements": e.get("requirements", {})}
                )

            elif agg == "customer_order" and kind == "ORDER_PLACED":
                self.orders[agg_id] = {
                    "order_id": agg_id, "tier": e["order_tier"],
                    "country": e["destination_country"],
                    "fresh_window_hours": e["fresh_window_hours"],
                }

            elif agg == "transport_booking":
                booking = self.bookings.setdefault(agg_id, {"revisions": 0, "sources": []})
                if kind == "BOOKING_REQUESTED":
                    booking.update(booking_id=agg_id, order_id=e["order_id"],
                                   transport_mode=e["transport_mode"],
                                   slot_at=_parse_dt(e["slot_at"]),
                                   capacity_kg=float(e["capacity_kg"]),
                                   sources=e.get("sources", []),
                                   status="REQUESTED")
                elif kind == "BOOKING_REVISED":
                    booking["slot_at"] = _parse_dt(e["slot_at"])
                    if "capacity_kg" in e:
                        booking["capacity_kg"] = float(e["capacity_kg"])
                    booking["revisions"] += 1
                elif kind == "BOOKING_CONFIRMED":
                    booking["slot_at"] = _parse_dt(e["slot_at"])
                    booking["capacity_kg"] = float(e["capacity_kg"])
                    booking["status"] = "CONFIRMED"

            elif kind == "SHIPMENT_DISPATCHED":
                self.shipments[agg_id] = {
                    "shipment_id": agg_id,
                    "sources": e.get("sources", []),
                    "order_id": e.get("order_id"),
                    "booking_id": e.get("booking_id"),
                    "shipped_at": _parse_dt(e["occurred_at"]),
                }

            elif agg == "customs_clearance":
                row = {"event": kind, "at": _parse_dt(e["occurred_at"])}
                if kind == "CLEARANCE_APPLIED":
                    row.update(shipment_id=e["shipment_id"], border_port=e["border_port"])
                elif kind == "CLEARANCE_INSPECTED":
                    row.update(result=e["inspection_result"])
                self.clearances[agg_id].append(row)

            elif kind == "DELIVERY_ACCEPTED":
                self.deliveries.append(
                    {"delivery_id": agg_id, "shipment_id": e["shipment_id"],
                     "accepted_kg": float(e["accepted_quantity_kg"]),
                     "rejected_kg": float(e.get("rejected_quantity_kg", 0.0)),
                     "quality_reasons": e.get("quality_reasons", []),
                     "at": _parse_dt(e["occurred_at"])}
                )

    # ------------------------------------------------------------- 组成与数量

    def _node(self, ref_type: str, ref_id: str) -> dict | None:
        return self.batches.get(ref_id) if ref_type == "processing_batch" else self.lots.get(ref_id)

    def components(self, ref_type: str, ref_id: str) -> list[dict]:
        """展开节点的原料构成，返回原料批次（foraged_lot）及其在节点中的数量。

        混装（BATCH_CREATED 多来源）、拆箱（BATCH_REPACKED 改变构成）、
        改加工形态（BATCH_FORM_CHANGED，标识不变）都会被正确追溯。
        """
        node = self._node(ref_type, ref_id)
        if node is None:
            return []
        if ref_type == "foraged_lot":
            return [{"lot_id": ref_id, "kg": node.get("accepted_kg", node.get("received_kg", 0.0))}]

        result: dict[str, float] = defaultdict(float)
        for source in node["sources"]:
            for part in self.components(source["ref_type"], source["ref_id"]):
                share = part["kg"]
                total = self.input_total_kg(source["ref_type"], source["ref_id"])
                if total > 0 and source["quantity_kg"] < total:
                    share = part["kg"] * source["quantity_kg"] / total
                result[part["lot_id"]] += share
        return [{"lot_id": lot_id, "kg": kg} for lot_id, kg in result.items()]

    def input_total_kg(self, ref_type: str, ref_id: str) -> float:
        node = self._node(ref_type, ref_id)
        if node is None:
            return 0.0
        if ref_type == "foraged_lot":
            return node.get("accepted_kg", node.get("received_kg", 0.0))
        return sum(float(s["quantity_kg"]) for s in node["sources"])

    def trace(self, ref_type: str, ref_id: str, quantity_kg: float | None = None) -> list[dict]:
        """追溯到采集户与采集区域；quantity_kg 给定时按比例切分数量。"""
        parts = self.components(ref_type, ref_id)
        if quantity_kg is not None and parts:
            total = sum(p["kg"] for p in parts)
            scale = quantity_kg / total if total else 0.0
            parts = [{"lot_id": p["lot_id"], "kg": round(p["kg"] * scale, 3)} for p in parts]
        traced = []
        for p in parts:
            lot = self.lots[p["lot_id"]]
            traced.append(
                {"lot_id": p["lot_id"], "quantity_kg": round(p["kg"], 3),
                 "collector_id": lot["collector_id"], "zone_id": lot["zone_id"],
                 "species_code": lot["species_code"], "grade": lot.get("grade")}
            )
        return traced

    def active_holds(self, ref_type: str, ref_id: str) -> list[dict]:
        node = self._node(ref_type, ref_id)
        if not node:
            return []
        return [self.holds[hold_id] for hold_id in node.get("holds", {}) if self.holds[hold_id]["active"]]

    def available_kg(self, ref_type: str, ref_id: str) -> float:
        """节点当前可用数量：物种存疑或温度超限只冻结相关数量。"""
        total = self.input_total_kg(ref_type, ref_id)
        frozen = sum(h["kg"] for h in self.active_holds(ref_type, ref_id))
        return round(total - frozen, 3)

    # ----------------------------------------------------------- 放行与降级

    def rule_for(self, country: str, on_date: date) -> dict | None:
        """目的国规则按出运日期匹配，而非按登记或查询日期。"""
        candidates = [
            r for r in self.rules.get(country, [])
            if r["effective_from"] <= on_date and (r["effective_to"] is None or on_date <= r["effective_to"])
        ]
        return max(candidates, key=lambda r: r["effective_from"], default=None)

    def _certificates_for_nodes(self, source_nodes: list[dict]) -> list[dict]:
        """按证书列明的实物批次节点与出运实物节点直接比对（海关核对口径）。"""
        shipped = {(s["ref_type"], s["ref_id"]) for s in source_nodes}
        return [
            cert for cert in self.certificates.values()
            if cert["active"]
            and {(s["ref_type"], s["ref_id"]) for s in cert["sources"]} & shipped
        ]

    def _samples_for(self, lot_ids: set[str]) -> list[str]:
        problems = []
        for sample_id, sample in self.samples.items():
            sample_lots = {p["lot_id"] for s in sample["sources"]
                           for p in self.components(s["ref_type"], s["ref_id"])}
            if sample_lots & lot_ids and sample["result"] != "PASS":
                problems.append(f"样本 {sample_id} 状态为 {sample['result']}")
        return problems

    def clearance_gaps(self, shipment_id: str) -> list[dict]:
        """企业在出运前可看到的放行缺口清单。"""
        shipment = self.shipments[shipment_id]
        shipped_date = shipment["shipped_at"].date()
        lot_ids = {p["lot_id"] for s in shipment["sources"]
                   for p in self.components(s["ref_type"], s["ref_id"])}
        shipped_nodes = {(s["ref_type"], s["ref_id"]) for s in shipment["sources"]}
        gaps: list[dict] = []

        country = self._shipment_country(shipment_id)
        rule = self.rule_for(country, shipped_date) if country else None
        if country is None:
            gaps.append({"type": "ORDER_MISSING", "detail": "出运未关联订单与目的国"})
        elif rule is None:
            gaps.append({"type": "RULE_NO_MATCH", "detail": f"{country} 在出运日 {shipped_date} 无有效准入规则"})

        certs = self._certificates_for_nodes(shipment["sources"])
        if not certs:
            gaps.append({"type": "CERT_MISSING", "detail": "实物批次缺少有效电子证书"})
        elif rule and any(c["rule_version"] != rule["rule_version"] for c in certs):
            gaps.append({"type": "CERT_RULE_MISMATCH",
                         "detail": f"证书规则版本与出运日生效版本 {rule['rule_version']} 不一致"})

        certified_nodes = {(s["ref_type"], s["ref_id"])
                           for cert in certs for s in cert["sources"]}
        uncovered_nodes = shipped_nodes - certified_nodes
        if certs and uncovered_nodes:
            gaps.append({"type": "CERT_COVERAGE_GAP",
                         "detail": f"证书未覆盖实物批次：{sorted(uncovered_nodes)}"})

        for problem in self._samples_for(lot_ids):
            gaps.append({"type": "SAMPLE_NOT_PASS", "detail": problem})

        for source in shipment["sources"]:
            for hold in self.active_holds(source["ref_type"], source["ref_id"]):
                gaps.append({"type": "QUANTITY_HOLD",
                             "detail": f"{source['ref_id']} 冻结 {hold['kg']}kg（{hold['reason']}）"})

        for lot_id in lot_ids:
            lot = self.lots[lot_id]
            if lot.get("species_status") == "DISPUTED":
                gaps.append({"type": "SPECIES_DOUBT", "detail": f"原料批次 {lot_id} 物种存疑"})

        window = self._order_fresh_window(shipment_id)
        for source in shipment["sources"]:
            node = self._node(source["ref_type"], source["ref_id"])
            if node and node.get("form") == "FRESH":
                deadline = self._fresh_deadline(source["ref_type"], source["ref_id"], window)
                if deadline and deadline <= shipment["shipped_at"]:
                    gaps.append({"type": "FRESH_WINDOW_EXCEEDED",
                                 "detail": f"{source['ref_id']} 已超出保鲜窗口，须降级加工"})
        return gaps

    def _shipment_country(self, shipment_id: str) -> str | None:
        shipment = self.shipments[shipment_id]
        order = self.orders.get(shipment.get("order_id"))
        return order["country"] if order else None

    def _order_fresh_window(self, shipment_id: str) -> int | None:
        shipment = self.shipments[shipment_id]
        order = self.orders.get(shipment.get("order_id"))
        return order["fresh_window_hours"] if order else None

    def _fresh_deadline(self, ref_type: str, ref_id: str,
                        window_hours: int | None) -> datetime | None:
        """节点的保鲜截止时刻 = 最早原料交接时间 + 订单保鲜窗口。"""
        if window_hours is None:
            return None
        received = [self.lots[p["lot_id"]]["received_at"] for p in self.components(ref_type, ref_id)]
        return min(received) + timedelta(hours=window_hours) if received else None

    def downgrade_options(self, shipment_id: str) -> list[dict]:
        """鲜品无法放行时，可改做的加工形态及其影响。"""
        gaps = self.clearance_gaps(shipment_id)
        blocking = {g["type"] for g in gaps}
        if not blocking:
            return []
        options = []
        for source in self.shipments[shipment_id]["sources"]:
            node = self._node(source["ref_type"], source["ref_id"])
            if node and node.get("form") == "FRESH" and self.available_kg(source["ref_type"], source["ref_id"]) > 0:
                kg = self.available_kg(source["ref_type"], source["ref_id"])
                options.append({"ref_id": source["ref_id"], "product_form": "FROZEN",
                                "quantity_kg": kg, "quality_reason": "FORM_CHANGED",
                                "usable": "SPECIES_DOUBT" not in blocking})
                options.append({"ref_id": source["ref_id"], "product_form": "DRIED",
                                "quantity_kg": kg, "quality_reason": "FORM_CHANGED",
                                "usable": "SPECIES_DOUBT" not in blocking})
        return options

    # -------------------------------------------------------------- 订舱排序

    def booking_candidates(self) -> list[dict]:
        """构造当前所有预约在舱位竞争中的候选视图。

        候选紧迫度取自预约装货批次中最早的原料保鲜截止时刻；
        是否小农批次取决于装货构成中是否含小农（SMALLHOLDER）原料。
        """
        candidates = []
        for booking in self.bookings.values():
            order = self.orders.get(booking["order_id"])
            if order is None:
                continue
            lot_ids = {p["lot_id"] for s in booking.get("sources", [])
                       for p in self.components(s["ref_type"], s["ref_id"])}
            tiers = {self.lots[lid]["collector_tier"] for lid in lot_ids}
            deadlines = [self.lots[lid]["received_at"] + timedelta(hours=order["fresh_window_hours"])
                         for lid in lot_ids]
            # 小农/合作社批次受保护；混入庄园货的预约不享受保护份额
            small_scale = bool(tiers & {"SMALLHOLDER", "COOPERATIVE"}) and "ESTATE" not in tiers
            candidates.append({
                "booking_id": booking["booking_id"],
                "order_id": booking["order_id"],
                "order_tier": order["tier"],
                "smallholder": small_scale,
                "quantity_kg": booking["capacity_kg"],
                "deadline": min(deadlines) if deadlines else booking["slot_at"],
                "slot_at": booking["slot_at"],
                "transport_mode": booking["transport_mode"],
            })
        return candidates

    @staticmethod
    def rank_bookings(candidates: list[dict], capacity_kg: float) -> list[dict]:
        """预约变化后重新排序。

        排序依据：剩余保鲜时长（最接近保鲜截止的优先）、订单等级（同级再看
        PREMIUM），并为小农批次保留 30% 舱位底线：第一轮先按紧迫度安排小农，
        第二轮对所有候选按紧迫度补足。任何小农都不会被更宽松的大户挤出。
        """
        def urgency(c: dict) -> tuple:
            return (c["deadline"], 0 if c["order_tier"] == "PREMIUM" else 1)

        smallholders = sorted([c for c in candidates if c["smallholder"]], key=urgency)
        others = sorted([c for c in candidates if not c["smallholder"]], key=urgency)

        protected_floor = capacity_kg * SMALLHOLDER_PROTECTED_SHARE
        ranking: list[dict] = []
        used = 0.0
        # 第一轮：小农保护份额
        for c in smallholders:
            if used >= protected_floor:
                break
            if used + c["quantity_kg"] <= capacity_kg:
                ranking.append({**c, "rank": len(ranking) + 1, "protected": True})
                used += c["quantity_kg"]

        placed = {r["booking_id"] for r in ranking}
        # 第二轮：所有候选统一按紧迫度竞争剩余舱位
        for c in sorted(candidates, key=urgency):
            if c["booking_id"] in placed:
                continue
            if used + c["quantity_kg"] <= capacity_kg:
                ranking.append({**c, "rank": len(ranking) + 1,
                                "protected": bool(c["smallholder"])})
                used += c["quantity_kg"]
            else:
                ranking.append({**c, "rank": None, "protected": bool(c["smallholder"]),
                                "reason": "CAPACITY_EXCEEDED"})
        return ranking

    # -------------------------------------------------------------- 结算与视图

    def compute_settlements(self, grade_price: dict[str, float],
                            reason_deductions: dict[str, float]) -> list[dict]:
        """按最终接收重量与质量原因与菌农结算。

        grade_price：等级单价；reason_deductions：质量原因扣款（元/kg）。
        数量来自 DELIVERY_ACCEPTED 回溯到原料批次，而非交接时的毛重。
        """
        accepted: dict[str, dict] = defaultdict(lambda: {"kg": 0.0, "reasons": set()})
        for delivery in self.deliveries:
            shipment = self.shipments.get(delivery["shipment_id"])
            if not shipment:
                continue
            total_shipped = sum(float(s["quantity_kg"]) for s in shipment["sources"]) or 1.0
            scale = delivery["accepted_kg"] / total_shipped
            for source in shipment["sources"]:
                for part in self.trace(source["ref_type"], source["ref_id"],
                                       float(source["quantity_kg"]) * scale):
                    row = accepted[part["lot_id"]]
                    row["kg"] += part["quantity_kg"]
                    row["reasons"].update(delivery["quality_reasons"])

        settlements = []
        for lot_id, row in accepted.items():
            lot = self.lots[lot_id]
            unit_price = grade_price.get(lot.get("grade"), 0.0)
            deductions = sum(reason_deductions.get(r, 0.0) for r in row["reasons"])
            settlements.append({
                "collector_id": lot["collector_id"], "lot_id": lot_id,
                "accepted_quantity_kg": round(row["kg"], 3),
                "unit_price_cny": unit_price,
                "deductions_per_kg_cny": deductions,
                "quality_reasons": sorted(row["reasons"]),
                "amount_cny": round(row["kg"] * max(unit_price - deductions, 0.0), 2),
            })
        return sorted(settlements, key=lambda s: s["collector_id"])

    def customs_view(self, clearance_id: str) -> dict:
        """海关核对电子证书、实物批次、样本与规则版本。"""
        rows = self.clearances[clearance_id]
        applied = next(r for r in rows if r["event"] == "CLEARANCE_APPLIED")
        shipment = self.shipments[applied["shipment_id"]]
        lot_ids = {p["lot_id"] for s in shipment["sources"]
                   for p in self.components(s["ref_type"], s["ref_id"])}
        certs = self._certificates_for_nodes(shipment["sources"])
        rule = None
        country = certs[0]["country"] if certs else None
        if country:
            rule = self.rule_for(country, shipment["shipped_at"].date())

        cert_chain = []
        for cert in certs:
            chain = []
            cur = cert
            while cur:
                chain.append(cur["certificate_no"])
                cur = self.certificates.get(cur["supersedes"]) if cur["supersedes"] else None
            cert_chain.append({"certificate_no": cert["certificate_no"],
                               "rule_version": cert["rule_version"],
                               "version_chain": chain[::-1]})

        inspection = next((r for r in rows if r["event"] == "CLEARANCE_INSPECTED"), None)
        scoped_samples = {
            sid: s["result"] for sid, s in self.samples.items()
            if {p["lot_id"] for src in s["sources"]
                for p in self.components(src["ref_type"], src["ref_id"])} & lot_ids
        }
        return {
            "clearance_id": clearance_id,
            "border_port": applied["border_port"],
            "physical_batches": [{"ref_type": s["ref_type"], "ref_id": s["ref_id"],
                                  "quantity_kg": float(s["quantity_kg"])}
                                 for s in shipment["sources"]],
            "certificates": cert_chain,
            "rule_effective_on_ship_date": rule["rule_version"] if rule else None,
            "samples": scoped_samples,
            "active_holds": [{"target": h["target"], "kg": h["kg"], "reason": h["reason"]}
                             for h in self.holds.values() if h["active"]],
            "inspection_result": inspection["result"] if inspection else None,
        }

    def customer_view(self, shipment_id: str) -> dict:
        """海外客户视图：可验证的产地与冷链记录，但不暴露具体采集点与菌农身份。"""
        shipment = self.shipments[shipment_id]
        zones, species, grades = set(), set(), set()
        scoped_nodes = {s["ref_id"] for s in shipment["sources"]}
        for source in shipment["sources"]:
            for part in self.trace(source["ref_type"], source["ref_id"]):
                lot = self.lots[part["lot_id"]]
                if lot.get("zone_public_name"):
                    zones.add(lot["zone_public_name"])
                species.add(lot["species_code"])
                if lot.get("grade"):
                    grades.add(lot["grade"])

        cold_chain = [
            {"stage": e["temp_stage"], "temp_c": e["temp_c"], "at": e["occurred_at"]}
            for e in self.events
            if e["event_type"] == "TEMP_READING_RECORDED" and e["aggregate_id"] in scoped_nodes
        ]
        certs = [
            {"certificate_no": cert["certificate_no"],
             "rule_version": cert["rule_version"],
             "destination_country": cert["country"]}
            for cert in self._certificates_for_nodes(shipment["sources"])
        ]

        delivery = next((d for d in self.deliveries if d["shipment_id"] == shipment_id), None)
        return {
            "shipment_id": shipment_id,
            "origin_public_zones": sorted(zones),   # 仅公开区域名，无坐标/采集点/菌农
            "species": sorted(species),
            "grades": sorted(grades),
            "cold_chain": cold_chain,
            "certificates": certs,
            "accepted_quantity_kg": delivery["accepted_kg"] if delivery else None,
            "quality_reasons": delivery["quality_reasons"] if delivery else None,
        }
