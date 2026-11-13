"""API 路由。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import agent, automation, cases, exceptions, findings, gaps, health, knowledge, mcp, members, metrics, projects, push, quality, reports, review, runs, schedules, security, settings, sla, test_data, visual

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(projects.router)
api_router.include_router(members.router)
api_router.include_router(knowledge.router)
api_router.include_router(cases.router)
api_router.include_router(gaps.router)
api_router.include_router(review.router)
api_router.include_router(push.router)
api_router.include_router(runs.router)
api_router.include_router(schedules.router)
api_router.include_router(exceptions.router)
api_router.include_router(sla.router)
api_router.include_router(automation.router)
api_router.include_router(visual.router)
api_router.include_router(quality.router)
api_router.include_router(metrics.router)
api_router.include_router(findings.router)
api_router.include_router(agent.router)
api_router.include_router(reports.router)
api_router.include_router(settings.router)
api_router.include_router(security.router)
api_router.include_router(test_data.router)
api_router.include_router(mcp.router)

__all__ = ["api_router"]
