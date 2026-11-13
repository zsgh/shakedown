#!/usr/bin/env python
"""Shakedown 端到端演示。

流程：
1. 建项目（被测环境指向本地演示服务）；
2. 导入 4 个知识源（Markdown 文档 / 本地代码库 / Swagger 2.0 / OpenAPI 3.0）；
3. Agent 抽取功能点、生成四维度用例；
4. 人工「审核」：启用用例；
5. 在沙箱里执行（本地沙箱模式，未接 berth 时也能跑）；
6. 失败归因 → 生成疑似缺陷；
7. 输出看板数据 + HTML 报告路径。

用法：
    python scripts/demo.py                # 使用本地演示服务（自动拉起）
    python scripts/demo.py --no-server   # 不拉起服务（只做前半段）
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SHAKEDOWN_SANDBOX_MODE", "local")
os.environ.setdefault("SHAKEDOWN_LLM_PROVIDER", "mock")

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.enums import CaseStatus, KnowledgeKind, RunStatus  # noqa: E402
from app.models import Endpoint, Finding, Project, Run, TestCase  # noqa: E402
from app.worker import handlers  # noqa: E402

DEMO_PROJECT = "demo-user-center"
DEMO_DIR = REPO_ROOT / "examples" / "demo-project"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_target(port: int) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "examples" / "demo-target" / "server.py"),
        "--port",
        str(port),
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("演示服务启动失败")


def ensure_project(base_url: str) -> int:
    with session_scope() as session:
        project = session.scalar(select(Project).where(Project.name == DEMO_PROJECT))
        if project is None:
            project = Project(
                name=DEMO_PROJECT,
                description="演示项目：用户中心（故意留了两个 bug）",
                base_url=base_url,
                isolation={"mode": "prefix"},
                settings_json={},
            )
            session.add(project)
            session.flush()
        else:
            project.base_url = base_url
        project_id = project.id
    return project_id


def ingest_sources(project_id: int) -> None:
    from app.ingest.service import ingest_source
    from app.models import KnowledgeSource

    targets = [
        (KnowledgeKind.MARKDOWN.value, str(DEMO_DIR / "docs"), "接口文档（Markdown）"),
        (KnowledgeKind.CODE.value, str(DEMO_DIR / "app"), "被测代码库"),
        (KnowledgeKind.OPENAPI.value, str(DEMO_DIR / "specs" / "swagger.json"), "Swagger 2.0"),
        (KnowledgeKind.OPENAPI.value, str(DEMO_DIR / "specs" / "openapi.yaml"), "OpenAPI 3.0"),
    ]
    with session_scope() as session:
        project = session.get(Project, project_id)
        for kind, location, name in targets:
            existing = session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.project_id == project_id,
                    KnowledgeSource.kind == kind,
                    KnowledgeSource.location == location,
                )
            )
            if existing:
                continue
            source = KnowledgeSource(
                project_id=project_id,
                kind=kind,
                name=name,
                location=location,
                status="pending",
            )
            session.add(source)
            session.flush()
            try:
                meta = ingest_source(session, project, source)
                print(f"  ✓ 导入 {name}: {meta.get('chunks', 0)} 块 / {meta.get('endpoints', 0)} 接口")
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ 导入 {name} 失败：{exc}")


def reset_project(project_id: int) -> None:
    """清掉演示项目上一轮的数据，保证演示可重复。"""
    from app.models import CaseResult, Feature, Finding, HumanFeedback, Run, TestCase

    with session_scope() as session:
        session.query(CaseResult).filter_by(project_id=project_id).delete()
        session.query(Finding).filter_by(project_id=project_id).delete()
        session.query(TestCase).filter_by(project_id=project_id).delete()
        session.query(Feature).filter_by(project_id=project_id).delete()
        session.query(HumanFeedback).filter_by(project_id=project_id).delete()
        session.query(Run).filter_by(project_id=project_id).delete()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-server", action="store_true", help="不拉起演示服务")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep-data", action="store_true", help="保留上一轮演示数据")
    parser.add_argument("--report", default=str(REPO_ROOT / "var" / "demo-report.html"))
    args = parser.parse_args()

    port = args.port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = None
    if not args.no_server:
        print(f"▶ 拉起演示被测系统：{base_url}")
        process = start_target(port)

    try:
        print("▶ 创建项目")
        project_id = ensure_project(base_url)
        if not args.keep_data:
            reset_project(project_id)

        print("▶ 导入知识源")
        ingest_sources(project_id)

        print("▶ Agent 抽取功能点")
        with session_scope() as session:
            result = handlers.handle_plan_features(session, {"project_id": project_id})
        print(f"  ✓ 功能点 {result['features']} 个（未映射 {result['unmapped']} 个）")

        print("▶ Agent 生成用例（功能 / 边界 / 安全 / 性能）")
        with session_scope() as session:
            result = handlers.handle_generate_cases(session, {"project_id": project_id})
        print(f"  ✓ 用例 {result['cases']} 条")

        print("▶ 人工审核：启用全部用例")
        with session_scope() as session:
            cases = session.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
            for case in cases:
                case.status = CaseStatus.ENABLED.value
            print(f"  ✓ 已启用 {len(cases)} 条")

        print("▶ 执行")
        with session_scope() as session:
            run = Run(project_id=project_id, trigger="manual", status=RunStatus.QUEUED.value, case_ids=[])
            session.add(run)
            session.flush()
            run_id = run.id
        with session_scope() as session:
            handlers.handle_execute_run(session, {"run_id": run_id})

        with session_scope() as session:
            run = session.get(Run, run_id)
            stats = {k: v for k, v in (run.stats or {}).items() if k != "sandbox"}
            print(f"  ✓ 执行完成：{stats}")

        with session_scope() as session:
            findings = session.scalars(select(Finding).where(Finding.project_id == project_id)).all()
            print(f"▶ 疑似缺陷 {len(findings)} 条：")
            for finding in findings:
                print(f"  - [{finding.severity}] {finding.title}")
                if finding.repro:
                    print(f"      复现：{finding.repro[:120]}")

            endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all()
            inconsistent = [e for e in endpoints if (e.consistency or {}).get("state") in {"inconsistent", "conflict"}]
            print(f"▶ 接口清单 {len(endpoints)} 条，其中三方不一致 {len(inconsistent)} 条：")
            for endpoint in inconsistent:
                messages = [m.get("message", "") for m in (endpoint.consistency or {}).get("mismatches", [])]
                print(f"  - {endpoint.method} {endpoint.path}：{messages}")

        from app.api.reports import build_overview
        from app.reporting.html_export import render_project_report

        with session_scope() as session:
            project = session.get(Project, project_id)
            overview = build_overview(session, project)
            html = render_project_report({"project": {"id": project.id, "name": project.name}, "overview": overview})
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(html, encoding="utf-8")
        print(f"▶ 报告已导出：{report_path}")
        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
