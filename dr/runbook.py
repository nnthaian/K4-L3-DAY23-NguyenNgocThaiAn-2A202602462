"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print("RUNBOOK", json.dumps(record))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    started = time.time()

    def health(region: str) -> tuple[bool, str]:
        try:
            response = httpx.get(f"{URL[region]}/healthz", timeout=1.0)
            return response.status_code == 200, f"http_{response.status_code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def verify_streak(region: str, expected_alive: bool, threshold: int = 3,
                      max_attempts: int = 5) -> tuple[bool, str, int]:
        consecutive = 0
        last_reason = "no probe result"
        for attempt in range(1, max_attempts + 1):
            alive, last_reason = health(region)
            consecutive = consecutive + 1 if alive == expected_alive else 0
            if consecutive >= threshold:
                return True, last_reason, attempt
            if attempt < max_attempts:
                time.sleep(0.2)
        return False, last_reason, max_attempts

    primary_down, primary_reason, primary_attempts = verify_streak(primary, expected_alive=False)
    target_alive, target_reason, target_attempts = verify_streak(target, expected_alive=True)
    outage_ts = None
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_log.exists():
        try:
            events = [json.loads(line) for line in chaos_log.read_text().splitlines() if line.strip()]
            kills = [e for e in events if e.get("action") == "kill" and e.get("region") == primary]
            if kills:
                outage_ts = kills[-1].get("ts")
        except (OSError, json.JSONDecodeError):
            pass
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_alive=not primary_down, primary_reason=primary_reason,
         primary_probe_attempts=primary_attempts,
         target_alive=target_alive, target_reason=target_reason,
         target_probe_attempts=target_attempts,
         confirmed=primary_down and target_alive)

    if not primary_down or not target_alive:
        step(2, "thong_bao_incident", ok=False, aborted=True,
             reason="outage not confirmed or failover target unavailable")
        return {"ok": False, "error": "outage not confirmed or target unavailable"}

    if not confirm(auto, f"Region {primary} is unavailable. Start failover to {target}?"):
        step(2, "thong_bao_incident", ok=False, cancelled=True,
             outage_ts=outage_ts, incident_ts=time.time())
        return {"ok": False, "cancelled": True}
    incident_ts = time.time()
    step(2, "thong_bao_incident", ok=True, outage_ts=outage_ts,
         incident_ts=incident_ts,
         notification_delay_s=None if outage_ts is None else round(incident_ts - outage_ts, 2))

    try:
        result = fo.failover(target, backend, wait=60.0)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    step(3, "scale_gpu_pool", ok=bool(result.get("ok")), failover_result=result)

    state = result.get("state") or {}
    replica_ok = bool(result.get("ok")) and state.get("count", 0) > 0 and state.get("weights") is True
    step(4, "verify_state_replica", ok=replica_ok,
         vector_count=state.get("count"), weights=state.get("weights"),
         embed_model_version=result.get("embed_model_version"))
    step(5, "dns_cutover", ok=bool(result.get("ok")),
         active_region=target if result.get("ok") else None)

    latencies = []
    errors = 0
    if result.get("ok"):
        for _ in range(10):
            t0 = time.perf_counter()
            try:
                response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
                if response.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.perf_counter() - t0) * 1000)
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))] if ordered else None
    step(6, "verify_golden_signals", ok=bool(latencies) and errors == 0,
         requests=len(latencies), p95_latency_ms=None if p95 is None else round(p95, 1),
         error_rate=None if not latencies else round(errors / len(latencies), 3))

    elapsed = time.time() - started
    summary = step(7, "post_incident", ok=bool(result.get("ok")), elapsed_s=round(elapsed, 2),
                   rto_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": bool(result.get("ok")), "failover": result,
            "golden_signals": {"requests": len(latencies), "errors": errors,
                               "p95_latency_ms": None if p95 is None else round(p95, 1)},
            "post_incident": summary}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
