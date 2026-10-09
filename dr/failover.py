"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print("FAILOVER", json.dumps(record))
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL or wait <= 0:
        return {"ok": False, "error": "invalid target or wait"}
    primary = "b" if target == "a" else "a"
    try:
        before = state_of(target)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="1_verify_target", target=target, ok=False, reason=reason)
        return {"ok": False, "target": target, "error": f"target state unavailable: {reason}"}
    emit(step="1_verify_target", target=target, state=before, ok=True)
    try:
        restored = snapshot.get(target, backend)
        primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
        target_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        rpo = snapshot.rpo(primary_db, target_db) if primary_db.exists() and target_db.exists() else {
            "rpo_seconds": None, "docs_lost": None}
        restore_event = emit(step="2_restore_snapshot", target=target, ok=True,
                             snapshot_at=restored.get("snapshot_at"),
                             latest_doc_ts=restored.get("latest_doc_ts"),
                             embed_model_version=restored.get("embed_model_version"),
                             rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"))
    except Exception as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             reason=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "target": target, "error": str(exc)}
    try:
        pathlib.Path(f"state/region-{target}/pool_state").write_text("full")
        emit(step="3_scale_pool", target=target, ok=True, pool_state="full")
    except Exception as exc:
        emit(step="3_scale_pool", target=target, ok=False,
             reason=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "target": target, "error": str(exc)}
    deadline = time.monotonic() + wait
    ready = False
    last_reason = None
    while time.monotonic() <= deadline:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, wait)))
            if response.status_code == 200:
                ready = True
                break
            try:
                last_reason = response.json().get("reasons")
            except ValueError:
                last_reason = f"http_{response.status_code}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        time.sleep(0.2)
    if not ready:
        emit(step="4_wait_ready", target=target, ok=False, reason=last_reason,
             waited_s=round(wait, 2))
        return {"ok": False, "target": target, "error": "target did not become ready", "rpo": rpo}
    waited = wait - max(0.0, deadline - time.monotonic())
    try:
        ready_state = state_of(target)
    except Exception:
        ready_state = {}
    emit(step="4_wait_ready", target=target, ok=True, waited_s=round(waited, 2), state=ready_state)
    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", target=target, ok=True, active_region=target)
    return {"ok": True, "target": target, "primary": primary, "state": ready_state,
            "rpo": rpo, "rpo_seconds": restore_event.get("rpo_seconds"),
            "docs_lost": restore_event.get("docs_lost"),
            "embed_model_version": restored.get("embed_model_version")}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
