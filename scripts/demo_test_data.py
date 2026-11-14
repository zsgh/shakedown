#!/usr/bin/env python
"""测试数据端到端演示：数据集 → 绑定用例 → 逐行执行 → 断言 → 清理。

用法：
    .venv/bin/python scripts/demo_test_data.py

它会：
1. 自动拉起 examples/demo-target/server.py；
2. 通过 API 建项目、建数据集（3 行用户数据）；
3. 建一条用例，使用 {{data.email}} / {{data.name}} / {{data.role}}；
4. 把数据集绑定到用例（iterate）；
5. 用本地沙箱执行 3 行数据：每行创建用户 → 查询 → 断言 → 删除；
6. 打印结果，并在结束时删除演示项目。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SHAKEDOWN_SANDBOX_MODE", "local")
os.environ.setdefault("SHAKEDOWN_WORKER_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.enums import RunStatus  # noqa: E402
from app.executor.environment import Environment  # noqa: E402
from app.executor.runner import persist_results, run_cases_in_sandbox  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Project, Run, TestCase  # noqa: E402
from app.test_data_service import load_case_data  # noqa: E402

PROJECT_NAME = f"demo-test-data-{int(time.time())}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_target(port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "examples" / "demo-target" / "server.py"), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("演示服务启动失败")


def assertion(kind: str, **extra):
    return {
        "kind": kind,
        "status": "confirmed",
        "evidence": {"source_type": "manual", "ref": "demo", "note": "演示用例"},
        **extra,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="不手工建数据集/绑定，由 {{data.*}} 自动补全")
    args = parser.parse_args()
    port = free_port()
    target = start_target(port)
    project_id = None
    app = create_app()
    try:
        with TestClient(app) as client:
            print(f"▶ 被测服务：http://127.0.0.1:{port}")
            project = client.post(
                "/api/projects",
                json={
                    "name": PROJECT_NAME,
                    "description": "测试数据端到端演示",
                    "base_url": f"http://127.0.0.1:{port}",
                    "isolation": {"mode": "prefix"},
                    "settings": {"test_data_auto_rows": 3} if args.auto else {},
                },
            )
            project.raise_for_status()
            project_id = project.json()["id"]
            print(f"✓ 建项目 #{project_id}")

            dataset = None
            if not args.auto:
                dataset = client.post(
                    f"/api/projects/{project_id}/test-datasets",
                    json={
                        "name": "demo-users",
                        "count": 3,
                        "seed": 7,
                        "fields": [
                            {"name": "email", "type": "string", "format": "email", "unique": True},
                            {"name": "name", "type": "string", "minLength": 4},
                            {"name": "role", "type": "string", "enum": ["user", "viewer"]},
                        ],
                    },
                )
                dataset.raise_for_status()
                dataset = dataset.json()
                print(f"✓ 建数据集 #{dataset['id']}，{dataset['row_count']} 行，PII={dataset['pii_fields']}")
                for row in dataset["rows"]:
                    print("   ", row["row_index"], row["data"])

            spec = {
                "schema_version": "1.0",
                "title": "[数据演示] 用户创建 / 查询 / 清理",
                "dimension": "functional",
                "type": "api",
                "fixtures": [
                    {
                        "kind": "api",
                        "name": "health",
                        "steps": [
                            {
                                "action": "request",
                                "method": "GET",
                                "path": "/api/health",
                                "assert": [assertion("status_code", operator="eq", expected=200)],
                            }
                        ],
                    }
                ],
                "steps": [
                    {
                        "action": "request",
                        "name": "创建用户",
                        "method": "POST",
                        "path": "/api/users",
                        "body": {
                            "name": "{{data.name}}",
                            "email": "{{data.email}}",
                            "role": "{{data.role}}",
                        },
                        "save": {"user_id": "$.id"},
                        "assert": [
                            assertion("status_code", operator="eq", expected=201),
                            assertion("json_path", target="$.email", operator="eq", expected="{{data.email}}"),
                        ],
                    },
                    {
                        "action": "request",
                        "name": "查询用户",
                        "method": "GET",
                        "path": "/api/users/{{user_id}}",
                        "assert": [
                            assertion("status_code", operator="eq", expected=200),
                            assertion("json_path", target="$.name", operator="eq", expected="{{data.name}}"),
                        ],
                    },
                ],
                "cleanup": [
                    {
                        "action": "request",
                        "method": "DELETE",
                        "path": "/api/users/{{user_id}}",
                    }
                ],
            }
            case = client.post(
                f"/api/projects/{project_id}/cases",
                json={
                    "title": spec["title"],
                    "dimension": "functional",
                    "case_type": "api",
                    "status": "enabled",
                    "spec": spec,
                },
            )
            case.raise_for_status()
            case_id = case.json()["id"]
            print(f"✓ 建用例 #{case_id}，使用 {{{{data.*}}}}")

            if not args.auto:
                binding = client.post(
                    f"/api/test-cases/{case_id}/data-bindings",
                    json={"dataset_id": dataset["id"], "mode": "iterate"},
                )
                binding.raise_for_status()
                print(f"✓ 绑定数据集 #{dataset['id']}（iterate）")

            if args.auto:
                with SessionLocal() as session:
                    auto_project = session.get(Project, project_id)
                    auto_case = session.get(TestCase, case_id)
                    from app.test_data_auto import ensure_auto_datasets

                    auto_result = ensure_auto_datasets(session, auto_project, [auto_case])
                    session.commit()
                item = (auto_result.get("items") or [{}])[0]
                print(f"✓ 自动补数据集 #{item.get('dataset_id')}，{item.get('row_count')} 行，无需手工创建/绑定")
        with SessionLocal() as session:
            project = session.get(Project, project_id)
            case = session.get(TestCase, case_id)
            run = Run(project_id=project_id, trigger="manual", status=RunStatus.RUNNING.value)
            session.add(run)
            session.flush()
            data_by_case = load_case_data(session, [case])
            env = Environment(base_url=project.base_url, accounts={})
            with tempfile.TemporaryDirectory(prefix="shakedown-demo-data-") as artifacts:
                outcome = run_cases_in_sandbox(
                    project,
                    run,
                    [case],
                    env,
                    artifacts_dir=artifacts,
                    sandbox_mode="local",
                    data_by_case=data_by_case,
                )
                if outcome.error:
                    print(f"✗ 沙箱错误：{outcome.error}")
                    print(outcome.stderr[-2000:])
                    return 1
                rows = persist_results(session, project, run, [case], outcome)
                run.status = RunStatus.COMPLETED.value
                session.commit()
                result = rows[0]
                print("\n▶ 执行结果")
                print(f"  用例：{case.title}")
                print(f"  状态：{result.status}")
                print(f"  数据行：{result.output.get('data_rows')} / iterations={len(result.output.get('iterations') or [])}")
                for iteration in result.output.get("iterations") or []:
                    print(f"    [{iteration['index']}] {iteration['status']} · {iteration['assertions']} 条断言")
                print("  日志：")
                for line in (result.logs or "").splitlines():
                    print("   ", line)

            if result.status == "pass":
                print("\n✅ 3 行数据全部通过：每行都用不同的 {{data.*}} 建用户、查用户、清理。")
                return 0
            print("\n❌ 演示失败，见上方结果。")
            return 1
    finally:
        target.terminate()
        try:
            target.wait(timeout=3)
        except Exception:  # noqa: BLE001
            target.kill()
        if project_id is not None:
            with SessionLocal() as session:
                project = session.get(Project, project_id)
                if project is not None:
                    session.delete(project)
                    session.commit()
            print("✓ 已清理演示项目")


if __name__ == "__main__":
    raise SystemExit(main())
