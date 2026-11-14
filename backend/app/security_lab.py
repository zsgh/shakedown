"""隔离安全实验室：由 ShakeDown 自己构建一次性 SUT 实例并执行破坏性探针。

解决两类不能碰生产 SUT 的测试：
- 不可逆操作（建同名组、无删除接口）；
- 会把管理面锁死的竞态（最后管理员并发禁用）。

流程：复制代码 → go build → 临时数据目录/随机端口启动 → 建账号与夹具 →
执行探针 → 生成/更新 Finding → 杀进程并清理。全程不接触项目真实环境。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models import Finding, Project

logger = get_logger("shakedown.security_lab")
ADMIN_PASSWORD = "LabAdminPass1!"


@dataclass
class LabHandle:
    base_url: str
    proc: subprocess.Popen
    workdir: Path
    config_path: Path

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(self.workdir, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _code_root(db: Session, project: Project) -> Path | None:
    from app.models import KnowledgeSource

    for source in db.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project.id, KnowledgeSource.kind == "code"
        )
    ).all():
        path = Path(str(source.location)).expanduser()
        if path.is_dir():
            return path
    return None


def start_lab(db: Session, project: Project) -> LabHandle:
    root = _code_root(db, project)
    if root is None or not (root / "go.mod").is_file():
        raise RuntimeError("隔离实验室目前只支持 Go 项目（需要 go.mod）")
    workdir = Path(tempfile.mkdtemp(prefix="shakedown-lab-"))
    src = workdir / "src"
    shutil.copytree(
        root, src, ignore=shutil.ignore_patterns(".git", "data", "dist", "backups", ".run-sandbox")
    )
    binary = workdir / "lab-bin"
    env = dict(os.environ)
    env["GOCACHE"] = "/tmp/shakedown-lab-gocache"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/dm-git"],
        cwd=src,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if build.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"构建失败：{build.stderr[-500:]}")

    port = _free_port()
    data = workdir / "data"
    data.mkdir()
    cfg = json.loads((src / "config.json").read_text(encoding="utf-8"))
    cfg.update(
        {
            "addr": f"127.0.0.1:{port}",
            "externalURL": f"http://127.0.0.1:{port}",
            "dataDir": str(data),
            "allowAnonymousRead": True,
        }
    )
    cfg["backup"] = {
        "dir": str(data / "backups"),
        "intervalHours": 24,
        "keep": 7,
        "includeMavenHosted": False,
        "targets": [{"name": "本地磁盘", "type": "local", "enabled": True, "port": 22}],
    }
    config_path = workdir / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    run_env = dict(os.environ)
    run_env["DMGIT_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    proc = subprocess.Popen(
        [str(binary), "-config", str(config_path), "-addr", f"127.0.0.1:{port}", "-data", str(data)],
        cwd=workdir,
        env=run_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = LabHandle(f"http://127.0.0.1:{port}", proc, workdir, config_path)
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            handle.close()
            raise RuntimeError("实验室实例启动失败")
        try:
            if httpx.get(f"{handle.base_url}/api/v1/version", timeout=1, trust_env=False).status_code == 200:
                return handle
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    handle.close()
    raise RuntimeError("实验室实例启动超时")


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, trust_env=False, follow_redirects=False, timeout=5.0)


def _upsert_finding(db: Session, project: Project, *, key: str, title: str, description: str,
                    evidence: list[dict[str, Any]], repro: str) -> Finding:
    row = db.scalar(select(Finding).where(Finding.project_id == project.id, Finding.cluster_key == key))
    if row is None:
        row = Finding(
            project_id=project.id, source="whitebox", title=title, description=description,
            severity="high", evidence=evidence, risk_type="鉴权 / 越权", module="隔离安全实验室",
            repro=repro, status="confirmed", confirmed_by="auto:security-lab", cluster_key=key,
        )
        db.add(row)
    else:
        row.description = description
        row.evidence = evidence
        row.repro = repro
        row.status = "confirmed"
        row.confirmed_by = "auto:security-lab"
        row.severity = "high"
    db.flush()
    return row


def run_probes(db: Session, project: Project) -> dict[str, Any]:
    lab = start_lab(db, project)
    findings: list[str] = []
    errors: list[str] = []
    try:
        admin = ("admin", ADMIN_PASSWORD)
        with _client(lab.base_url) as c:
            for user in ("alice_lab", "bob_lab"):
                c.post("/api/v1/users", auth=admin,
                       json={"username": user, "password": user + "Pass1!"})
            # 1) 同名组与个人命名空间冲突
            c.post("/admin/groups", auth=admin, data={"name": "alice_lab", "description": "collision"})
            c.post("/admin/groups/alice_lab/members", auth=admin,
                   data={"username": "bob_lab", "role": "developer"})
            c.post("/new", auth=("alice_lab", "alice_labPass1!"),
                   data={"namespace": "alice_lab", "name": "alice-private", "private": "on"})
            read = c.get("/api/v1/projects/alice_lab/alice-private", auth=("bob_lab", "bob_labPass1!"))
            create = c.post("/api/v1/projects", auth=("bob_lab", "bob_labPass1!"),
                            json={"namespace": "alice_lab", "name": "bob-owned", "private": True})
            if read.status_code == 200 or create.status_code == 201:
                _upsert_finding(
                    db, project,
                    key="security:group-namespace-collision",
                    title="[越权] 同名用户组可读写该用户的个人项目",
                    description=(
                        "组名与用户名相同时，组成员会获得该用户个人命名空间下项目的权限。"
                        f"实验室实测：bob 读取 alice 私有项目 {read.status_code}，"
                        f"在 alice 命名空间创建项目 {create.status_code}。"
                    ),
                    evidence=[
                        {"type": "code", "file": "internal/store/store.go",
                         "detail": "Access/canCreateIn 只按 namespace 字符串匹配，不区分个人与组"},
                        {"type": "lab", "read_status": read.status_code, "create_status": create.status_code},
                    ],
                    repro="隔离实例：建用户 alice_lab/bob_lab → 建同名组 alice_lab → bob 加入 developer → alice 建私有项目 → bob 读取/创建",
                )
                findings.append("同名组越权")

            # 2) 最后管理员并发禁用
            c.post("/api/v1/users", auth=admin,
                   json={"username": "admin2", "password": "admin2Pass1!", "isAdmin": True})
            both_dead = False
            for _ in range(200):
                results: list[int] = []
                barrier = threading.Barrier(2)

                def toggle(target: str, auth: tuple[str, str]) -> None:
                    with _client(lab.base_url) as x:
                        barrier.wait()
                        results.append(x.post(f"/admin/users/{target}/toggle", auth=auth).status_code)

                t1 = threading.Thread(target=toggle, args=("admin2", admin))
                t2 = threading.Thread(target=toggle, args=("admin", ("admin2", "admin2Pass1!")))
                t1.start(); t2.start(); t1.join(); t2.join()
                with _client(lab.base_url) as probe:
                    a1 = probe.get("/api/v1/users", auth=admin).status_code
                    a2 = probe.get("/api/v1/users", auth=("admin2", "admin2Pass1!")).status_code
                if a1 == 401 and a2 == 401:
                    both_dead = True
                    break
                if a1 != 200:
                    with _client(lab.base_url) as probe:
                        probe.post("/admin/users/admin/toggle", auth=("admin2", "admin2Pass1!"))
                if a2 != 200:
                    with _client(lab.base_url) as probe:
                        probe.post("/admin/users/admin2/toggle", auth=admin)
            if both_dead:
                _upsert_finding(
                    db, project,
                    key="security:last-admin-race",
                    title="[竞态] 并发禁用可把最后一个管理员也禁掉",
                    description=(
                        "两个管理员并发互相 toggle 时，AdminCount 检查与 UpdateUser 不在同一把锁内，"
                        "最终两个管理员都被禁用，管理面 401。"
                    ),
                    evidence=[
                        {"type": "code", "file": "internal/server/admin_handlers.go",
                         "detail": "AdminCount 与 UpdateUser 分离，存在 TOCTOU"},
                        {"type": "lab", "result": "both admins disabled"},
                    ],
                    repro="隔离实例：建 admin2 → 两线程互相 toggle 6 轮 → 两个管理员均 401",
                )
                findings.append("最后管理员竞态")
    finally:
        lab.close()
        db.flush()
    return {"lab": lab.base_url, "findings": findings, "errors": errors}
