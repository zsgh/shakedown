"""端到端 API 测试：建项目 → 导入知识源 → 抽取功能点 → 生成用例 → 执行 → 缺陷。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.db import session_scope
from app.models import Finding, Run
from app.models import TestCase as CaseModel
from app.worker import handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "examples" / "demo-project"


class _Handler(BaseHTTPRequestHandler):
    """极简被测服务：实现 spec 里声明的三个接口，并故意不校验 amount。"""

    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/users"):
            self._send(200, {"items": [], "total": 0})
        elif self.path.startswith("/api/orders"):
            self._send(200, {"id": 1, "amount": 100, "status": "created"})
        elif self.path.startswith("/api/admin/stats"):
            self._send(200, {"users": 0, "orders": 0})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/api/users"):
            self._send(201, {"id": 1, "name": payload.get("name"), "email": payload.get("email")})
        elif self.path.startswith("/api/orders"):
            self._send(201, {"id": 1, "amount": payload.get("amount"), "status": "created"})
        else:
            self._send(404, {"message": "not found"})


@pytest.fixture()
def target_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_end_to_end_flow(client, target_server):
    # 1. 建项目
    response = client.post(
        "/api/projects",
        json={"name": "e2e", "base_url": target_server, "description": "端到端"},
    )
    assert response.status_code == 201
    project_id = response.json()["id"]

    # 2. 导入知识源（同步解析）
    response = client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "markdown", "location": str(DEMO / "docs"), "name": "文档"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "ready"

    response = client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "openapi", "location": str(DEMO / "specs" / "swagger.json"), "name": "Swagger 2.0"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "ready"

    response = client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "code", "location": str(DEMO / "app"), "name": "代码库"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "ready"

    # 3. 接口清单
    endpoints = client.get(f"/api/projects/{project_id}/endpoints").json()
    paths = {(e["method"], e["path"]) for e in endpoints}
    assert ("POST", "/api/users") in paths
    assert ("DELETE", "/api/users/{id}") in paths  # 只在代码里出现的接口

    # 4. 三方一致性
    report = client.post(f"/api/projects/{project_id}/consistency/check").json()
    assert report["endpoints"] > 0
    inconsistent = client.get(f"/api/projects/{project_id}/consistency").json()
    assert isinstance(inconsistent, list)

    # 4.1 导出给 AI 修的 Markdown 清单：带证据定位与修复指引
    response = client.get(f"/api/projects/{project_id}/consistency/export.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert "任务清单" in body and "修复指引" in body and "验收标准" in body
    if inconsistent:
        first = inconsistent[0]
        assert f"{first['method']} {first['path']}" in body  # 每条不一致都要出现在清单里

    # 5. 抽取功能点 + 生成用例（直接调 handler，避免依赖 worker）
    with session_scope() as session:
        result = handlers.handle_plan_features(session, {"project_id": project_id})
    assert result["features"] > 0

    with session_scope() as session:
        result = handlers.handle_generate_cases(session, {"project_id": project_id})
    assert result["cases"] > 0

    # 6. 用例默认「待确认」，人工启用后才参与执行
    queue = client.get(f"/api/projects/{project_id}/cases/review-queue").json()
    assert queue, "Agent 生成的用例应进入待审核队列"
    assert all(item["status"] in {"pending", "needs_review"} for item in queue)

    coverage = client.get(f"/api/projects/{project_id}/coverage").json()
    assert coverage["total_features"] > 0

    # 7. 启用全部用例（高风险操作需要显式确认）
    ids = [item["id"] for item in queue]
    response = client.post(
        f"/api/projects/{project_id}/cases/batch-status",
        json={"case_ids": ids, "status": "enabled"},
    )
    if response.status_code == 409:
        # 存在高风险用例（删除数据 / 修改配置）→ 需要人工确认
        assert "高风险" in response.json()["error"]["message"]
        response = client.post(
            f"/api/projects/{project_id}/cases/batch-status",
            json={"case_ids": ids, "status": "enabled", "confirm_risk": True},
        )
    assert response.status_code == 200
    assert response.json()["updated"] == len(ids)

    # 8. 执行
    with session_scope() as session:
        run = Run(project_id=project_id, trigger="manual", status="queued", case_ids=ids)
        session.add(run)
        session.flush()
        run_id = run.id
    with session_scope() as session:
        handlers.handle_execute_run(session, {"run_id": run_id})

    with session_scope() as session:
        run = session.get(Run, run_id)
        stats = run.stats or {}
        assert stats["total"] > 0
        assert stats["passed"] + stats["failed"] + stats["errored"] + stats["flaky"] == stats["total"]

    results = client.get(f"/api/runs/{run_id}/results").json()
    assert len(results) >= stats["total"]
    assert any(item["status"] in {"pass", "fail"} for item in results)

    # 9. 失败归因 + 缺陷
    with session_scope() as session:
        findings = session.query(Finding).filter_by(project_id=project_id).all()
        for finding in findings:
            assert finding.source == "execution"
            assert finding.repro or finding.evidence

    # 10. 报告导出
    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Shakedown" in response.text

    response = client.get(f"/api/projects/{project_id}/report")
    assert response.status_code == 200


def test_agent_generated_cases_have_evidence(client):
    """每条用例都要有依据；无依据的断言标待确认。"""
    response = client.post("/api/projects", json={"name": "evidence", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "openapi", "location": str(DEMO / "specs" / "swagger.json")},
    )
    with session_scope() as session:
        handlers.handle_plan_features(session, {"project_id": project_id})
        handlers.handle_generate_cases(session, {"project_id": project_id})
        cases = session.query(CaseModel).filter_by(project_id=project_id).all()
        assert cases
        for case in cases:
            spec = case.spec or {}
            assertions = spec.get("assertions") or []
            for assertion in assertions:
                evidence = assertion.get("evidence") or {}
                if assertion.get("status") == "confirmed":
                    assert evidence.get("source_type") not in (None, "", "none"), (
                        f"已确认断言必须带依据：{case.title}"
                    )
                else:
                    assert assertion.get("status") == "pending"
            assert case.status == "pending", "Agent 生成的用例默认待确认"
