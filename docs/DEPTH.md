# 深度登记 商圈客流设备重叠与人数区间估计

规模：中型。核心规则：无法识别个人时只输出满足区域交集约束的人数上下界；矛盾观测隔离而非伪造精确人数。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 设备采信 | 查看并复算设备采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 区域映射 | 查看并复算区域映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 观测分桶 | 查看并复算观测分桶 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 重叠约束 | 查看并复算重叠约束 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 人数估界 | 查看并复算人数估界 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 日汇对账 | 查看并复算日汇对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py` | `tests/test_reconcile.py` |
| M7 报告归档 | 查看并复算报告归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |
| M8 续跑水位 | 查看下一批续跑位置与待补算时段 | `/api/m8_watermark` | `app/watermark.py` | `tests/test_watermark.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-11 | 时区混用 | 同刻不同桶 | UTC归整 | `app/modules/m2_mapping/coerce.py` | `tests/test_versions.py::test_same_instant` |
| TRAP-19 | 三方约束矛盾 | 输出伪精确数 | 非负原子区域可行检查 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_three_way_contradiction` |
| TRAP-27 | 补传晚到且业务时间早 | 单水位跳过迟到时段或补传不触发重算 | 到达水位与业务水位分离，迟到时段登记待补算，确认覆盖后结转 | `app/watermark.py` | `tests/test_watermark.py::test_late_batch_registers_backfill` |
| TRAP-28 | 水位确认中途失败 | 半更新或记录丢失 | 提案先落库，单事务CAS确认，失败保留旧版且提案与日志留痕 | `app/watermark.py` | `tests/test_watermark.py::test_failed_confirm_keeps_old_and_logs` |
| TRAP-29 | 批次重试 | 水位倒退或重复占位 | 幂等键已占位则跳过，确认只增 | `app/store.py` | `tests/test_watermark.py::test_retry_does_not_regress` |
| TRAP-30 | 签认后再次复核 | 已签认结果被覆盖 | 通过即封存，再改返回409 | `app/store.py` | `tests/test_watermark.py::test_sealed_review_cannot_be_overwritten` |

## 续跑水位

到达时间与业务时间是两条独立时间线，任何单一水位都无法同时正确表达。到达水位按入库批次分配单调序号，只增不减，重试同一幂等键不重复占位，下一批续跑位置为到达水位加一；业务水位为已确认覆盖的最大业务时段（UTC小时桶），只取最大不倒退。批次中不晚于当前业务水位的时段判定为迟到补传：未在册的登记为待补算，已在册的由本批次结转为已补算并记录解决批次。水位变更两阶段执行：提案独立落库后单事务条件确认，版本冲突则保留旧版、记录失败日志并按最新水位重新提案；日志独立提交，不随确认回滚。复核通过的批次封存，任何再覆盖返回409。

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
