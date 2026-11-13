#!/usr/bin/env python3
"""灰度 / 部署后 canary 检查：就绪探针 → 触发 smoke → gate 判定。

用法：
  python scripts/canary_check.py --base-url http://127.0.0.1:8000 \
      --project-id 11 --token "$SHAKEDOWN_TRIGGER_TOKEN"

退出码：0=canary 通过；1=run 失败/超时；2=环境未就绪/配置错误。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _request(method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", errors="replace")[:500]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    status, ready = _request("GET", f"{base}/api/health/ready")
    if status != 200 or ready.get("status") != "ready":
        print(json.dumps({"ok": False, "stage": "readiness", "status": status, "body": ready}, ensure_ascii=False))
        return 2

    headers = {"X-Shakedown-Trigger-Token": args.token} if args.token else {}
    status, triggered = _request(
        "POST",
        f"{base}/api/projects/{args.project_id}/automation/trigger",
        body={"trigger": "canary", "policy": {"mode": "smoke", "max_cases": 30}, "note": "部署后 canary"},
        headers=headers,
    )
    if status not in {200, 201} or not triggered.get("run_id"):
        print(json.dumps({"ok": False, "stage": "trigger", "status": status, "body": triggered}, ensure_ascii=False))
        return 2
    run_id = int(triggered["run_id"])
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        _, run = _request("GET", f"{base}/api/runs/{run_id}")
        if run.get("status") in {"completed", "failed", "cancelled"}:
            _, gate = _request("GET", f"{base}/api/projects/{args.project_id}/automation/gate?run_id={run_id}")
            ok = run.get("status") == "completed" and bool(gate.get("passed"))
            print(json.dumps({"ok": ok, "run_id": run_id, "run_status": run.get("status"), "gate": gate}, ensure_ascii=False))
            return 0 if ok else 1
        time.sleep(3)
    print(json.dumps({"ok": False, "stage": "poll", "run_id": run_id, "error": "canary 超时"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
