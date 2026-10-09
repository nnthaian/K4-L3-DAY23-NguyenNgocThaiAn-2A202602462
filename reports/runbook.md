# Runbook — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage và target còn sống | `python3 chaos/kill_region.py status` | `a.alive=false`, `b.alive=true`; xác minh qua `/healthz` | On-call |
| 2 | Mở incident, ghi nhận thời điểm phát hiện | `python3 dr/runbook.py --primary a --target b --backend fs` rồi xác nhận `y` | `reports/runbook-run.jsonl` có `thong_bao_incident`; lưu t_outage và incident_ts | On-call |
| 3 | Restore snapshot, scale pool, chờ readiness | `tail -n 5 reports/failover-events.jsonl && curl -fsS http://localhost:8002/readyz` (failover được lệnh bước 2 thực hiện) | Log có bước 1–4 đúng thứ tự và `/readyz` B trả 200 | DR operator |
| 4 | Xác minh state replica | `curl -s http://localhost:8002/v1/state` | `count > 0`, `weights=true`, model version được ghi trong log | DR operator |
| 5 | Xác minh DNS/LB cutover | `curl -s http://localhost:8080/edge/state` | `active_region` là `b`; failover log có `5_dns_cutover` | DR operator; incident commander duyệt |
| 6 | Kiểm tra golden signals | `for i in $(seq 1 10); do curl -fsS http://localhost:8080/v1/infer >/dev/null || break; done` | `reports/runbook-run.jsonl` ghi 10 request, error rate 0; lần drill này p95 284.4ms | Service owner |
| 7 | Đo RTO/RPO và viết postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `warnings=[]`, `rto_verdict=PASS`; lưu output và điền report | Incident commander |

**Rollback:** chỉ failback khi A đã `/readyz` 200 ổn định, state đã đồng bộ và 10 request kiểm tra đều thành công. Incident commander phê duyệt; DR operator thực hiện cutover và theo dõi. Nếu B lỗi sau cutover, giữ traffic ở region đang healthy, dừng tự động đổi qua lại, báo incident commander và chỉ failback sau khi A đạt đủ điều kiện trên.

**Drill evidence:** lần chạy hợp lệ ngày 2026-10-09 đo RTO 59.3s, RPO 0.0s / 0 docs; `p95=284.4ms`, `error_rate=0.0` (`reports/runbook-run.jsonl:6`).
