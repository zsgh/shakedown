#!/usr/bin/env python3
"""dm-git 变异测试基线：用真实的 HTTP 断言衡量用例的杀伤力。

做法：复制源码 → 逐个替换关键判断 → go build → 在独立端口启动 →
跑一组「基线全绿」的 HTTP 检查；任一检查失败即视为该变异体被杀死。
不修改原仓库，所有构建/数据都在 /tmp。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

SOURCE = Path("/Users/zhangsong/github/weidayong/dm-git")
WORK = Path("/tmp/dmgit-mutation")
PORT = 18089
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = ("admin", "mut-admin-pass")
ALICE = ("alice", "AlicePass1!")
VIEWER = ("viewer", "ViewerPass1!")

MUTANTS = [
    (
        "api.users.admin_check",
        "internal/server/api.go",
        """\tif !u.IsAdmin {\n\t\twriteJSON(w, 403, map[string]string{"message": "需要系统管理员权限"})\n\t\treturn\n\t}\n\tswitch {\n\tcase len(seg) == 1 && r.Method == http.MethodGet:""",
        """\tif false {\n\t\twriteJSON(w, 403, map[string]string{"message": "需要系统管理员权限"})\n\t\treturn\n\t}\n\tswitch {\n\tcase len(seg) == 1 && r.Method == http.MethodGet:""",
    ),
    (
        "api.projects.read_check",
        "internal/server/api.go",
        """\t\tacc := s.st.Access(u, p)\n\t\tif !acc.Read {\n\t\t\twriteJSON(w, 403, map[string]string{"message": "没有访问权限"})""",
        """\t\tacc := s.st.Access(u, p)\n\t\tif false {\n\t\t\twriteJSON(w, 403, map[string]string{"message": "没有访问权限"})""",
    ),
    (
        "git.private_read_check",
        "internal/server/server.go",
        """\tif !acc.Read && !acc.Write {\n\t\tif u == nil {""",
        """\tif false {\n\t\tif u == nil {""",
    ),
    (
        "project.page_read_check",
        "internal/server/handlers.go",
        """\tacc := s.st.Access(u, p)\n\tif !acc.Read {\n\t\tif u == nil {""",
        """\tacc := s.st.Access(u, p)\n\tif false {\n\t\tif u == nil {""",
    ),
    (
        "store.namespace_owner_access",
        "internal/store/store.go",
        """\t\tif strings.EqualFold(p.Namespace, u.Username) {\n\t\t\treturn Access{Read: true, Write: true, Admin: true}\n\t\t}""",
        """\t\tif false {\n\t\t\treturn Access{Read: true, Write: true, Admin: true}\n\t\t}""",
    ),
    (
        "auth.disabled_user_session",
        "internal/auth/auth.go",
        """\tu := m.st.User(s.Username)\n\tif u == nil || u.Disabled {\n\t\treturn nil\n\t}\n\treturn u""",
        """\tu := m.st.User(s.Username)\n\tif u == nil {\n\t\treturn nil\n\t}\n\treturn u""",
    ),
    (
        "store.valid_name",
        "internal/store/store.go",
        "func ValidName(s string) bool { return nameRe.MatchString(s) }",
        "func ValidName(s string) bool { return true }",
    ),
    (
        "auth.check_origin",
        "internal/auth/auth.go",
        """func CheckOrigin(r *http.Request) bool {
	ref := r.Header.Get("Origin")""",
        """func CheckOrigin(r *http.Request) bool {
	return true
	ref := r.Header.Get("Origin")""",
    ),
    (
        "npm.publish_auth",
        "internal/npm/npm.go",
        """	if !m.requireWrite(w, r, repo) {
		return
	}
	if repo.Type != "hosted" {""",
        """	if false {
		return
	}
	if repo.Type != "hosted" {""",
    ),
    (
        "api.projects.delete_admin",
        "internal/server/api.go",
        """		if len(seg) == 3 && r.Method == http.MethodDelete {
			if !acc.Admin {""",
        """		if len(seg) == 3 && r.Method == http.MethodDelete {
			if false {""",
    ),
    (
        "project.settings.admin",
        "internal/server/handlers.go",
        """	if !acc.Admin {
		http.Error(w, "403 需要项目管理员权限", http.StatusForbidden)
		return
	}
	_ = r.ParseForm()
	err := s.st.UpdateProject""",
        """	if false && !acc.Admin {
		http.Error(w, "403 需要项目管理员权限", http.StatusForbidden)
		return
	}
	_ = r.ParseForm()
	err := s.st.UpdateProject""",
    ),
    (
        "api.projects.members_admin",
        "internal/server/api.go",
        """		if len(seg) == 4 && seg[3] == "members" && r.Method == http.MethodPost {
			if !acc.Admin {""",
        """		if len(seg) == 4 && seg[3] == "members" && r.Method == http.MethodPost {
			if false {""",
    ),
    (
        "api.backups.admin_check",
        "internal/server/api.go",
        """\tif !u.IsAdmin {\n\t\twriteJSON(w, 403, map[string]string{"message": "需要系统管理员权限"})\n\t\treturn\n\t}\n\tif s.bk == nil {\n\t\twriteJSON(w, 503, map[string]string{"message": "备份功能未启用"})""",
        """\tif false {\n\t\twriteJSON(w, 403, map[string]string{"message": "需要系统管理员权限"})\n\t\treturn\n\t}\n\tif s.bk == nil {\n\t\twriteJSON(w, 503, map[string]string{"message": "备份功能未启用"})""",
    ),
]


def _copy_source() -> Path:
    src = WORK / "src"
    if src.exists():
        shutil.rmtree(src)
    shutil.copytree(
        SOURCE,
        src,
        ignore=shutil.ignore_patterns(".git", "data", "dist", "backups", ".run-sandbox"),
    )
    return src


def _config(data_dir: Path) -> dict:
    cfg = json.loads((SOURCE / "config.json").read_text(encoding="utf-8"))
    cfg.update(
        {
            "addr": f"127.0.0.1:{PORT}",
            "externalURL": BASE,
            "dataDir": str(data_dir),
            "allowAnonymousRead": True,
        }
    )
    cfg["backup"] = {
        "dir": str(data_dir / "backups"),
        "intervalHours": 24,
        "keep": 7,
        "includeMavenHosted": False,
        "targets": [{"name": "本地磁盘", "type": "local", "enabled": True, "port": 22}],
    }
    return cfg


def _wait_ready(proc: subprocess.Popen, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            response = httpx.get(BASE + "/api/v1/version", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


def _http() -> httpx.Client:
    return httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False, timeout=5.0)


def _check(name: str, passed: bool, detail: str = "") -> tuple[str, bool, str]:
    return name, passed, detail


def run_checks() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    with _http() as admin:
        # 准备两个普通账号与一个私有项目
        for username, password in (ALICE, VIEWER):
            response = admin.post(
                "/api/v1/users",
                auth=ADMIN,
                json={"username": username, "password": password, "displayName": username},
            )
            if response.status_code not in {201, 409}:
                return [_check("setup.create_user", False, f"{username} -> {response.status_code}")]

        with _http() as alice:
            response = alice.post(
                "/new",
                auth=ALICE,
                data={"namespace": "alice", "name": "mutproj", "description": "", "private": "on"},
            )
            if response.status_code not in {303, 409}:
                return [_check("setup.create_project", False, f"-> {response.status_code}")]

        with _http() as viewer:
            response = viewer.get("/api/v1/projects/alice/mutproj", auth=VIEWER)
            results.append(_check("viewer.api.project", response.status_code == 403, str(response.status_code)))
            response = viewer.get("/p/alice/mutproj", auth=VIEWER)
            results.append(_check("viewer.page.project", response.status_code == 403, str(response.status_code)))
            response = viewer.get(
                "/alice/mutproj.git/info/refs",
                params={"service": "git-upload-pack"},
                auth=VIEWER,
            )
            results.append(_check("viewer.git.private", response.status_code == 403, str(response.status_code)))
            response = viewer.get("/api/v1/users", auth=VIEWER)
            results.append(_check("viewer.api.users", response.status_code == 403, str(response.status_code)))
            response = viewer.get("/api/v1/backups", auth=VIEWER)
            results.append(_check("viewer.api.backups", response.status_code == 403, str(response.status_code)))

        response = admin.get("/api/v1/users")
        results.append(_check("anon.api.users", response.status_code == 401, str(response.status_code)))

        with _http() as alice:
            response = alice.get("/api/v1/projects/alice/mutproj", auth=ALICE)
            results.append(_check("owner.api.project", response.status_code == 200, str(response.status_code)))

        with _http() as alice:
            member = alice.post(
                "/p/alice/mutproj/members",
                auth=ALICE,
                data={"username": "viewer", "role": "guest"},
            )
            results.append(_check("setup.guest_member", member.status_code == 303, str(member.status_code)))
        with _http() as viewer:
            response = viewer.delete("/api/v1/projects/alice/mutproj", auth=VIEWER)
            results.append(_check("viewer.delete.project", response.status_code in {401, 403}, str(response.status_code)))
        with _http() as alice:
            response = alice.get("/api/v1/projects/alice/mutproj", auth=ALICE)
            results.append(_check("project.survives_delete_attempt", response.status_code == 200, str(response.status_code)))

        response = admin.post(
            "/api/v1/projects",
            auth=ADMIN,
            json={"namespace": "alice", "name": "bad name!", "private": True},
        )
        results.append(_check("invalid.project_name", response.status_code in {400, 409}, str(response.status_code)))

        with _http() as viewer:
            response = viewer.post(
                "/api/v1/projects/alice/mutproj/members",
                auth=VIEWER,
                json={"username": "viewer", "role": "developer"},
            )
            results.append(_check("viewer.add_member", response.status_code == 403, str(response.status_code)))
            response = viewer.post(
                "/p/alice/mutproj/settings",
                auth=VIEWER,
                data={"description": "pwned", "private": "on"},
            )
            results.append(_check("viewer.project_settings", response.status_code == 403, str(response.status_code)))

        # 外站 Origin 的管理写操作不得产生副作用
        csrf_user = "csrf_mut"
        response = admin.post(
            "/admin/users",
            auth=ADMIN,
            headers={"Origin": "http://evil.example"},
            data={"username": csrf_user, "password": "ValidPass1!", "displayName": "csrf"},
        )
        users = admin.get("/api/v1/users", auth=ADMIN).json()
        names = {item.get("username") for item in users} if isinstance(users, list) else set()
        results.append(_check("csrf.side_effect", csrf_user not in names, f"status={response.status_code} exists={csrf_user in names}"))

        # 未认证的 npm 发布必须 401
        with _http() as anonymous:
            response = anonymous.put(
                "/repository/npm-hosted/mutpkg",
                headers={"Content-Type": "application/json"},
                content='{"name":"mutpkg","versions":{"1.0.0":{}},"_attachments":{}}',
            )
            results.append(_check("npm.publish.anonymous", response.status_code == 401, str(response.status_code)))

        # 禁用用户后，旧会话必须失效
        with _http() as alice_session:
            login = alice_session.post(
                "/login",
                data={"username": "alice", "password": ALICE[1], "next": "/"},
            )
            me = alice_session.get("/api/v1/me")
            results.append(_check("alice.session.before_disable", login.status_code == 303 and me.status_code == 200, f"{login.status_code}/{me.status_code}"))
            with _http() as admin_session:
                toggle = admin_session.post("/admin/users/alice/toggle", auth=ADMIN)
            after = alice_session.get("/api/v1/me")
            results.append(_check("disabled.session.rejected", toggle.status_code == 303 and after.status_code == 401, f"{toggle.status_code}/{after.status_code}"))
    return results


def _start(src: Path, cfg: Path, data: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["DMGIT_ADMIN_PASSWORD"] = ADMIN[1]
    binary = WORK / "dm-git"
    return subprocess.Popen(
        [str(binary), "-config", str(cfg)],
        cwd=str(src),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _build(src: Path) -> bool:
    env = dict(os.environ)
    env["GOCACHE"] = "/tmp/dmgit-gocache"
    result = subprocess.run(
        ["go", "build", "-o", str(WORK / "dm-git"), "./cmd/dm-git"],
        cwd=str(src),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        print("BUILD FAILED:", result.stderr[-1000:])
    return result.returncode == 0


def _run_variant(label: str, src: Path) -> tuple[bool, str]:
    data = WORK / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    cfg_path = WORK / "config.json"
    cfg_path.write_text(json.dumps(_config(data), ensure_ascii=False), encoding="utf-8")
    proc = _start(src, cfg_path, data)
    try:
        if not _wait_ready(proc):
            return False, "服务未就绪"
        checks = run_checks()
        failed = [item for item in checks if not item[1]]
        detail = ", ".join(f"{name}={detail}" for name, _, detail in failed[:4])
        return not failed, detail or "全部通过"
    finally:
        _stop(proc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="只跑指定变异体 label")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    src = _copy_source()

    print("== 基线 ==")
    if not _build(src):
        return 2
    baseline_ok, detail = _run_variant("baseline", src)
    print("baseline", "PASS" if baseline_ok else "FAIL", detail)
    if not baseline_ok:
        return 2

    killed = 0
    total = 0
    survivors: list[str] = []
    invalid: list[str] = []
    for label, rel, old, new in MUTANTS:
        if args.only and args.only not in label:
            continue
        total += 1
        target = src / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(label, "SKIP（未找到替换点）")
            invalid.append(label)
            continue
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            if not _build(src):
                invalid.append(label)
                continue
            passed, detail = _run_variant(label, src)
        finally:
            target.write_text(original, encoding="utf-8")
        if not passed:
            print(f"{label:32} KILLED   {detail}")
            killed += 1
        else:
            print(f"{label:32} SURVIVED {detail}")
            survivors.append(label)
    valid = total - len(invalid)
    print(f"\n分数：{killed}/{valid} = {(killed / valid * 100) if valid else 0:.0f}%")
    if survivors:
        print("未被杀死：", ", ".join(survivors))
    if invalid:
        print("无效/跳过：", ", ".join(invalid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
