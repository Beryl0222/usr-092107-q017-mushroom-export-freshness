# 野生菌出口鲜度链

本仓库记录该项目已确认的领域对象、事件名称和基础校验方式，便于不同系统交换一致的数据。

香格里拉松茸从山民交货到海外客户收货只有很窄的鲜度窗口。属地查检、原产地证、目的国准入和
口岸预约任何一步延误，都可能迫使企业改做冷冻或干制产品，原先的鲜品订单与菌农结算也随之改变。
本仓库的契约覆盖这条链路的完整记录：采集区域许可、采集户交接、物种复核、分级称重、加工批次、
温控轨迹、检验样本、证书签发、航班或陆运舱位、口岸查验、目的地规则和客户签收。

## 资料范围

- `contracts/domain.schema.json`：领域事件信封、聚合类型、事件名称与逐事件的必备补充字段。
- `data/sample.json`：一条用于本地联调的中文样例。
- `data/chain_sample.json`：从采集许可到客户签收、菌农结算的完整事件链样例。
- `src/validator.py`：事件信封校验（单事件不变量与事件流不变量）。
- `src/policy.py`：舱位重排、放行缺口与降级选择的纯函数策略。
- `tests/`：验证样例与关键约定。

## 聚合与事件

| 聚合 | 含义 | 主要事件 |
| --- | --- | --- |
| `collection_permit` | 采集区域许可 | `PERMIT_REGISTERED` |
| `foraged_lot` | 采集户交接的原料批 | `LOT_RECEIVED`、`SPECIES_CONFIRMED`、`LOT_GRADED`、`HOLD_PLACED`、`HOLD_RELEASED` |
| `processing_batch` | 加工批次（含混装、拆箱、改形态） | `BATCH_CREATED`、`FORM_CHANGED`、`TEMPERATURE_LOGGED`、`SAMPLE_TESTED`、`HOLD_PLACED`、`HOLD_RELEASED` |
| `export_clearance` | 出口清关 | `CLEARANCE_FILED`、`RULE_MATCHED`、`CERTIFICATE_ISSUED`、`CERTIFICATE_CORRECTED`、`CUSTOMS_CHECKED`、`CLEARANCE_RELEASED` |
| `transport_booking` | 航班或陆运舱位预约 | `BOOKING_RESERVED`、`BOOKING_REVISED` |
| `shipment` | 出运批次 | `SHIPMENT_DISPATCHED`、`TEMPERATURE_LOGGED`、`DELIVERY_ACCEPTED` |
| `destination_rule` | 目的国准入规则 | `RULE_PUBLISHED` |
| `settlement` | 菌农结算单 | `SETTLEMENT_RECORDED` |

## 关键约定

- **防重**：`event_id` 全局唯一，各系统以此去重，重复投递必须忽略，防止多系统重复登记。
- **不可改写**：记录一经接收，标识、发生时间与版本不得原地改写；同一聚合内 `version` 从 1 起
  单调递增；更正使用新的后继记录。
- **谱系**：`BATCH_CREATED` 必须携带 `inputs`，混装、拆箱和改加工形态（`FORM_CHANGED`，
  鲜/冻/干）后仍能追到原料来源。
- **按量冻结**：物种存疑或温度超限用 `HOLD_PLACED`/`HOLD_RELEASED` 按 `quantity` 登记，
  只冻结相关数量，不波及整批。
- **规则匹配**：目的国规则按 `shipment_date`（出运日期）落在规则生效区间来匹配。
- **证书版本**：证书更正用 `CERTIFICATE_CORRECTED` 并携带 `supersedes` 指向被更正事件，
  旧版保留可查，不得覆盖。
- **舱位重排**：预约变化时按剩余保鲜时长、订单等级重排；被挤出的预约累计宽减（见
  `policy.rank_bookings`），不能总让小农批次被挤出。
- **放行缺口**：企业用 `policy.release_gaps` 提前看到每批货缺哪些放行环节，用
  `policy.downgrade_options` 评估鲜/冻/干降级选择。
- **海关核对**：`CUSTOMS_CHECKED` 记录电子证书与实物批次的核对结果。
- **结算**：`SETTLEMENT_RECORDED` 按最终接收重量登记，`deductions` 逐项记录质量原因。
- **隐私分级**：`visibility` 分 internal/customs/customer 三级；customer 级记录只给
  `origin_region` 粗粒度产地，不得包含 `collection_point` 具体采集点。个人、机构及商业敏感
  信息仅向履行职责所需的调用方开放。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
