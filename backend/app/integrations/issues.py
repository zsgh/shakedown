"""GitHub Issue / Jira 工单创建。

只负责把「修复组」变成外部工单；幂等记录由 API 层的 IntegrationIssue 表维护。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.reporting.findings_md import render_group_issue_markdown
from app.settings_store import effective_settings


class IntegrationError(RuntimeError):
    """外部工单服务返回错误。"""


class IntegrationNotConfigured(IntegrationError):
    """缺少必要配置。"""


def _labels(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _nonempty(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def project_integration_targets(project: Any) -> dict[str, Any]:
    """凭据来自系统设置，目标仓库 / Jira 项目优先取项目级配置。"""
    settings = effective_settings()
    settings_json = getattr(project, "settings_json", None) or {}
    if not isinstance(settings_json, dict):
        settings_json = {}
    raw = settings_json.get("integrations") or {}
    if not isinstance(raw, dict):
        raw = {}
    github_override = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    jira_override = raw.get("jira") if isinstance(raw.get("jira"), dict) else {}
    return {
        "github": {
            "token": settings.github_token,
            "api_url": settings.github_api_url,
            "repo": _nonempty(github_override.get("repo")) or settings.github_repo,
            "labels": (
                _nonempty(github_override.get("labels"))
                if "labels" in github_override
                else settings.github_labels
            ),
        },
        "jira": {
            "base_url": settings.jira_base_url,
            "email": settings.jira_email,
            "token": settings.jira_api_token,
            "project_key": _nonempty(jira_override.get("project_key")) or settings.jira_project_key,
            "issue_type": _nonempty(jira_override.get("issue_type")) or settings.jira_issue_type or "Bug",
        },
    }


def integration_status(project: Any = None) -> dict[str, Any]:
    if project is not None:
        targets = project_integration_targets(project)
    else:
        settings = effective_settings()
        targets = {
            "github": {
                "token": settings.github_token,
                "api_url": settings.github_api_url,
                "repo": settings.github_repo,
                "labels": settings.github_labels,
            },
            "jira": {
                "base_url": settings.jira_base_url,
                "email": settings.jira_email,
                "token": settings.jira_api_token,
                "project_key": settings.jira_project_key,
                "issue_type": settings.jira_issue_type,
            },
        }
    github = targets["github"]
    jira = targets["jira"]
    raw: Any = {}
    if project is not None:
        settings_json = getattr(project, "settings_json", None) or {}
        if isinstance(settings_json, dict):
            raw = settings_json.get("integrations", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "github": {
            "configured": bool(github["token"] and github["repo"]),
            "repo": github["repo"],
            "api_url": github["api_url"],
            "labels": github["labels"],
            "source": "project" if isinstance(raw.get("github"), dict) and raw["github"] else "global",
        },
        "jira": {
            "configured": bool(
                jira["base_url"] and jira["email"] and jira["token"] and jira["project_key"]
            ),
            "base_url": jira["base_url"],
            "project_key": jira["project_key"],
            "issue_type": jira["issue_type"],
            "source": "project" if isinstance(raw.get("jira"), dict) and raw["jira"] else "global",
        },
    }


def build_issue_draft(project_name: str, group: dict[str, Any]) -> dict[str, Any]:
    title = f"[Shakedown][{group['priority']}] {group['title']}"[:250]
    return {
        "title": title,
        "body": render_group_issue_markdown(group, project_name),
        "labels": ["shakedown"],
        "group_key": group["id"],
        "finding_ids": group["member_ids"],
    }


def _github_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def create_github_issue(
    draft: dict[str, Any],
    settings: Settings | None = None,
    *,
    repo: str | None = None,
    labels_raw: str | None = None,
) -> dict[str, Any]:
    settings = settings or effective_settings()
    target_repo = repo or settings.github_repo
    if not settings.github_token or not target_repo:
        raise IntegrationNotConfigured("GitHub 未配置：请填写 github_token，并在项目设置填写仓库")
    url = f"{settings.github_api_url.rstrip('/')}/repos/{target_repo}/issues"
    payload: dict[str, Any] = {"title": draft["title"], "body": draft["body"]}
    labels = _labels(settings.github_labels if labels_raw is None else labels_raw)
    if labels:
        payload["labels"] = labels
    try:
        response = httpx.post(url, headers=_github_headers(settings), json=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise IntegrationError(f"GitHub 请求失败：{exc}") from exc
    if response.status_code >= 400:
        raise IntegrationError(f"GitHub 返回 {response.status_code}：{response.text[:500]}")
    data = response.json()
    return {
        "external_key": str(data.get("number") or ""),
        "external_url": str(data.get("html_url") or ""),
        "raw": data,
    }


def create_jira_issue(
    draft: dict[str, Any],
    settings: Settings | None = None,
    *,
    project_key: str | None = None,
    issue_type: str | None = None,
) -> dict[str, Any]:
    settings = settings or effective_settings()
    target_key = project_key or settings.jira_project_key
    target_type = issue_type or settings.jira_issue_type or "Bug"
    required = [
        settings.jira_base_url,
        settings.jira_email,
        settings.jira_api_token,
        target_key,
    ]
    if not all(required):
        raise IntegrationNotConfigured(
            "Jira 未配置：请填写 jira_base_url、jira_email、jira_api_token，并在项目设置填写项目 Key"
        )
    url = f"{settings.jira_base_url.rstrip('/')}/rest/api/2/issue"
    payload = {
        "fields": {
            "project": {"key": target_key},
            "summary": draft["title"][:250],
            "description": draft["body"],
            "issuetype": {"name": target_type},
            "labels": ["shakedown"],
        }
    }
    try:
        response = httpx.post(
            url,
            auth=(settings.jira_email, settings.jira_api_token),
            json=payload,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise IntegrationError(f"Jira 请求失败：{exc}") from exc
    if response.status_code >= 400:
        raise IntegrationError(f"Jira 返回 {response.status_code}：{response.text[:500]}")
    data = response.json()
    key = str(data.get("key") or "")
    return {
        "external_key": key,
        "external_url": f"{settings.jira_base_url.rstrip('/')}/browse/{key}" if key else "",
        "raw": data,
    }


def create_issue(provider: str, draft: dict[str, Any], project: Any = None) -> dict[str, Any]:
    targets = project_integration_targets(project) if project is not None else None
    if provider == "github":
        return create_github_issue(
            draft,
            repo=targets["github"]["repo"] if targets else None,
            labels_raw=targets["github"]["labels"] if targets else None,
        )
    if provider == "jira":
        return create_jira_issue(
            draft,
            project_key=targets["jira"]["project_key"] if targets else None,
            issue_type=targets["jira"]["issue_type"] if targets else None,
        )
    raise IntegrationError(f"不支持的工单 provider：{provider}")


def sync_issue_status(provider: str, external_key: str, project: Any = None) -> str:
    """读取外部工单状态；只同步展示状态，不自动替被测系统判定缺陷已修复。"""
    settings = effective_settings()
    targets = project_integration_targets(project) if project is not None else None
    if provider == "github":
        repo = targets["github"]["repo"] if targets else settings.github_repo
        if not settings.github_token or not repo or not external_key:
            raise IntegrationNotConfigured("GitHub 未配置或工单号为空")
        url = f"{settings.github_api_url.rstrip('/')}/repos/{repo}/issues/{external_key}"
        try:
            response = httpx.get(url, headers=_github_headers(settings), timeout=30.0)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"GitHub 请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise IntegrationError(f"GitHub 返回 {response.status_code}：{response.text[:500]}")
        return str(response.json().get("state") or "unknown")
    if provider == "jira":
        base_url = settings.jira_base_url
        if not all([base_url, settings.jira_email, settings.jira_api_token, external_key]):
            raise IntegrationNotConfigured("Jira 未配置或工单 Key 为空")
        url = f"{base_url.rstrip('/')}/rest/api/2/issue/{external_key}"
        try:
            response = httpx.get(
                url,
                auth=(settings.jira_email, settings.jira_api_token),
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Jira 请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise IntegrationError(f"Jira 返回 {response.status_code}：{response.text[:500]}")
        fields = response.json().get("fields") or {}
        status = fields.get("status") or {}
        category = status.get("statusCategory") or {}
        return str(status.get("name") or category.get("name") or "unknown")
    raise IntegrationError(f"不支持的工单 provider：{provider}")
