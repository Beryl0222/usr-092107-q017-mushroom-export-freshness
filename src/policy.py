"""舱位重排与放行缺口的纯函数策略。

预约变化时按剩余保鲜时长、订单等级和舱位重新排序；被挤出的预约
累计宽减，避免小农批次反复被挤到队尾。企业侧用 release_gaps 提前
看到每批货的放行缺口，用 downgrade_options 评估鲜/冻/干降级选择。
"""
from __future__ import annotations

# 放行前必须完成的环节（事件类型 → 中文说明）。
# 调用方汇总该批货关联聚合上已发生的事件类型后传入 release_gaps。
RELEASE_REQUIRED_STEPS = (
    ("SPECIES_CONFIRMED", "物种复核"),
    ("SAMPLE_TESTED", "检验样本合格"),
    ("RULE_MATCHED", "目的国规则匹配"),
    ("CERTIFICATE_ISSUED", "证书签发"),
    ("CUSTOMS_CHECKED", "口岸查验核对"),
)


def rank_bookings(
    bookings: list[dict],
    *,
    deferral_relief_hours: float = 6.0,
    max_relief_hours: float = 24.0,
) -> list[dict]:
    """按出运紧迫度重排舱位预约，返回排序后的列表（不改动入参）。

    排序依据：
    1. 等效剩余保鲜时长（remaining_freshness_hours 减去被挤出宽减）短者优先；
    2. 订单等级 order_grade 高者优先；
    3. 小农批次 smallholder 优先；
    4. booking_id 字典序，保证结果确定。

    每被挤出一次（deferrals），预约获得 deferral_relief_hours 小时的宽减，
    上限 max_relief_hours——小农批次不会因为没有订单等级优势而永远排不到舱位。
    """
    def sort_key(booking: dict) -> tuple:
        relief = min(
            booking.get("deferrals", 0) * deferral_relief_hours,
            max_relief_hours,
        )
        effective_remaining = booking["remaining_freshness_hours"] - relief
        return (
            effective_remaining,
            -booking.get("order_grade", 0),
            not booking.get("smallholder", False),
            booking["booking_id"],
        )

    return sorted(bookings, key=sort_key)


def release_gaps(
    completed_event_types,
    *,
    open_holds: int = 0,
    required=RELEASE_REQUIRED_STEPS,
) -> list[str]:
    """根据已完成的事件类型，列出放行前仍缺的环节（中文说明）。

    证书更正（CERTIFICATE_CORRECTED）视为证书已签发的新版，满足证书环节；
    存在未解除的数量冻结时，追加提示。
    """
    done = set(completed_event_types)
    if "CERTIFICATE_CORRECTED" in done:
        done.add("CERTIFICATE_ISSUED")
    gaps = [label for step, label in required if step not in done]
    if open_holds > 0:
        gaps.append(f"存在 {open_holds} 笔未解除的数量冻结")
    return gaps


def downgrade_options(
    remaining_freshness_hours: float,
    *,
    fresh_min_hours: float = 36.0,
    frozen_min_hours: float = 8.0,
) -> list[str]:
    """按剩余保鲜时长给出可行的加工形态降级选择。

    默认阈值为示例值，企业应按航线与口岸耗时调整：
    - 剩余时长足够鲜品运输（>= fresh_min_hours）：无需降级，返回空列表；
    - 不足鲜品但尚可换装冻品（>= frozen_min_hours）：可转 frozen 或 dried；
    - 更短：只能转 dried。
    """
    if remaining_freshness_hours >= fresh_min_hours:
        return []
    if remaining_freshness_hours >= frozen_min_hours:
        return ["frozen", "dried"]
    return ["dried"]
