"""AI 分诊：只写派生字段、按根因合并、不丢原始 finding。"""
from __future__ import annotations

from typing import Any

from app.agent.providers.base import LLMProvider, LLMResponse
from app.agent.triage import triage_findings
from app.db import session_scope
from app.models import Finding, Project


class FakeTriageProvider(LLMProvider):
    name = "fake"
    model = "fake-triage"
    supports_tools = True

    def __init__(self, groups: list[dict[str, Any]]):
        self.groups = groups

    def chat(self, messages, **kwargs) -> LLMResponse:
        return LLMResponse(content="{}")

    def structured(self, system, user, schema, *, context=None, **kwargs):
        return {"groups": self.groups}


def test_triage_preserves_raw_findings_and_compresses_singletons():
    with session_scope() as session:
        project = Project(name="triage", base_url="http://127.0.0.1:1")
        session.add(project)
        session.flush()
        findings = [
            Finding(
                project_id=project.id,
                source="whitebox",
                title="路径未校验 A",
                description="原始描述 A，不能被覆盖",
                severity="high",
                risk_type="入参未校验",
                code_location={"file_path": "web/a.py", "start_line": 10},
                evidence=[{"source_type": "code", "ref": "web/a.py:10"}],
                status="pending",
            ),
            Finding(
                project_id=project.id,
                source="whitebox",
                title="路径未校验 B",
                description="原始描述 B，不能被覆盖",
                severity="medium",
                risk_type="入参未校验",
                code_location={"file_path": "web/b.py", "start_line": 20},
                evidence=[{"source_type": "code", "ref": "web/b.py:20"}],
                status="pending",
            ),
            Finding(
                project_id=project.id,
                source="whitebox",
                title="AI 没覆盖的条目",
                description="fallback 也要保留",
                severity="low",
                risk_type="异常吞没",
                code_location={"file_path": "web/c.py", "start_line": 30},
                evidence=[],
                status="pending",
            ),
        ]
        session.add_all(findings)
        session.flush()
        ids = [item.id for item in findings]
        provider = FakeTriageProvider(
            [
                {
                    "theme_key": "path-check-a",
                    "title": "路径校验 A",
                    "verdict": "real_bug",
                    "severity": "high",
                    "confidence": 0.9,
                    "reason": "A 依据",
                    "recommended_action": "fix_code",
                    "classification": "environment",
                    "finding_ids": [ids[0]],
                },
                {
                    "theme_key": "path-check-b",
                    "title": "路径校验 B",
                    "verdict": "real_bug",
                    "severity": "medium",
                    "confidence": 0.8,
                    "reason": "B 依据",
                    "recommended_action": "fix_code",
                    "finding_ids": [ids[1]],
                },
            ]
        )
        result = triage_findings(session, project, provider)
        assert result["findings"] == 3
        assert findings[0].title == "路径未校验 A"
        assert findings[0].description == "原始描述 A，不能被覆盖"
        assert findings[0].triage_status == "ai_triaged"
        assert findings[0].triage["classification"] == "environment"
        # 两个单条发现按 模块+类别+判定 合组
        assert findings[0].cluster_key == findings[1].cluster_key == "web:single:real_bug:input"
        # AI 没覆盖的条目保留并标未分诊
        assert findings[2].triage_status == "untriaged"
        assert findings[2].cluster_key == "web:untriaged:exception"
        assert findings[2].description == "fallback 也要保留"
