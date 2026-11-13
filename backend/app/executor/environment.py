"""执行环境准备：按项目 / run 隔离原则准备被测库与地址。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.crypto import decrypt, decrypt_json
from app.logging import get_logger
from app.models import Project, Run
from app.sandbox.berth import BerthClient, BerthError

logger = get_logger("shakedown.executor.environment")

DB_TEMPLATE_BY_PROJECT = "postgres"


@dataclass
class Environment:
    base_url: str = ""
    target_dsn: str = ""
    read_dsn: str = ""
    accounts: dict[str, Any] = field(default_factory=dict)
    db_service: str = ""
    db_service_created: bool = False
    db_host: str = ""
    db_port: int | None = None
    berth_task_id: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "db_service": self.db_service,
            "db_service_created": self.db_service_created,
            "db_host": self.db_host,
            "db_port": self.db_port,
            "accounts": sorted(self.accounts.keys()),
            "notes": self.notes,
        }


def project_accounts(project: Project) -> dict[str, Any]:
    return decrypt_json(project.test_accounts_encrypted) or {}


def project_dsn(project: Project) -> str:
    return decrypt(project.target_dsn_encrypted) or ""


def project_read_dsn(project: Project) -> str:
    return decrypt(project.target_read_dsn_encrypted) or project_dsn(project)


def _service_name(project: Project, run: Run, isolation: dict[str, Any]) -> str:
    base = str(isolation.get("db_service") or f"{project.name}-db")
    if isolation.get("per_run"):
        return f"{base}-{run.id}"
    return base


def prepare_environment(session: Session, project: Project, run: Run) -> Environment:
    """准备执行环境：按需启动一次性的被测库（berth）并得到 DSN。"""
    env = Environment(base_url=project.base_url or "")
    env.accounts = project_accounts(project)
    env.target_dsn = project_dsn(project)
    env.read_dsn = project_read_dsn(project)

    isolation = project.isolation or {}
    service_name = _service_name(project, run, isolation)
    env.db_service = service_name

    if isolation.get("mode") == "reset" and (isolation.get("db_service") or isolation.get("per_run")):
        client = BerthClient()
        try:
            overrides = dict(isolation.get("overrides") or {})
            if isolation.get("db_port") and "port" not in overrides:
                overrides["port"] = isolation["db_port"]
            service = client.ensure_service(
                service_name,
                isolation.get("db_template") or DB_TEMPLATE_BY_PROJECT,
                **overrides,
            )
            env.db_service_created = True
            env.db_host = service.get("host", "localhost")
            env.db_port = service.get("port")
            env.berth_task_id = service.get("service_id", "")
            env.notes.append(f"berth 服务 {service_name} 已就绪（端口 {env.db_port}）")
            if not env.target_dsn:
                db_name = isolation.get("db_name") or "app"
                env.target_dsn = f"postgresql://postgres@127.0.0.1:{env.db_port}/{db_name}"
                env.read_dsn = env.read_dsn or env.target_dsn
        except BerthError as exc:
            env.notes.append(f"berth 起库失败（继续用项目已有 DSN）：{exc}")
    return env


def cleanup_environment(project: Project, env: Environment, *, drop: bool = True) -> list[str]:
    """只销毁本次 run 创建的服务，避免误删项目共享数据库。"""
    notes: list[str] = []
    if not env.db_service or not env.db_service_created:
        return notes
    client = BerthClient()
    try:
        client.delete_service(env.db_service, volumes=drop)
        notes.append(f"berth 服务 {env.db_service} 已销毁")
    except BerthError as exc:
        notes.append(f"berth 服务 {env.db_service} 销毁失败：{exc}")
    return notes
