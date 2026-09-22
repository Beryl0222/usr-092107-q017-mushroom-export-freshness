"""校验领域事件信封的基础字段与关键不变量。

validate_event 校验单条记录；validate_stream 校验一组记录之间的
跨记录不变量（event_id 去重、版本单调递增、证书更正链接）。
"""
from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = {
    "PERMIT_REGISTERED",
    "LOT_RECEIVED",
    "SPECIES_CONFIRMED",
    "LOT_GRADED",
    "HOLD_PLACED",
    "HOLD_RELEASED",
    "BATCH_CREATED",
    "FORM_CHANGED",
    "TEMPERATURE_LOGGED",
    "SAMPLE_TESTED",
    "RULE_PUBLISHED",
    "CLEARANCE_FILED",
    "RULE_MATCHED",
    "CERTIFICATE_ISSUED",
    "CERTIFICATE_CORRECTED",
    "BOOKING_RESERVED",
    "BOOKING_REVISED",
    "CUSTOMS_CHECKED",
    "CLEARANCE_RELEASED",
    "SHIPMENT_DISPATCHED",
    "DELIVERY_ACCEPTED",
    "SETTLEMENT_RECORDED",
}

AGGREGATE_TYPES = {
    "collection_permit",
    "foraged_lot",
    "processing_batch",
    "shipment",
    "export_clearance",
    "transport_booking",
    "destination_rule",
    "settlement",
}

# 每类事件允许登记的聚合，防止张冠李戴。
EVENT_AGGREGATES = {
    "PERMIT_REGISTERED": {"collection_permit"},
    "LOT_RECEIVED": {"foraged_lot"},
    "SPECIES_CONFIRMED": {"foraged_lot"},
    "LOT_GRADED": {"foraged_lot"},
    "HOLD_PLACED": {"foraged_lot", "processing_batch"},
    "HOLD_RELEASED": {"foraged_lot", "processing_batch"},
    "BATCH_CREATED": {"processing_batch"},
    "FORM_CHANGED": {"processing_batch"},
    "TEMPERATURE_LOGGED": {"processing_batch", "shipment"},
    "SAMPLE_TESTED": {"processing_batch"},
    "RULE_PUBLISHED": {"destination_rule"},
    "CLEARANCE_FILED": {"export_clearance"},
    "RULE_MATCHED": {"export_clearance"},
    "CERTIFICATE_ISSUED": {"export_clearance"},
    "CERTIFICATE_CORRECTED": {"export_clearance"},
    "BOOKING_RESERVED": {"transport_booking"},
    "BOOKING_REVISED": {"transport_booking"},
    "CUSTOMS_CHECKED": {"export_clearance"},
    "CLEARANCE_RELEASED": {"export_clearance"},
    "SHIPMENT_DISPATCHED": {"shipment"},
    "DELIVERY_ACCEPTED": {"shipment"},
    "SETTLEMENT_RECORDED": {"settlement"},
}

# 各类事件必须携带的补充字段。
EVENT_REQUIRED_FIELDS = {
    "HOLD_PLACED": ("quantity", "hold_reason"),
    "HOLD_RELEASED": ("quantity", "hold_reason"),
    "BATCH_CREATED": ("inputs",),
    "FORM_CHANGED": ("quantity", "from_form", "to_form"),
    "CERTIFICATE_CORRECTED": ("supersedes",),
    "RULE_MATCHED": ("rule_id", "shipment_date"),
    "SETTLEMENT_RECORDED": ("quantity",),
}

HOLD_REASONS = {"species_doubt", "temperature_breach", "document_gap", "other"}
PRODUCT_FORMS = {"fresh", "frozen", "dried"}
VISIBILITIES = {"internal", "customs", "customer"}
QUANTITY_UNITS = {"kg", "box", "piece"}
CERTIFICATE_EVENTS = {"CERTIFICATE_ISSUED", "CERTIFICATE_CORRECTED"}


def _validate_quantity(quantity: object) -> list[str]:
    if not isinstance(quantity, dict):
        return ["quantity 必须是对象，包含 value 与 unit"]
    errors = []
    value = quantity.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        errors.append("quantity.value 必须是正数")
    if quantity.get("unit") not in QUANTITY_UNITS:
        errors.append(f"quantity.unit 必须是 {sorted(QUANTITY_UNITS)} 之一")
    return errors


def validate_event(record: dict) -> list[str]:
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if errors:
        return errors

    event_type = record["event_type"]
    aggregate_type = record["aggregate_type"]

    if not isinstance(record["event_id"], str) or not record["event_id"]:
        errors.append("event_id 必须是非空字符串")
    if event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    if aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")
    elif event_type in EVENT_AGGREGATES and aggregate_type not in EVENT_AGGREGATES[event_type]:
        errors.append(f"{event_type} 不能登记在 {aggregate_type} 上")

    if not isinstance(record["version"], int) or isinstance(record["version"], bool) or record["version"] < 1:
        errors.append("version 必须是正整数")

    occurred_at = record["occurred_at"]
    if not isinstance(occurred_at, str):
        errors.append("occurred_at 必须是 ISO 8601 字符串")
    else:
        try:
            datetime.fromisoformat(occurred_at)
        except ValueError:
            errors.append(f"occurred_at 不是合法的 ISO 8601 时间：{occurred_at}")

    for field in EVENT_REQUIRED_FIELDS.get(event_type, ()):
        if field not in record:
            errors.append(f"{event_type} 缺少字段：{field}")

    if "quantity" in record:
        errors.extend(_validate_quantity(record["quantity"]))
    if "hold_reason" in record and record["hold_reason"] not in HOLD_REASONS:
        errors.append(f"hold_reason 必须是 {sorted(HOLD_REASONS)} 之一")
    for field in ("from_form", "to_form"):
        if field in record and record[field] not in PRODUCT_FORMS:
            errors.append(f"{field} 必须是 {sorted(PRODUCT_FORMS)} 之一")

    if "inputs" in record:
        inputs = record["inputs"]
        if not isinstance(inputs, list) or not inputs:
            errors.append("inputs 必须是非空数组，记录加工批次的投入来源")
        else:
            for item in inputs:
                if not isinstance(item, dict) or not item.get("aggregate_type") or not item.get("aggregate_id"):
                    errors.append("inputs 的每一项都必须包含 aggregate_type 与 aggregate_id")
                    break

    visibility = record.get("visibility", "internal")
    if visibility not in VISIBILITIES:
        errors.append(f"visibility 必须是 {sorted(VISIBILITIES)} 之一")
    elif visibility == "customer" and "collection_point" in record:
        errors.append("customer 级记录不得包含具体采集点 collection_point")

    return errors


def validate_stream(records: list[dict]) -> list[str]:
    """校验事件序列的跨记录不变量。

    - event_id 全局唯一，重复登记即拒绝（多系统防重）；
    - 同一聚合内 version 单调递增，记录不得原地改写；
    - 证书更正必须引用同一聚合上此前签发的证书，旧版保留。
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    last_version: dict[tuple[str, str], int] = {}
    certificate_events: dict[str, dict] = {}

    for record in records:
        label = record.get("event_id", "<无 event_id>")
        errors.extend(f"{label}: {error}" for error in validate_event(record))

        event_id = record.get("event_id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen_ids:
                errors.append(f"{event_id}: event_id 重复登记")
            seen_ids.add(event_id)

        key = (record.get("aggregate_type"), record.get("aggregate_id"))
        version = record.get("version")
        if isinstance(version, int) and not isinstance(version, bool) and all(key):
            if key in last_version and version <= last_version[key]:
                errors.append(f"{label}: 同一聚合的 version 必须递增（当前 {version}，已有 {last_version[key]}）")
            last_version[key] = max(version, last_version.get(key, 0))

        if record.get("event_type") == "CERTIFICATE_CORRECTED":
            supersedes = record.get("supersedes")
            if supersedes:
                original = certificate_events.get(supersedes)
                if original is None:
                    errors.append(f"{label}: supersedes 引用的证书事件不存在：{supersedes}")
                elif original.get("aggregate_id") != record.get("aggregate_id"):
                    errors.append(f"{label}: 更正必须与被更正证书属于同一聚合")

        if record.get("event_type") in CERTIFICATE_EVENTS and isinstance(event_id, str):
            certificate_events[event_id] = record

    return errors
