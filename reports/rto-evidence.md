# RTO/RPO Evidence — Lab 23

Mọi số dưới đây lấy từ log của drill trong workspace; thời gian tương đối tính từ `t_outage`.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T14:47:04` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:16` |
| Request thành công sau đó | không có | không có request `ok:true` sau outage trong cửa sổ traffic | `reports/drill-1-nodr.jsonl:28` |
| RTO | `NO_RECOVERY` | không có request phục hồi trước khi traffic kết thúc | `reports/drill-1-nodr.jsonl:28` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | `0s` | `2026-10-09T15:29:26` | `chaos/chaos-events.jsonl:4` |
| User thấy lỗi đầu tiên | `+2.2s` | dòng `ok:false` đầu sau outage | `reports/drill-2-withdr.jsonl:41` |
| Health check phát hiện | `+18.2s` | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `+48.9s` | `step:2_restore_snapshot`, RPO 0s/0 docs | `reports/failover-events.jsonl:2` |
| Region phụ ready | `+55.7s` | `step:4_wait_ready`, waited 6.52s | `reports/failover-events.jsonl:4` |
| DNS cutover | `+55.7s` | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | `+59.3s` | request đầu `ok:true`, served_by b | `reports/drill-2-withdr.jsonl:67` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `59.3s` | 300s (5 phút) | PASS; headroom 240.7s |
| RPO — Vector DB | `0.0s` / `0` doc | 300s (5 phút) | PASS; headroom 300s |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detection | `18.2s` (floor `15.0s`) | `interval_s=5`, `threshold=3`; `reports/health-events.jsonl:2` | Giảm interval; cân bằng với nguy cơ flapping |
| Operator/runbook dispatch delay | `29.9s` | Health alert → runbook start; `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:1` | Paging và mục tiêu phản hồi on-call rõ ràng |
| Target verify + snapshot restore | `0.8s` | Runbook start → restore complete; `reports/runbook-run.jsonl:1`, `reports/failover-events.jsonl:2` | Tối ưu thao tác restore; snapshot copy hiện đã nhanh |
| GPU pool warm-up | `6.75s` | `3_scale_pool`→`4_wait_ready`; `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4` (`waited_s=6.52s`) | Warm pool sẵn sàng sẽ giảm latency nhưng tốn tài nguyên |
| DNS/LB TTL cache | `3.6s` | Cutover → request phục hồi; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:67` | Giảm EDGE_TTL_SECONDS; cân nhắc cache churn |

Tổng các thành phần là khoảng `59.25s`, làm tròn thành RTO đo được `59.3s`. RPO và embedding model version được ghi tại `reports/failover-events.jsonl:2`.
