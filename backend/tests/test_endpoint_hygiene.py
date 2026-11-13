"""接口清理 / 幽灵接口 / 开发日志过滤 / 文档缺口降级的测试。"""
from __future__ import annotations

from app.ingest.consistency import extract_doc_mentions
from app.ingest.docapi import extract_doc_endpoints, is_dev_log


def test_is_dev_log():
    assert is_dev_log("/repo/todo.md")
    assert is_dev_log("docs/changelog.md")
    assert is_dev_log("开发日志.md")
    assert not is_dev_log("docs/api.md")
    assert not is_dev_log("docs/DESIGN.md")


def test_dev_log_not_used_as_api_spec():
    """开发日志里的接口提及不参与接口抽取。"""
    chunks = [
        {
            "id": 1,
            "content": "修：helper 一次性注册了 GET/POST/PUT/PATCH/DELETE 五个方法",
            "locator": {"file_path": "/repo/todo.md"},
        }
    ]
    assert extract_doc_endpoints(chunks) == []
    assert extract_doc_mentions(chunks) == []


def test_method_list_not_treated_as_path():
    chunks = [
        {
            "id": 1,
            "content": "支持 GET/POST/PUT/PATCH/DELETE 五种方法",
            "locator": {"file_path": "/repo/docs/api.md"},
        }
    ]
    assert extract_doc_endpoints(chunks) == []
    assert extract_doc_mentions(chunks) == []


def test_normal_doc_still_extracted():
    chunks = [
        {
            "id": 1,
            "content": "| GET | /api/users | 列表 |",
            "locator": {"file_path": "/repo/docs/api.md"},
        }
    ]
    assert extract_doc_endpoints(chunks)
    assert extract_doc_mentions(chunks)


def test_prune_endpoints_removes_ghosts(session, sample_project):
    """重新导入后，代码里不再存在的接口必须从清单里消失。"""
    from app.ingest.service import prune_endpoints, upsert_endpoint
    from app.models import Endpoint

    upsert_endpoint(session, sample_project.id, "GET", "/api/old", "code", ref="a.go:1", source_id=None)
    upsert_endpoint(session, sample_project.id, "GET", "/api/new", "code", ref="a.go:2", source_id=None)
    session.commit()
    assert session.query(Endpoint).filter_by(project_id=sample_project.id).count() == 2

    removed = prune_endpoints(session, sample_project.id, "code", {("GET", "/api/new")})
    session.commit()
    assert removed == 1
    paths = {e.path for e in session.query(Endpoint).filter_by(project_id=sample_project.id).all()}
    assert paths == {"/api/new"}


def test_prune_endpoints_keeps_other_sources(session, sample_project):
    """同一接口在别处还有来源时，只摘掉这一条来源，不能把接口删了。"""
    from app.ingest.service import prune_endpoints, upsert_endpoint
    from app.models import Endpoint

    upsert_endpoint(session, sample_project.id, "GET", "/api/x", "code", ref="a.go:1", source_id=None)
    upsert_endpoint(session, sample_project.id, "GET", "/api/x", "openapi", ref="#/paths/x/get", source_id=None)
    session.commit()

    prune_endpoints(session, sample_project.id, "code", set())
    session.commit()
    endpoint = session.query(Endpoint).filter_by(project_id=sample_project.id).one()
    assert [s["type"] for s in endpoint.sources] == ["openapi"]
