"""外部工单服务的请求构造与错误处理。"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.integrations import issues


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_github_issue_payload(monkeypatch):
    settings = Settings(
        github_token="gh-token",
        github_repo="acme/repo",
        github_api_url="https://api.github.com",
        github_labels="shakedown, bug",
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response(201, {"number": 42, "html_url": "https://github.com/acme/repo/issues/42"})

    monkeypatch.setattr(issues.httpx, "post", fake_post)
    result = issues.create_github_issue(
        {"title": "[Shakedown][P0] 鉴权缺失", "body": "证据", "finding_ids": [1]},
        settings,
    )
    assert result["external_key"] == "42"
    assert captured["url"] == "https://api.github.com/repos/acme/repo/issues"
    assert captured["headers"]["Authorization"] == "Bearer gh-token"
    assert captured["json"]["labels"] == ["shakedown", "bug"]


def test_jira_issue_payload(monkeypatch):
    settings = Settings(
        jira_base_url="https://team.atlassian.net",
        jira_email="dev@example.com",
        jira_api_token="jira-token",
        jira_project_key="SEC",
        jira_issue_type="Bug",
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response(201, {"key": "SEC-7"})

    monkeypatch.setattr(issues.httpx, "post", fake_post)
    result = issues.create_jira_issue(
        {"title": "[Shakedown][P0] 鉴权缺失", "body": "证据", "finding_ids": [1]},
        settings,
    )
    assert result["external_key"] == "SEC-7"
    assert result["external_url"].endswith("/browse/SEC-7")
    assert captured["auth"] == ("dev@example.com", "jira-token")
    assert captured["json"]["fields"]["project"]["key"] == "SEC"
    assert captured["json"]["fields"]["issuetype"]["name"] == "Bug"


def test_project_targets_override_global_defaults(monkeypatch):
    monkeypatch.setattr(
        issues,
        "effective_settings",
        lambda: Settings(
            github_token="token",
            github_repo="global/repo",
            github_labels="global",
            jira_base_url="https://team.atlassian.net",
            jira_email="dev@example.com",
            jira_api_token="jira-token",
            jira_project_key="GLOBAL",
            jira_issue_type="Task",
        ),
    )

    class Project:
        settings_json = {
            "integrations": {
                "github": {"repo": "project/repo", "labels": "project-label"},
                "jira": {"project_key": "PROJ", "issue_type": "Bug"},
            }
        }

    targets = issues.project_integration_targets(Project())
    assert targets["github"]["repo"] == "project/repo"
    assert targets["github"]["labels"] == "project-label"
    assert targets["jira"]["project_key"] == "PROJ"
    assert targets["jira"]["issue_type"] == "Bug"
    status = issues.integration_status(Project())
    assert status["github"]["configured"] is True
    assert status["github"]["source"] == "project"
    assert status["jira"]["configured"] is True


def test_integration_not_configured():
    with pytest.raises(issues.IntegrationNotConfigured):
        issues.create_github_issue({"title": "x", "body": "y"}, Settings(github_token="", github_repo=""))
