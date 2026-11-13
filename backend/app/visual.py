"""视觉基线：基于浏览器用例截图 hash 的轻量回归比较。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseResult, Project, TestCase, VisualBaseline


def latest_screenshot(db: Session, project: Project, case_id: int) -> dict[str, Any] | None:
    case = db.get(TestCase, case_id)
    if case is None or case.project_id != project.id:
        raise ValueError(f"用例不存在或不属于当前项目：{case_id}")
    result = db.scalar(
        select(CaseResult)
        .where(
            CaseResult.project_id == project.id,
            CaseResult.case_id == case_id,
            CaseResult.output["screenshot_sha256"].astext.isnot(None),
        )
        .order_by(CaseResult.id.desc())
        .limit(1)
    )
    if result is None:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    return {
        "case_id": case_id,
        "result_id": result.id,
        "sha256": str(output.get("screenshot_sha256") or ""),
        "screenshot_path": str(output.get("screenshot") or result.screenshot_path or ""),
        "created_at": result.created_at.isoformat() if result.created_at else None,
    }


def set_baseline(
    db: Session,
    project: Project,
    case_id: int,
    *,
    name: str = "default",
    sha256: str | None = None,
    screenshot_path: str | None = None,
) -> VisualBaseline:
    latest = latest_screenshot(db, project, case_id)
    digest = (sha256 or (latest or {}).get("sha256") or "").strip()
    if not digest:
        raise ValueError("还没有该用例的截图 hash，请先执行一次浏览器用例")
    row = db.scalar(
        select(VisualBaseline).where(
            VisualBaseline.project_id == project.id,
            VisualBaseline.case_id == case_id,
            VisualBaseline.name == name,
        )
    )
    if row is None:
        row = VisualBaseline(
            project_id=project.id,
            case_id=case_id,
            name=name,
            sha256=digest,
            screenshot_path=screenshot_path or (latest or {}).get("screenshot_path", ""),
            meta={"source_result_id": (latest or {}).get("result_id")},
        )
        db.add(row)
    else:
        row.sha256 = digest
        row.screenshot_path = screenshot_path or (latest or {}).get("screenshot_path", "")
        row.meta = {"source_result_id": (latest or {}).get("result_id")}
    db.flush()
    return row


def compare_baseline(
    db: Session,
    project: Project,
    case_id: int,
    *,
    name: str = "default",
) -> dict[str, Any]:
    baseline = db.scalar(
        select(VisualBaseline).where(
            VisualBaseline.project_id == project.id,
            VisualBaseline.case_id == case_id,
            VisualBaseline.name == name,
        )
    )
    if baseline is None:
        raise ValueError("视觉基线不存在，请先创建 baseline")
    latest = latest_screenshot(db, project, case_id)
    if latest is None:
        return {
            "case_id": case_id,
            "name": name,
            "status": "no_screenshot",
            "baseline_sha256": baseline.sha256,
            "current_sha256": "",
            "changed": None,
            "message": "还没有新的截图结果",
        }
    changed = latest["sha256"] != baseline.sha256
    return {
        "case_id": case_id,
        "name": name,
        "status": "changed" if changed else "same",
        "baseline_sha256": baseline.sha256,
        "current_sha256": latest["sha256"],
        "baseline_path": baseline.screenshot_path,
        "current_path": latest["screenshot_path"],
        "changed": changed,
        "source_result_id": latest["result_id"],
    }


def list_baselines(db: Session, project: Project) -> list[VisualBaseline]:
    return list(
        db.scalars(
            select(VisualBaseline)
            .where(VisualBaseline.project_id == project.id)
            .order_by(VisualBaseline.case_id, VisualBaseline.name)
        ).all()
    )
