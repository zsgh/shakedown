"""生成用例的批次超时 / 断点续跑。"""
from __future__ import annotations

import time
from typing import Any

import pytest

from app.agent.agents import _invalid_cases, _normalize_generated_case, _project_sla, generate_cases
from app.agent.providers.base import LLMProvider, LLMResponse
from app.db import session_scope
from app.models import Feature, Project, Task
from app.models import TestCase as CaseModel
from app.worker import handlers


class RecordingProvider(LLMProvider):
    name = "recording"
    model = "recording"
    supports_tools = False

    def __init__(self, sleep: float = 0.0, slow_calls: int = 0):
        self.calls = 0
        self.sleep = sleep
        self.slow_calls = slow_calls

    def chat(self, messages, **kwargs) -> LLMResponse:
        return LLMResponse(content="{}")

    def structured(self, system, user, schema, *, context=None, **kwargs):
        self.calls += 1
        if self.sleep and (not self.slow_calls or self.calls <= self.slow_calls):
            time.sleep(self.sleep)
        return {"cases": []}


def _project_with_features(session, count: int = 5) -> Project:
    project = Project(name="gen-resilience", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    for index in range(count):
        session.add(
            Feature(
                project_id=project.id,
                name=f"功能 {index}",
                module="mod",
                description="",
                source_refs=[],
                endpoint_ids=[],
                unmapped=False,
            )
        )
    session.flush()
    return project


def test_generate_cases_batch_timeout_does_not_block_forever():
    provider = RecordingProvider(sleep=1.0)
    with session_scope() as session:
        project = _project_with_features(session, count=1)
        started = time.time()
        progress: list[dict[str, Any]] = []
        cases = generate_cases(
            session,
            project,
            provider,
            dimensions=["functional"],
            job_timeout=0.15,
            on_progress=progress.append,
        )
        elapsed = time.time() - started
    assert cases == []
    assert elapsed < 0.9, f"批次超时没有生效，耗时 {elapsed:.2f}s"
    assert progress[-1]["failed"] == 1
    assert progress[-1]["total"] == 1


def test_generate_cases_keeps_making_progress_after_a_wave_times_out():
    """第一波 4 个调用挂住并超时后，后面的 wave 不能被泄漏线程堵死。"""
    provider = RecordingProvider(sleep=0.5, slow_calls=4)
    with session_scope() as session:
        project = _project_with_features(session, count=8)
        progress: list[dict[str, Any]] = []
        started = time.time()
        generate_cases(
            session,
            project,
            provider,
            dimensions=["functional"],
            batch_size=1,
            job_timeout=0.12,
            on_progress=progress.append,
        )
        elapsed = time.time() - started
    assert elapsed < 0.8, f"后续批次被超时线程堵住了，耗时 {elapsed:.2f}s"
    assert progress[-1]["done"] == 4
    assert progress[-1]["failed"] == 4


def test_project_sla_fallback_accepts_configured_threshold():
    from types import SimpleNamespace

    project = SimpleNamespace(settings_json={"performance_sla_ms": 500})
    assert _project_sla(project)["threshold_ms"] == 500.0
    assert _project_sla(SimpleNamespace(settings_json={"performance_sla_ms": ""})) == {}
    assert _project_sla(SimpleNamespace(settings_json={"performance_sla_ms": -1})) == {}


def test_generate_cases_only_missing_dimensions_skips_covered_feature_dimension():
    provider = RecordingProvider()
    with session_scope() as session:
        project = _project_with_features(session, count=2)
        features = session.query(Feature).filter_by(project_id=project.id).order_by(Feature.id).all()
        session.add(
            CaseModel(
                project_id=project.id,
                feature_id=features[0].id,
                title="已有功能用例",
                dimension="functional",
                case_type="api",
                spec={"title": "已有功能用例", "dimension": "functional", "steps": [], "assertions": []},
            )
        )
        session.commit()
        generate_cases(
            session,
            project,
            provider,
            dimensions=["functional"],
            batch_size=4,
            only_missing_dimensions=True,
        )
        assert provider.calls == 1


def test_generate_cases_resumes_completed_batches():
    provider = RecordingProvider()
    with session_scope() as session:
        project = _project_with_features(session, count=8)
        progress: list[dict[str, Any]] = []
        done_keys: set[str] = set()

        def record(event: dict[str, Any]) -> None:
            progress.append(event)
            if event.get("key") and event.get("ok"):
                done_keys.add(str(event["key"]))

        generate_cases(
            session,
            project,
            provider,
            dimensions=["functional"],
            batch_size=4,
            on_progress=record,
        )
        first_calls = provider.calls
        assert first_calls >= 1
        assert len(done_keys) == 2

        generate_cases(
            session,
            project,
            provider,
            dimensions=["functional"],
            batch_size=4,
            completed_keys=done_keys,
            on_progress=record,
        )
        assert provider.calls == first_calls


def test_generated_case_precondition_strings_are_normalized():
    normalized = _normalize_generated_case(
        {
            "title": "健康检查",
            "dimension": "functional",
            "type": "api",
            "steps": [{"action": "request", "method": "GET", "path": "/api/health"}],
            "preconditions": ["服务已启动", {"name": "管理员", "role": "admin"}],
        }
    )
    assert normalized["preconditions"][0]["kind"] == "manual"
    assert normalized["preconditions"][1]["kind"] == "http_login"
    assert _invalid_cases([normalized]) == []


class _EmptyProvider:
    def __init__(self):
        self.calls = 0

    def chat(self, *_args, **_kwargs):
        return LLMResponse(content="{}")

    def structured(self, *_args, **_kwargs):
        self.calls += 1
        return {"cases": []}


def test_handle_generate_cases_records_checkpoint_and_resumes(
    session, sample_project, monkeypatch
):
    feature = Feature(
        project_id=sample_project.id,
        name="登录",
        module="web",
        description="",
        source_refs=[],
        endpoint_ids=[],
        unmapped=False,
    )
    session.add(feature)
    task = Task(
        project_id=sample_project.id,
        kind="generate_cases",
        status="running",
        payload={"project_id": sample_project.id},
    )
    session.add(task)
    session.commit()

    provider = _EmptyProvider()
    monkeypatch.setattr(handlers, "get_provider", lambda: provider)
    payload = {
        "project_id": sample_project.id,
        "_task_id": task.id,
        "dimensions": ["functional"],
    }
    result = handlers.handle_generate_cases(session, payload)
    session.refresh(task)
    generate = task.checkpoint["generate"]
    assert result["batches_total"] == 1
    assert result["batches_done"] == 1
    assert generate["done_count"] == 1
    assert generate["failed_count"] == 0
    assert task.heartbeat_at is not None

    result = handlers.handle_generate_cases(session, payload)
    assert provider.calls == 1, "已完成批次不应再次调用模型"
    assert result["batches_done"] == 1


def test_handle_generate_cases_only_missing_skips_covered_features(
    session, sample_project, monkeypatch
):
    covered = Feature(
        project_id=sample_project.id,
        name="已有用例",
        module="web",
        description="",
        source_refs=[],
        endpoint_ids=[],
        unmapped=False,
    )
    missing = Feature(
        project_id=sample_project.id,
        name="缺少用例",
        module="web",
        description="",
        source_refs=[],
        endpoint_ids=[],
        unmapped=False,
    )
    session.add_all([covered, missing])
    session.flush()
    session.add(
        CaseModel(
            project_id=sample_project.id,
            feature_id=covered.id,
            title="已有用例",
            dimension="functional",
            case_type="api",
            spec={},
        )
    )
    task = Task(
        project_id=sample_project.id,
        kind="generate_cases",
        status="running",
        payload={"project_id": sample_project.id, "only_missing": True},
    )
    session.add(task)
    session.commit()

    provider = _EmptyProvider()
    monkeypatch.setattr(handlers, "get_provider", lambda: provider)
    result = handlers.handle_generate_cases(
        session,
        {
            "project_id": sample_project.id,
            "_task_id": task.id,
            "dimensions": ["functional"],
            "only_missing": True,
        },
    )
    session.refresh(task)
    generate = task.checkpoint["generate"]
    assert provider.calls == 1
    assert result["batches_total"] == 1
    assert generate["done_count"] == 1
    assert generate["done"] == [f"functional|{missing.id}"]


def test_handle_generate_cases_raises_to_retry_failed_batches(
    session, sample_project, monkeypatch
):
    feature = Feature(
        project_id=sample_project.id,
        name="登录",
        module="web",
        description="",
        source_refs=[],
        endpoint_ids=[],
        unmapped=False,
    )
    session.add(feature)
    task = Task(
        project_id=sample_project.id,
        kind="generate_cases",
        status="running",
        payload={"project_id": sample_project.id},
    )
    session.add(task)
    session.commit()

    provider = _EmptyProvider()
    provider.fail = True

    def structured(*args, **kwargs):
        provider.calls += 1
        if provider.fail:
            return {"__error__": "上游超时"}
        return {"cases": []}

    provider.structured = structured
    monkeypatch.setattr(handlers, "get_provider", lambda: provider)
    payload = {
        "project_id": sample_project.id,
        "_task_id": task.id,
        "dimensions": ["functional"],
    }
    with pytest.raises(RuntimeError, match="批次失败"):
        handlers.handle_generate_cases(session, payload)
    session.refresh(task)
    generate = task.checkpoint["generate"]
    assert generate["done_count"] == 0
    assert generate["failed_count"] == 1

    provider.fail = False
    result = handlers.handle_generate_cases(session, payload)
    session.refresh(task)
    generate = task.checkpoint["generate"]
    assert provider.calls == 2, "重试只应重跑失败批次"
    assert result["batches_done"] == 1
    assert generate["done_count"] == 1
    assert generate["failed_count"] == 0


def test_handle_generate_cases_stops_when_cancelled(session, sample_project, monkeypatch):
    feature = Feature(
        project_id=sample_project.id,
        name="登录",
        module="web",
        description="",
        source_refs=[],
        endpoint_ids=[],
        unmapped=False,
    )
    session.add(feature)
    task = Task(
        project_id=sample_project.id,
        kind="generate_cases",
        status="cancelled",
        payload={"project_id": sample_project.id},
    )
    session.add(task)
    session.commit()

    provider = _EmptyProvider()
    monkeypatch.setattr(handlers, "get_provider", lambda: provider)
    result = handlers.handle_generate_cases(
        session,
        {
            "project_id": sample_project.id,
            "_task_id": task.id,
            "dimensions": ["functional"],
        },
    )
    session.refresh(task)
    assert result["cancelled"] is True
    assert provider.calls == 0
    assert task.status == "cancelled"
    assert task.checkpoint["generate"]["cancelled"] is True
