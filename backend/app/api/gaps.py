"""用例依据缺口 API：统一列出待确认断言 / 无步骤 / 待补全说明。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError
from app.gaps import (
    accept_pending_assertion_risk,
    batch_set_case_gap_state,
    collect_case_gaps,
    confirm_pending_assertions,
    filter_gaps,
    gap_facets,
    gap_summary,
    group_pending_assertions,
    list_case_gap_audits,
    render_gaps_markdown,
    set_case_gap_state,
)
from app.models import Project
from app.schemas import (
    AssertionBatchConfirm,
    AssertionBatchRisk,
    CaseGapBatchUpdate,
    CaseGapUpdate,
)

router = APIRouter(tags=["gaps"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


def _emit(db: Session, project: Project, event: str, data: dict[str, Any]) -> None:
    """推送失败不能影响缺口处置本身。"""
    try:
        from app.push import queue_event

        if queue_event(db, project, event, data) is not None:
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


@router.get("/projects/{project_id}/case-gaps")
def list_case_gaps(
    project_id: int,
    kind: str = Query(default="all", pattern="^(all|pending|questions|no_steps|missing_input)$"),
    include_disabled: bool = Query(default=False),
    include_closed: bool = Query(default=False),
    dimension: str | None = Query(default=None),
    feature_id: int | None = Query(default=None),
    evidence_source: str | None = Query(default=None),
    owner: str | None = Query(default=None),
    missing_input_kind: str | None = Query(default=None),
    owner_kind: str | None = Query(default=None, pattern="^(test-side|sut-contract|environment)$"),
    gap_status: str | None = Query(default=None),
    execution_state: str | None = Query(
        default=None,
        pattern="^(never|executed|never_executed|enabled_no_result|pending_not_enabled|stale_after_source)$",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """统一返回需要补依据 / 补步骤的用例，供「契约缺口」页和报告导出使用。"""
    project = _project(db, project_id)
    items = collect_case_gaps(db, project, include_disabled=include_disabled)
    return {
        "summary": gap_summary(items),
        "facets": gap_facets(items),
        "items": filter_gaps(
            items,
            kind,
            include_closed=include_closed,
            dimension=dimension,
            feature_id=feature_id,
            evidence_source=evidence_source,
            owner=owner,
            missing_input_kind=missing_input_kind,
            owner_kind=owner_kind,
            gap_status=gap_status,
            execution_state=execution_state,
        ),
    }


@router.patch("/projects/{project_id}/case-gaps/{case_id}")
def update_case_gap(
    project_id: int,
    case_id: int,
    payload: CaseGapUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """处置一个契约缺口：认领 / 标记已修复 / 接受风险 / 不修复 / 重新打开。"""
    project = _project(db, project_id)
    try:
        state = set_case_gap_state(
            db,
            project,
            case_id,
            status=payload.status,
            owner=payload.owner,
            note=payload.note,
            actor=payload.actor,
            source="manual",
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    _emit(
        db,
        project,
        "case_gap.updated",
        {
            "case_id": state.case_id,
            "status": state.status,
            "owner": state.owner,
            "note": state.note,
        },
    )
    return {
        "case_id": state.case_id,
        "status": state.status,
        "owner": state.owner,
        "note": state.note,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


@router.post("/projects/{project_id}/case-gaps/batch")
def batch_update_case_gaps(
    project_id: int,
    payload: CaseGapBatchUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按筛选条件批量处置缺口。

    默认 ``dry_run=true`` 只返回将影响的用例数量和样例，前端二次确认后再传
    ``dry_run=false`` 执行。
    """
    project = _project(db, project_id)
    filters = payload.filters.model_dump()
    items = collect_case_gaps(db, project, include_disabled=filters["include_disabled"])
    matched = filter_gaps(
        items,
        filters["kind"],
        include_closed=filters["include_closed"],
        dimension=filters.get("dimension"),
        feature_id=filters.get("feature_id"),
        evidence_source=filters.get("evidence_source"),
        owner=filters.get("owner"),
        missing_input_kind=filters.get("missing_input_kind"),
        owner_kind=filters.get("owner_kind"),
        gap_status=filters.get("gap_status"),
        execution_state=filters.get("execution_state"),
    )
    if payload.case_ids:
        wanted = set(payload.case_ids)
        matched = [item for item in matched if item["case_id"] in wanted]
    if payload.dry_run:
        return {
            "dry_run": True,
            "matched": len(matched),
            "status": payload.status,
            "owner": payload.owner or "",
            "note": payload.note or "",
            "cases": [
                {
                    "case_id": item["case_id"],
                    "title": item["title"],
                    "gap_status": item["gap_status"],
                    "missing_input_kind": item.get("missing_input_kind"),
                    "owner_kind": item.get("owner_kind"),
                }
                for item in matched[:100]
            ],
        }
    try:
        result = batch_set_case_gap_state(
            db,
            project,
            filters=filters,
            status=payload.status,
            owner=payload.owner,
            note=payload.note,
            case_ids=payload.case_ids,
            actor=payload.actor,
            source="batch",
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    _emit(
        db,
        project,
        "case_gap.batch_updated",
        {
            "status": payload.status,
            "updated": result["updated"],
            "case_ids": result["case_ids"][:200],
            "filters": filters,
        },
    )
    return {"dry_run": False, **result}


@router.get("/projects/{project_id}/case-gaps/{case_id}/audits")
def case_gap_audits(
    project_id: int,
    case_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """单条用例的缺口处置审计（谁、何时、原因）。"""
    project = _project(db, project_id)
    return {"items": list_case_gap_audits(db, project, case_id, limit=limit)}


@router.get("/projects/{project_id}/case-gaps/assertions/groups")
def list_assertion_groups(
    project_id: int,
    group_by: str = Query(default="endpoint", pattern="^(endpoint|kind|evidence_source|feature|owner_kind)$"),
    include_closed: bool = Query(default=False),
    owner_kind: str | None = Query(default=None, pattern="^(test-side|sut-contract|environment)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按接口 / 断言类型 / 证据来源 / 功能点聚合待确认断言。"""
    project = _project(db, project_id)
    items = collect_case_gaps(db, project)
    items = filter_gaps(items, "pending", include_closed=include_closed, owner_kind=owner_kind)
    groups = group_pending_assertions(items, group_by)
    return {
        "group_by": group_by,
        "total": sum(group["count"] for group in groups),
        "confirmable": sum(group["confirmable"] for group in groups),
        "groups": groups,
    }


@router.post("/projects/{project_id}/case-gaps/assertions/confirm")
def batch_confirm_assertions(
    project_id: int,
    payload: AssertionBatchConfirm,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """批量确认有 openapi / code / doc 依据的断言；无依据的会被跳过。"""
    project = _project(db, project_id)
    return confirm_pending_assertions(
        db,
        project,
        [selection.model_dump() for selection in payload.selections],
        actor=payload.actor,
        note=payload.note,
    )


@router.post("/projects/{project_id}/case-gaps/assertions/accept-risk")
def batch_accept_assertion_risk(
    project_id: int,
    payload: AssertionBatchRisk,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """批量接受风险：把对应用例缺口标为 accepted 并写审计。"""
    project = _project(db, project_id)
    return accept_pending_assertion_risk(
        db,
        project,
        payload.case_ids,
        actor=payload.actor,
        note=payload.note,
    )


@router.get("/projects/{project_id}/case-gaps/export.md")
def export_case_gaps(
    project_id: int,
    include_disabled: bool = Query(default=False),
    include_closed: bool = Query(default=False),
    owner_kind: str | None = Query(default=None, pattern="^(test-side|sut-contract|environment)$"),
    db: Session = Depends(get_db),
) -> Response:
    """导出被测系统的契约缺口报告，供其维护者或授权的编码 Agent 处理。"""
    from urllib.parse import quote as urlquote

    project = _project(db, project_id)
    items = collect_case_gaps(db, project, include_disabled=include_disabled)
    items = filter_gaps(items, "all", include_closed=include_closed, owner_kind=owner_kind)
    markdown = render_gaps_markdown(project, items, owner_kind=owner_kind)
    filename = f"case-gaps-{project_id}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(filename)}"},
    )
