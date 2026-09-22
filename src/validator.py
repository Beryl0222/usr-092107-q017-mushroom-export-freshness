"""校验领域事件信封与事件流。

单记录校验：``validate_event`` 检查信封必填字段。
事件流校验：``validate_stream`` 在此基础上保证：

- event_id 与 idempotency_key 不重复（防止多系统重复登记）；
- 同一聚合内 version 从 1 起连续递增，记录只追加、不原地改写；
- 证书更正必须引用一份已签发的旧证，且使用新的 certificate_id，
  不得复用旧证标识（证书版本不覆盖）。
"""

from datetime import datetime

REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)

EVENT_TYPES = {
    "ZONE_PERMIT_RECORDED",
    "LOT_RECEIVED",
    "SPECIES_CONFIRMED",
    "SPECIES_DISPUTED",
    "LOT_GRADED",
    "HOLD_PLACED",
    "HOLD_RELEASED",
    "BATCH_CREATED",
    "BATCH_REPACKED",
    "BATCH_FORM_CHANGED",
    "TEMP_READING_RECORDED",
    "SAMPLE_TAKEN",
    "SAMPLE_RESULTED",
    "CERTIFICATE_ISSUED",
    "CERTIFICATE_CORRECTED",
    "DESTINATION_RULE_PUBLISHED",
    "ORDER_PLACED",
    "BOOKING_REQUESTED",
    "BOOKING_REVISED",
    "BOOKING_CONFIRMED",
    "SHIPMENT_DISPATCHED",
    "CLEARANCE_APPLIED",
    "CLEARANCE_INSPECTED",
    "CLEARANCE_RELEASED",
    "DELIVERY_ACCEPTED",
    "SETTLEMENT_CALCULATED",
}

AGGREGATE_TYPES = {
    "foraging_zone",
    "foraged_lot",
    "processing_batch",
    "inspection_sample",
    "certificate",
    "destination_rule",
    "customer_order",
    "transport_booking",
    "shipment",
    "customs_clearance",
    "delivery",
    "settlement",
}


def validate_event(record: dict) -> list[str]:
    """校验单条事件信封，返回错误信息列表（空列表表示通过）。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]

    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    if record.get("event_type") and record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知事件类型：{record['event_type']}")
    if record.get("aggregate_type") and record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{record['aggregate_type']}")
    if "occurred_at" in record:
        try:
            datetime.fromisoformat(record["occurred_at"])
        except (TypeError, ValueError):
            errors.append("occurred_at 必须是 ISO-8601 日期时间")

    return errors


def validate_stream(records: list[dict]) -> list[str]:
    """校验整条事件流的幂等、版本与证书更正规则。"""
    errors: list[str] = []

    seen_event_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    aggregate_versions: dict[str, set[int]] = {}
    aggregate_last_version: dict[str, int] = {}

    # certificate_id -> 是否已被旧版占用；更正链 supersedes 目标
    issued_certificates: set[str] = set()

    for index, record in enumerate(records):
        prefix = f"第 {index + 1} 条记录"
        errors.extend(f"{prefix}：{err}" for err in validate_event(record))

        event_id = record.get("event_id")
        if event_id:
            if event_id in seen_event_ids:
                errors.append(f"{prefix}：event_id 重复：{event_id}")
            seen_event_ids.add(event_id)

        idem_key = record.get("idempotency_key")
        if idem_key:
            if idem_key in seen_idempotency_keys:
                errors.append(f"{prefix}：idempotency_key 重复：{idem_key}（拒绝重复登记）")
            seen_idempotency_keys.add(idem_key)

        aggregate_id = record.get("aggregate_id")
        version = record.get("version")
        if aggregate_id and isinstance(version, int) and version >= 1:
            prior_versions = aggregate_versions.setdefault(aggregate_id, set())
            if version in prior_versions:
                errors.append(
                    f"{prefix}：聚合 {aggregate_id} 的 version {version} 已存在，记录不得原地改写"
                )
            last = aggregate_last_version.get(aggregate_id, 0)
            if version != last + 1:
                errors.append(
                    f"{prefix}：聚合 {aggregate_id} 的 version 必须连续递增，"
                    f"期望 {last + 1}，实际 {version}"
                )
            prior_versions.add(version)
            aggregate_last_version[aggregate_id] = version

        event_type = record.get("event_type")
        certificate_id = record.get("certificate_id")

        if event_type == "CERTIFICATE_ISSUED":
            if not certificate_id:
                errors.append(f"{prefix}：证书签发缺少 certificate_id")
            elif certificate_id in issued_certificates:
                errors.append(f"{prefix}：certificate_id 已被旧证占用：{certificate_id}")
            else:
                issued_certificates.add(certificate_id)

        if event_type == "CERTIFICATE_CORRECTED":
            old_id = record.get("supersedes_certificate_id")
            if not old_id:
                errors.append(f"{prefix}：证书更正必须通过 supersedes_certificate_id 引用旧证")
            elif old_id not in issued_certificates:
                errors.append(f"{prefix}：被更正的旧证不存在：{old_id}")
            if not certificate_id:
                errors.append(f"{prefix}：证书更正缺少新 certificate_id")
            elif certificate_id == old_id:
                errors.append(f"{prefix}：更正必须签发新 certificate_id，不得覆盖旧版 {old_id}")
            elif certificate_id in issued_certificates:
                errors.append(f"{prefix}：certificate_id 已被占用：{certificate_id}")
            elif certificate_id:
                issued_certificates.add(certificate_id)

    return errors
