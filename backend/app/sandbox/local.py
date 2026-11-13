"""本地沙箱（开发 / 无 berth 时的降级路径）。

注意：本地模式没有 berth 的 overlayfs / cgroup / 网络隔离，
只在「berth 还没就绪」时用于打通链路；生产请用 berth。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.executor.compiler import materialize
from app.logging import get_logger
from app.sandbox.base import SandboxBackend, SandboxRequest, SandboxResult

logger = get_logger("shakedown.sandbox.local")


class LocalSandbox(SandboxBackend):
    name = "local"

    def run(self, request: SandboxRequest) -> SandboxResult:
        workdir = Path(tempfile.mkdtemp(prefix="shakedown-run-"))
        if request.command is not None:
            return self._run_command(request, workdir)
        materialize(request.files, workdir)
        (workdir / "results").mkdir(exist_ok=True)
        (workdir / "artifacts").mkdir(exist_ok=True)

        env = dict(os.environ)
        env.update({k: str(v) for k, v in request.env.items()})
        env["PYTHONPATH"] = f"{workdir}:{env.get('PYTHONPATH', '')}"

        cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "test_shakedown_cases.py"]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:  # pragma: no cover
            return SandboxResult(ok=False, error=f"无法启动 pytest：{exc}", exit_code=-1)

        cancelled = False
        deadline = time.monotonic() + request.timeout
        while proc.poll() is None:
            if request.should_cancel is not None and request.should_cancel():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if time.monotonic() > deadline:
                proc.kill()
                stdout, stderr = proc.communicate()
                return SandboxResult(
                    ok=False,
                    error=f"执行超时（>{request.timeout}s）",
                    exit_code=-1,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    task_id=request.task_id,
                    raw={"workdir": str(workdir), "cancelled": False},
                )
            time.sleep(0.5)
        stdout, stderr = proc.communicate()
        if cancelled:
            artifacts = [
                str(p.relative_to(workdir))
                for p in workdir.rglob("*")
                if p.is_file() and ("artifacts" in p.parts or "results" in p.parts)
            ]
            return SandboxResult(
                ok=False,
                error="执行已取消",
                exit_code=-9,
                stdout=stdout or "",
                stderr=stderr or "",
                artifacts=artifacts,
                task_id=request.task_id,
                raw={"workdir": str(workdir), "cancelled": True},
            )
        artifacts = [
            str(p.relative_to(workdir))
            for p in workdir.rglob("*")
            if p.is_file() and ("artifacts" in p.parts or "results" in p.parts)
        ]
        return SandboxResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            task_id=request.task_id,
            duration=0.0,
            raw={"workdir": str(workdir)},
        )

    def _run_command(self, request: SandboxRequest, workdir: Path) -> SandboxResult:
        """本地模式：没有 cgroup / 网络隔离，仅用于开发链路，结果会标记 local。"""
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in request.env.items()})
        cwd = request.cwd or str(workdir)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                list(request.command or []),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=request.timeout,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(ok=False, error=f"执行超时（>{request.timeout}s）", exit_code=-1, task_id=request.task_id, raw={"execution": "local"})
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(ok=False, error=f"{type(exc).__name__}: {exc}", exit_code=-1, task_id=request.task_id, raw={"execution": "local"})
        return SandboxResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            task_id=request.task_id,
            duration=round(time.perf_counter() - started, 3),
            raw={"execution": "local", "cwd": cwd},
        )

    def collect_results(self, workdir: str) -> list[dict]:
        import json

        results = []
        for path in sorted(Path(workdir).glob("results/*.json")):
            try:
                results.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return results
