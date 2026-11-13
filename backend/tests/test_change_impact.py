"""代码 / 文档结构快照与变更影响面测试。"""
from __future__ import annotations

from types import SimpleNamespace

from app.change_impact import (
    apply_change_impact,
    build_code_snapshot,
    build_markdown_snapshot,
    diff_snapshots,
    semantic_candidates,
)
from app.ingest.code import CodeChunk
from app.ingest.service import ingest_source
from app.models import Endpoint, Feature, KnowledgeSource, Run
from app.models import TestCase as CaseModel


def _code_chunk(content: str, *, symbol: str, start: int = 1) -> CodeChunk:
    return CodeChunk(
        content=content,
        file_path="api.py",
        start_line=start,
        end_line=start + 4,
        symbol=symbol,
        kind="function",
    )


def _route(method: str = "GET", path: str = "/api/orders") -> dict:
    return {
        "method": method,
        "path": path,
        "file_path": "api.py",
        "line": 2,
        "snippet": "@router.get('/api/orders')",
    }


def test_code_snapshot_diff_detects_route_handler_change():
    old = build_code_snapshot(
        "v1",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders")]},
    )
    new = build_code_snapshot(
        "v2",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return [1]", symbol="list_orders")]},
    )
    diff = diff_snapshots(old, new)
    assert diff["has_changes"] is True
    assert [item["key"] for item in diff["changed_routes"]] == ["GET /api/orders"]
    assert diff["changed_files"] == ["api.py"]


def test_code_snapshot_ignores_line_shift_when_handler_content_same():
    old = build_code_snapshot(
        "v1",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders", start=1)]},
    )
    new = build_code_snapshot(
        "v2",
        [{**_route(), "line": 12}],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders", start=10)]},
    )
    diff = diff_snapshots(old, new)
    assert diff["has_changes"] is False


def _md_chunk(file_path: str, section: list[str], text: str, role: str = "spec") -> SimpleNamespace:
    return SimpleNamespace(
        locator={
            "file_path": file_path,
            "section_path": section,
            "heading": section[-1] if section else "",
            "start_line": 1,
            "end_line": 10,
        },
        content=text,
        content_hash=f"hash:{text}",
        meta_json={"role": role},
    )


def test_markdown_snapshot_diff_detects_section_change():
    old = build_markdown_snapshot(
        "v1",
        [_md_chunk("docs/order.md", ["订单", "退款"], "退款仅支持未支付订单")],
        {"docs/order.md": "spec"},
    )
    new = build_markdown_snapshot(
        "v2",
        [_md_chunk("docs/order.md", ["订单", "退款"], "退款支持未支付和部分支付订单")],
        {"docs/order.md": "spec"},
    )
    diff = diff_snapshots(old, new)
    assert diff["has_changes"] is True
    assert diff["changed_sections"][0]["after"]["section"] == ["订单", "退款"]
    assert diff["changed_files"] == ["docs/order.md"]


def _source(session, project_id: int, kind: str) -> KnowledgeSource:
    source = KnowledgeSource(
        project_id=project_id,
        kind=kind,
        name=f"{kind}-source",
        location="/tmp/source",
        status="ready",
    )
    session.add(source)
    session.commit()
    return source


def _case_with_endpoint(session, project_id: int, source: KnowledgeSource) -> tuple[CaseModel, Endpoint, Feature]:
    endpoint = Endpoint(
        project_id=project_id,
        method="GET",
        path="/api/orders",
        summary="",
        params={},
        request_body={},
        responses={},
        security={},
        examples={},
        sources=[{"type": "code", "ref": "api.py:2", "source_id": source.id}],
    )
    session.add(endpoint)
    session.flush()
    feature = Feature(
        project_id=project_id,
        name="订单查询",
        module="orders",
        endpoint_ids=[endpoint.id],
        source_refs=[{"source_type": "code", "ref": "api.py:2"}],
    )
    session.add(feature)
    session.flush()
    case = CaseModel(
        project_id=project_id,
        feature_id=feature.id,
        title="查询订单列表",
        dimension="functional",
        case_type="api",
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/orders"}], "assertions": []},
        steps=[{"action": "request", "method": "GET", "path": "/api/orders"}],
        status="enabled",
    )
    session.add(case)
    session.commit()
    return case, endpoint, feature


def test_code_change_marks_directly_dependent_case_needs_review(session, sample_project):
    source = _source(session, sample_project.id, "code")
    case, _, _ = _case_with_endpoint(session, sample_project.id, source)
    old = build_code_snapshot(
        "v1",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders")]},
    )
    new = build_code_snapshot(
        "v2",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return [1]", symbol="list_orders")]},
    )

    result = apply_change_impact(session, sample_project, source, old, new, semantic=False)
    session.commit()
    session.refresh(case)
    assert result["direct"] == 1
    assert case.status == "needs_review"
    assert case.spec["change_impact"]["level"] == "direct"
    assert "change-impact" in case.tags


def test_doc_change_maps_feature_to_related_case(session, sample_project):
    source = _source(session, sample_project.id, "markdown")
    case, _, feature = _case_with_endpoint(session, sample_project.id, source)
    feature.source_refs = [{"source_type": "doc", "ref": "docs/order.md#退款"}]
    session.commit()
    old = build_markdown_snapshot(
        "v1",
        [_md_chunk("docs/order.md", ["订单", "退款"], "退款仅支持未支付订单")],
        {"docs/order.md": "spec"},
    )
    new = build_markdown_snapshot(
        "v2",
        [_md_chunk("docs/order.md", ["订单", "退款"], "退款支持未支付和部分支付订单")],
        {"docs/order.md": "spec"},
    )

    result = apply_change_impact(session, sample_project, source, old, new, semantic=False)
    session.commit()
    session.refresh(case)
    assert result["related"] == 1
    assert case.status == "needs_review"
    assert case.spec["change_impact"]["level"] == "related"


def test_note_change_generates_semantic_candidate_without_hard_marking(session, sample_project):
    source = _source(session, sample_project.id, "markdown")
    case, _, feature = _case_with_endpoint(session, sample_project.id, source)
    feature.source_refs = [{"source_type": "doc", "ref": "docs/order.md"}]
    session.commit()
    old = build_markdown_snapshot(
        "v1",
        [_md_chunk("docs/order.md", ["业务说明", "退款"], "退款仅支持未支付订单", role="notes")],
        {"docs/order.md": "notes"},
    )
    new = build_markdown_snapshot(
        "v2",
        [_md_chunk("docs/order.md", ["业务说明", "退款"], "退款支持未支付和部分支付订单", role="notes")],
        {"docs/order.md": "notes"},
    )

    result = apply_change_impact(session, sample_project, source, old, new, semantic=True)
    session.commit()
    session.refresh(case)
    assert result["candidates"]
    assert case.status == "enabled"
    assert case.spec["change_candidates"][0]["case_id"] == case.id


def test_semantic_candidates_returns_empty_without_changed_sections(session, sample_project):
    source = _source(session, sample_project.id, "markdown")
    _case_with_endpoint(session, sample_project.id, source)
    assert semantic_candidates(session, sample_project, source, {"kind": "markdown"}) == []


def test_change_impact_api_lists_affected_case(client, session, sample_project):
    source = _source(session, sample_project.id, "code")
    case, _, _ = _case_with_endpoint(session, sample_project.id, source)
    old = build_code_snapshot(
        "v1",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders")]},
    )
    new = build_code_snapshot(
        "v2",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return [1]", symbol="list_orders")]},
    )
    apply_change_impact(session, sample_project, source, old, new, semantic=False)
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/change-impact").json()
    assert body["cases"][0]["id"] == case.id
    assert body["cases"][0]["change_impact"]["level"] == "direct"


def test_code_reparse_stores_snapshot_and_reports_route_change(session, sample_project, tmp_path):
    code = tmp_path / "main.py"
    code.write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/api/orders')\n"
        "def list_orders():\n"
        "    return []\n",
        encoding="utf-8",
    )
    source = KnowledgeSource(
        project_id=sample_project.id,
        kind="code",
        name="订单代码",
        location=str(tmp_path),
        status="pending",
    )
    session.add(source)
    session.commit()

    first = ingest_source(session, sample_project, source)
    assert "GET /api/orders" in first["_structure_snapshot"]["routes"]

    code.write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/api/orders')\n"
        "def list_orders():\n"
        "    return [1]\n",
        encoding="utf-8",
    )
    second = ingest_source(session, sample_project, source)
    assert second["change_impact"]["changed_files"] == ["main.py"]
    assert [item["key"] for item in second["change_impact"]["changed_routes"]] == ["GET /api/orders"]
    assert second["_structure_snapshot"]["routes"]["GET /api/orders"]["hash"] != first[
        "_structure_snapshot"
    ]["routes"]["GET /api/orders"]["hash"]


def test_markdown_reparse_stores_snapshot_and_reports_change(session, sample_project, tmp_path):
    doc = tmp_path / "order.md"
    doc.write_text("# 订单\n\n## 退款\n\n退款仅支持未支付订单。\n", encoding="utf-8")
    source = KnowledgeSource(
        project_id=sample_project.id,
        kind="markdown",
        name="订单文档",
        location=str(tmp_path),
        status="pending",
        meta_json={"recursive": True},
    )
    session.add(source)
    session.commit()

    first = ingest_source(session, sample_project, source)
    assert first["_structure_snapshot"]["kind"] == "markdown"
    assert source.meta_json["_structure_snapshot"]["kind"] == "markdown"
    assert "_structure_snapshot" not in source.meta  # API 不暴露内部大对象

    doc.write_text("# 订单\n\n## 退款\n\n退款支持未支付和部分支付订单。\n", encoding="utf-8")
    second = ingest_source(session, sample_project, source)
    assert second["change_impact"]["changed_files"]
    assert second["change_impact"]["baseline"] is False
    assert source.meta_json["change_impact"]["changed_files"]


def test_auto_rerun_impacted_creates_change_impact_run(session, sample_project):
    sample_project.settings_json = {"automation": {"auto_rerun_impacted": True, "auto_rerun_limit": 10}}
    source = _source(session, sample_project.id, "code")
    case, _, _ = _case_with_endpoint(session, sample_project.id, source)
    old = build_code_snapshot(
        "v1",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return []", symbol="list_orders")]},
    )
    new = build_code_snapshot(
        "v2",
        [_route()],
        {"api.py": [_code_chunk("def list_orders():\n    return [1]", symbol="list_orders")]},
    )

    result = apply_change_impact(session, sample_project, source, old, new, semantic=False)
    session.commit()
    assert result["auto_rerun"]["created"] is True
    run = session.get(Run, result["auto_rerun"]["run_id"])
    assert run is not None
    assert run.trigger == "change-impact"
    assert run.case_ids == [case.id]
    assert run.status == "queued"
