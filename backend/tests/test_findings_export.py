"""缺陷修复清单导出：Markdown / JSON、分组、筛选与脱敏。"""
from __future__ import annotations

import json

from app.models import Finding


def _finding(
    project_id: int,
    *,
    title: str,
    cluster_key: str | None,
    verdict: str | None,
    severity: str = "high",
    status: str = "confirmed",
) -> Finding:
    triage = {}
    triage_status = "untriaged"
    if verdict:
        triage_status = "triaged"
        triage = {
            "verdict": verdict,
            "title": title,
            "group_title": title,
            "reason": "同一根因，建议集中修复。",
            "confidence": 0.8,
            "recommended_action": "fix_code" if verdict == "real_bug" else "review",
        }
    return Finding(
        project_id=project_id,
        source="whitebox",
        title=title,
        description="描述里有 Authorization: Bearer supersecret123，导出时必须脱敏。",
        severity=severity,
        status=status,
        module="internal/server",
        risk_type="越权",
        evidence=[
            {
                "source_type": "code",
                "ref": "internal/server/api.go:42",
                "quote": "Authorization: Bearer supersecret123",
            }
        ],
        code_location={
            "file_path": "internal/server/api.go",
            "start_line": 42,
            "end_line": 44,
            "snippet": "token=abc123\nif !authorized {\n  apiKey: $('#set-apikey').value.trim()\n  apiKey: \"sk-live-secret\"",
        },
        repro="curl -H 'Authorization: Bearer supersecret123' http://127.0.0.1:8888/api",
        cluster_key=cluster_key,
        triage_status=triage_status,
        triage=triage,
    )


def test_findings_export_markdown_groups_and_redacts(client, session, sample_project):
    first = _finding(sample_project.id, title="进程接口鉴权缺失", cluster_key="procs-auth", verdict="real_bug")
    second = _finding(
        sample_project.id,
        title="进程接口鉴权缺失（另一处）",
        cluster_key="procs-auth",
        verdict="real_bug",
        severity="medium",
    )
    false_positive = _finding(
        sample_project.id,
        title="已确认的误报",
        cluster_key="false-one",
        verdict="false_positive",
        status="false_positive",
    )
    untriaged = _finding(
        sample_project.id,
        title="尚未分诊",
        cluster_key=None,
        verdict=None,
        status="pending",
    )
    session.add_all([first, second, false_positive, untriaged])
    session.commit()

    response = client.get(f"/api/projects/{sample_project.id}/findings/export.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    text = response.text
    assert "# 缺陷修复清单" in text
    assert "进程接口鉴权缺失" in text
    assert "同一根因" in text
    assert "- [ ]" in text
    assert f"#{first.id}" in text
    assert "已确认的误报" not in text
    assert "尚未分诊" not in text
    assert "supersecret123" not in text
    assert "sk-live-secret" not in text
    assert "Bearer ***" in text
    assert "token=***" in text
    assert "apiKey: $('#set-apikey').value.trim()" in text
    assert 'apiKey: "***"' in text

    response = client.get(
        f"/api/projects/{sample_project.id}/findings/export.md?verdict=false_positive"
    )
    assert response.status_code == 200
    assert "已确认的误报" in response.text
    assert "进程接口鉴权缺失" not in response.text


def test_finding_integration_preview_create_and_idempotency(
    client, session, sample_project, monkeypatch
):
    finding = _finding(
        sample_project.id,
        title="进程接口鉴权缺失",
        cluster_key="procs-auth",
        verdict="real_bug",
    )
    session.add(finding)
    session.commit()

    url = f"/api/projects/{sample_project.id}/findings/integrations/github"
    preview = client.post(url, json={"group_key": "procs-auth", "dry_run": True})
    assert preview.status_code == 200
    assert preview.json()["status"] == "preview"
    assert "[Shakedown][P0]" in preview.json()["draft"]["title"]
    assert "原始发现" in preview.json()["draft"]["body"]
    assert client.get(f"/api/projects/{sample_project.id}/findings/integrations").json() == []

    def fake_create(provider, draft, project=None):
        assert provider == "github"
        assert draft["group_key"] == "procs-auth"
        assert project is not None
        return {"external_key": "17", "external_url": "https://github.com/acme/repo/issues/17"}

    monkeypatch.setattr("app.api.findings.create_issue", fake_create)
    created = client.post(url, json={"group_key": "procs-auth"})
    assert created.status_code == 200
    assert created.json()["status"] == "created"
    assert created.json()["external_key"] == "17"

    again = client.post(url, json={"group_key": "procs-auth"})
    assert again.status_code == 200
    assert again.json()["status"] == "existing"
    assert again.json()["external_url"].endswith("/issues/17")
    records = client.get(f"/api/projects/{sample_project.id}/findings/integrations").json()
    assert len(records) == 1
    assert records[0]["group_key"] == "procs-auth"


def test_findings_export_json_contains_structured_groups(client, session, sample_project):
    finding = _finding(
        sample_project.id,
        title="删除启动项缺少边界校验",
        cluster_key="delete-startup",
        verdict="real_bug",
        severity="critical",
    )
    session.add(finding)
    session.commit()

    response = client.get(
        f"/api/projects/{sample_project.id}/findings/export.json?verdict=all"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = json.loads(response.text)
    assert payload["version"] == "findings-export-v1"
    assert payload["project"]["id"] == sample_project.id
    assert payload["summary"]["findings"] == 1
    assert payload["summary"]["groups"] == 1
    group = payload["groups"][0]
    assert group["id"] == "delete-startup"
    assert group["priority"] == "P0"
    assert group["verdict"] == "real_bug"
    assert group["findings"][0]["id"] == finding.id
    assert "supersecret123" not in response.text
    assert group["findings"][0]["evidence"][0]["quote"].endswith("Bearer ***")
