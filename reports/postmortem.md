# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T15:29:26 | Region A bị netblock; RTO clock bắt đầu | `chaos/chaos-events.jsonl:4` |
| 2026-10-09T15:29:28 | Request đầu tiên lỗi, +2.2s | `reports/drill-2-withdr.jsonl:41` |
| 2026-10-09T15:29:44 | Health checker đánh dấu A UNHEALTHY, +18.2s | `reports/health-events.jsonl:2` |
| 2026-10-09T15:30:14 | Runbook bắt đầu/incident được ghi nhận, +48.1s | `reports/runbook-run.jsonl:1` và `:2` |
| 2026-10-09T15:30:22 | B ready và DNS cutover, +55.7s | `reports/failover-events.jsonl:4` và `:5` |
| 2026-10-09T15:30:25 | Request đầu tiên thành công từ B, RTO 59.3s | `reports/drill-2-withdr.jsonl:67` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `59.3s` · còn headroom: `240.7s`
- RPO mục tiêu: 300s · đo được: `0.0s` (`0` doc bị mất) · còn headroom: `300s`
- **Bước tốn nhiều giây nhất:** chờ operator khởi chạy runbook sau alert, khoảng `29.9s` — hệ thống phát hiện outage nhưng drill hiện yêu cầu operator xác nhận/chạy runbook.

## 3. Root cause (5 whys)

1. **Vì sao người dùng bị gián đoạn 59.3 giây?** Edge tiếp tục gửi request tới Region A sau khi A bị netblock; request lỗi đầu tiên xuất hiện ở +2.2 giây (`reports/drill-2-withdr.jsonl:41`).
2. **Vì sao traffic không chuyển ngay khi health checker phát hiện lỗi?** Alert UNHEALTHY xuất hiện ở +18.2 giây, nhưng runbook chỉ bắt đầu khoảng +48.1 giây (`reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:1`).
3. **Vì sao runbook bắt đầu muộn?** Drill yêu cầu operator khởi chạy runbook sau khi xác nhận outage; khoảng chờ từ alert đến khi bắt đầu là gần 29.9 giây (`reports/runbook-run.jsonl:2`).
4. **Vì sao operator là bước riêng?** Quy trình được thiết kế bán tự động để tránh failover hai chiều khi health signal chập chờn; chưa có paging/automation nối alert với xác nhận vận hành.
5. **Vì sao độ trễ này có thể lặp lại?** Chưa có mục tiêu thời gian phản hồi alert và chưa diễn tập đo riêng thời gian từ alert đến runbook.

**Root cause:** độ trễ vận hành giữa health alert và việc khởi chạy runbook, không phải tốc độ snapshot copy. Failover sau khi bắt đầu hoàn tất readiness trong khoảng 6.5 giây (`reports/failover-events.jsonl:4`).

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Đưa alert UNHEALTHY vào quy trình paging và đặt mục tiêu operator bắt đầu runbook trong 5s; diễn tập lại để đo cải thiện | On-call lead | 2026-10-16 | Tối ưu mục tiêu tới 25s so với lần đo này |
| 2 | Giữ pool phụ warm khi yêu cầu RTO thấp hơn; theo dõi chi phí và readiness liên tục | Platform owner | 2026-10-23 | Có thể giảm phần warm-up tối đa khoảng 6.5s |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold = 5 × 3 = 15s`, tương đương khoảng `25.3%` RTO 59.3s. Detect thực tế là 18.2s.
2. Hạ interval từ 5s xuống 1s làm detect floor từ 15s còn 3s, giảm tối đa 12s ở thành phần detection; RTO thực tế có thể giảm ít hơn vì operator dispatch, restore, warm-up và TTL vẫn còn. Poll dày hơn có thể tạo false alarm/flapping khi có lỗi ngắn.
3. `docs_lost=0` nghĩa là snapshot restore lần này không bỏ sót document nào tại thời điểm đo; với outage 6 giờ và mất primary vĩnh viễn, số này chỉ có ý nghĩa nếu replica tiếp tục chạy và snapshot cuối cùng còn nguyên. Nó đại diện cho dữ liệu khách hàng có thể phải nhập lại hoặc đã mất.
