"""berth HTTP 客户端 + 沙箱后端。

berth 文档：https://github.com/zsgh/berth（docs/api.md）
本模块只做「把 HTTP 调干净 + 错误信息清晰」，不做编排。
"""
from __future__ import annotations

from typing import Any

import os
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings, get_settings
from app.logging import get_logger
from app.sandbox.base import SandboxBackend, SandboxError, SandboxRequest, SandboxResult

logger = get_logger("shakedown.sandbox.berth")

DEFAULT_BASE_URL = "http://localhost:9000"
BERTH_MAX_TIMEOUT = 600


class BerthError(SandboxError):
    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class BerthClient:
    """berth API 的薄封装。"""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        from app.settings_store import effective_settings

        settings = effective_settings()
        self.base_url = (base_url or settings.berth_base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.berth_token
        self.timeout = timeout

    # ── 基础 ──────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            # B1（API 鉴权）尚未落地，先按约定用这两个头，任选其一即可
            headers["X-Berth-Token"] = self.token
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None, timeout: float | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                resp = client.request(method, url, json=json_body, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BerthError(f"berth 不可达（{self.base_url}）：{exc}") from exc
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text[:500]}
        if resp.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else data
            raise BerthError(f"berth 返回 {resp.status_code}：{detail}", status_code=resp.status_code, response=data)
        return data

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    # ── 脚本 ──────────────────────────────────────────────
    def run_script(
        self,
        code: str | None = None,
        *,
        cmd: list[str] | None = None,
        language: str = "python",
        image: str = "base",
        timeout: int = 60,
        task_id: str | None = None,
        memory_mb: int = 256,
        cpu_percent: int = 50,
        async_: bool = False,
        network: Any = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timeout": max(1, min(int(timeout), BERTH_MAX_TIMEOUT)),
            "image": image,
            "memory_mb": memory_mb,
            "cpu_percent": cpu_percent,
        }
        if code is not None:
            payload["code"] = code
            if language:
                payload["language"] = language
        elif cmd is not None:
            payload["cmd"] = list(cmd)
        else:
            raise ValueError("run_script 需要 code 或 cmd 之一")
        if task_id:
            payload["task_id"] = task_id
        if async_:
            payload["async"] = True
        if network is not None:
            payload["network"] = network
        return self._request("POST", "/scripts/run", json_body=payload, timeout=timeout + 30)

    def list_scripts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/scripts", params={"limit": limit})

    def get_script(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/scripts/{task_id}")

    def clear_script(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/scripts/{task_id}/clear")

    def cancel_script(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/scripts/{task_id}/cancel", timeout=30)

    # ── 长驻服务 ─────────────────────────────────────────
    def ensure_service(self, name: str, template: str, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"template": template}
        body.update({k: v for k, v in overrides.items() if v is not None})
        return self._request("PUT", f"/services/{name}", json_body=body, timeout=120)

    def get_service(self, ref: str) -> dict[str, Any]:
        return self._request("GET", f"/services/{ref}")

    def list_services(self, all_services: bool = False, status: str | None = None) -> list[dict[str, Any]]:
        params = {"all": "true" if all_services else None, "status": status}
        return self._request("GET", "/services", params={k: v for k, v in params.items() if v is not None})

    def stop_service(self, ref: str) -> dict[str, Any]:
        return self._request("POST", f"/services/{ref}/stop", timeout=60)

    def start_service(self, ref: str) -> dict[str, Any]:
        return self._request("POST", f"/services/{ref}/start", timeout=120)

    def delete_service(self, ref: str, volumes: bool = False) -> dict[str, Any]:
        return self._request("DELETE", f"/services/{ref}", params={"volumes": "true" if volumes else None}, timeout=120)

    def volumes(self) -> list[dict[str, Any]]:
        return self._request("GET", "/volumes")

    def exec_service(self, ref: str, cmd: list[str], *, timeout: int = 10) -> dict[str, Any]:
        """在服务的 mount/pid/net namespace 里执行命令（CLI 用例的替代通道）。"""
        return self._request(
            "POST",
            f"/services/{ref}/exec",
            json_body={"cmd": [str(item) for item in cmd], "timeout": max(1, min(int(timeout), 60))},
            timeout=70,
        )


class BerthSandbox(SandboxBackend):
    """把编译好的用例丢进 berth 沙箱执行。"""

    name = "berth"

    def __init__(self, client: BerthClient | None = None, settings: Settings | None = None):
        from app.settings_store import effective_settings

        self.settings = settings or effective_settings()
        self.client = client or BerthClient()

    def _run_async(self, request: SandboxRequest, **kwargs: Any) -> SandboxResult:
        """异步跑 + 轮询；取消时调用 berth 的 cancel 接口。"""
        try:
            started = self.client.run_script(async_=True, **kwargs)
        except BerthError as exc:
            return SandboxResult(ok=False, error=str(exc), exit_code=-1, raw={"execution": "berth"})
        task_id = str(started.get("task_id") or request.task_id)
        data = started
        while True:
            if request.should_cancel is not None and request.should_cancel():
                try:
                    self.client.cancel_script(task_id)
                except BerthError:
                    pass
                return SandboxResult(
                    ok=False,
                    exit_code=-9,
                    task_id=task_id,
                    error="执行已取消",
                    raw={"execution": "berth", "cancelled": True, **started},
                )
            status = str(data.get("status") or "")
            if status in {"finished", "failed", "cancelled", "canceled", "timeout", "killed"} or data.get("exit_code") is not None:
                break
            time.sleep(0.5)
            try:
                data = self.client.get_script(task_id)
            except BerthError as exc:
                return SandboxResult(ok=False, error=str(exc), exit_code=-1, task_id=task_id, raw={"execution": "berth"})
        return SandboxResult(
            ok=int(data.get("exit_code", -1)) == 0,
            exit_code=int(data.get("exit_code", -1)),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            artifacts=data.get("artifacts", []) or [],
            task_id=task_id,
            duration=float(data.get("duration", 0.0) or data.get("duration_ms", 0.0) or 0.0) / (1000.0 if data.get("duration_ms") else 1.0),
            raw={"execution": "berth", **data},
        )

    def run(self, request: SandboxRequest) -> SandboxResult:
        from app.executor.compiler import build_command

        if request.command is not None:
            command = [str(item) for item in request.command]
            if request.env:
                command = ["env", *[f"{key}={value}" for key, value in request.env.items()], *command]
            if request.cwd:
                # 用 sh -c 传参数数组，避免把 cwd / 命令拼进可注入字符串。
                command = [
                    "sh",
                    "-c",
                    'cd "$1" && shift && exec "$@"',
                    "sh",
                    request.cwd,
                    *command,
                ]
            return self._run_async(
                request,
                cmd=command,
                language=None,
                image=self.settings.berth_image or request.image or "builder-python",
                timeout=request.timeout,
                task_id=request.task_id or None,
                memory_mb=request.memory_mb,
                cpu_percent=request.cpu_percent,
            )

        env = dict(request.env or {})
        env["SHAKEDOWN_BOOTSTRAP_DEPS"] = "1"
        if self.settings.berth_cli_service:
            gateway = os.environ.get("SHAKEDOWN_BERTH_GATEWAY", "10.200.1.1")
            env["SHAKEDOWN_SERVICE_EXEC_URL"] = f"http://{gateway}:9000"
            env["SHAKEDOWN_SERVICE_EXEC_REF"] = self.settings.berth_cli_service
            env["SHAKEDOWN_SERVICE_EXEC_TOKEN"] = self.settings.berth_token
        # berth 沙箱里的 127.0.0.1 是它自己；宿主机上发布的服务要走 v1n 网关。
        base = str(env.get("SHAKEDOWN_BASE_URL") or "")
        if base:
            parts = urlsplit(base)
            if parts.hostname in {"127.0.0.1", "localhost", "::1"}:
                gateway = os.environ.get("SHAKEDOWN_BERTH_GATEWAY", "10.200.1.1")
                env["SHAKEDOWN_BASE_URL"] = urlunsplit(
                    (parts.scheme, f"{gateway}:{parts.port or 80}", parts.path, parts.query, parts.fragment)
                )
        script = build_command(request.files, env, workdir=request.workdir)
        return self._run_async(
            request,
            code=script,
            language="bash",
            image=self.settings.berth_image or request.image or "builder-python",
            timeout=request.timeout,
            task_id=request.task_id or None,
            memory_mb=request.memory_mb,
            cpu_percent=request.cpu_percent,
        )

    def health(self) -> dict[str, Any]:
        try:
            data = self.client.health()
            return {"backend": self.name, "ok": data.get("status") == "ok", "detail": data}
        except BerthError as exc:
            return {"backend": self.name, "ok": False, "error": str(exc)}
