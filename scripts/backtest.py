#!/usr/bin/env python
"""有效性验证（回测）：用「已修复的历史 bug」检验 Agent 能不能真的测出来。

流程（对每个 bug 重复）：
1. 用 ``git worktree`` 把被测代码回退到修复前的 commit；
2. 只用「当时的文档 / spec」建一个临时知识源（不导入代码 —— 模拟黑盒测试）；
3. 让 Agent 生成用例并执行（对��修复前代码运行的环境）；
4. 判断失败用例是否命中该 bug（按路径 / 关键字匹配）。

输出指标：抓到 bug 的比例、误报数、漏报原因分类，并生成 JSON + HTML 报告。

用法：
    python scripts/backtest.py \
        --repo /path/to/repo \
        --bugs examples/backtest-bugs.json \
        --target-base-url http://127.0.0.1:9100 \
        --project-name backtest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SHAKEDOWN_SANDBOX_MODE", "local")
os.environ.setdefault("SHAKEDOWN_LLM_PROVIDER", "mock")

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.enums import CaseStatus, KnowledgeKind, RunStatus  # noqa: E402
from app.ingest.service import ingest_source  # noqa: E402
from app.models import CaseResult, Feature, Finding, KnowledgeSource, Project, Run, TestCase  # noqa: E402
from app.worker import handlers  # noqa: E402


@dataclass
class Bug:
    id: str
    commit: str
    note: str = ""
    paths: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    doc_paths: list[str] = field(default_factory=list)
    spec_paths: list[str] = field(default_factory=list)


@dataclass
class BugOutcome:
    bug: Bug
    caught: bool = False
    matched_cases: list[str] = field(default_factory=list)
    total_cases: int = 0
    failed_cases: int = 0
    reason: str = ""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def make_worktree(repo: Path, commit: str, target: Path) -> Path:
    """把代码回退到修复前的状态（commit 的父提交）。"""
    parent = git(repo, "rev-parse", f"{commit}^")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), parent],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], capture_output=True)


def backtest_project(name: str, base_url: str) -> int:
    with session_scope() as session:
        project = session.scalar(select(Project).where(Project.name == name))
        if project is None:
            project = Project(name=name, description="回测临时项目", base_url=base_url, isolation={"mode": "prefix"})
            session.add(project)
            session.flush()
        else:
            project.base_url = base_url
        for model in (CaseResult, Finding, TestCase, Feature, Run):
            session.query(model).filter_by(project_id=project.id).delete()
        project_id = project.id
    return project_id


def add_source(project_id: int, kind: str, location: str, name: str) -> int:
    with session_scope() as session:
        project = session.get(Project, project_id)
        source = KnowledgeSource(
            project_id=project_id, kind=kind, name=name, location=location, status="pending"
        )
        session.add(source)
        session.flush()
        try:
            ingest_source(session, project, source)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! 导入 {name} 失败：{exc}")
        source_id = source.id
    return source_id


def run_case_suite(project_id: int, label: str) -> tuple[int, list[CaseResult]]:
    """启用全部用例 → 执行 → 返回 (run_id, 结果)。"""
    with session_scope() as session:
        cases = session.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
        for case in cases:
            case.status = CaseStatus.ENABLED.value
        run = Run(project_id=project_id, trigger="backtest", status=RunStatus.QUEUED.value, case_ids=[])
        session.add(run)
        session.flush()
        run_id = run.id

    with session_scope() as session:
        handlers.handle_execute_run(session, {"run_id": run_id})

    with session_scope() as session:
        return run_id, list(
            session.scalars(select(CaseResult).where(CaseResult.run_id == run_id, CaseResult.rerun.is_(False))).all()
        )


def match_case(case: TestCase | None, bug: Bug) -> bool:
    if case is None:
        return False
    spec = case.spec or {}
    text = json.dumps(spec, ensure_ascii=False).lower()
    if any(path.lower() in text for path in bug.paths):
        return True
    return any(keyword.lower() in text for keyword in bug.keywords)


def analyse(project_id: int, bug: Bug, results: list[CaseResult]) -> BugOutcome:
    outcome = BugOutcome(bug=bug)
    with session_scope() as session:
        cases = {c.id: c for c in session.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()}
    outcome.total_cases = len(results)
    failures = [r for r in results if r.status in {"fail", "error"}]
    outcome.failed_cases = len(failures)
    for result in failures:
        case = cases.get(result.case_id)
        if match_case(case, bug):
            outcome.caught = True
            outcome.matched_cases.append(case.title if case else str(result.case_id))
    if not outcome.caught:
        if outcome.total_cases == 0:
            outcome.reason = "没有生成任何用例"
        elif outcome.failed_cases == 0:
            outcome.reason = "用例全部通过 —— 没测出这个 bug"
        else:
            outcome.reason = "有失败用例，但都不覆盖该 bug 的接口 / 关键字"
    return outcome


def render_html(outcomes: list[BugOutcome], project_name: str) -> str:
    from app.reporting.html_export import _CSS  # 复用样式

    caught = sum(1 for outcome in outcomes if outcome.caught)
    rows = "".join(
        "<tr>"
        f"<td>{outcome.bug.id}</td>"
        f"<td>{'✅ 抓到' if outcome.caught else '❌ 漏报'}</td>"
        f"<td>{outcome.total_cases}</td>"
        f"<td>{outcome.failed_cases}</td>"
        f"<td>{'<br>'.join(outcome.matched_cases) or outcome.reason}</td>"
        f"<td>{outcome.bug.note}</td>"
        "</tr>"
        for outcome in outcomes
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Shakedown 回测报告</title>
<style>{_CSS}</style></head><body>
<h1>Shakedown 回测报告 · {project_name}</h1>
<div class="sub">共 {len(outcomes)} 个历史 bug，命中 {caught} 个</div>
<div class="cards">
<div class="card"><div class="num">{caught}/{len(outcomes)}</div><div class="label">命中 bug</div></div>
<div class="card"><div class="num">{round(caught / len(outcomes) * 100, 1) if outcomes else 0}%</div><div class="label">命中率</div></div>
</div>
<h2>明细</h2>
<table><thead><tr><th>Bug</th><th>结果</th><th>用例数</th><th>失败数</th><th>说明</th><th>备注</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>
"""


def start_target(deploy_cmd: str, worktree: Path, port: int) -> subprocess.Popen:
    """按模板启动「修复前代码」的服务（{worktree} / {port} / {python} 会被替换）。"""
    command = deploy_cmd.format(worktree=worktree, port=port, python=sys.executable)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket
    import time

    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return process
        except OSError:
            time.sleep(0.25)
    process.terminate()
    raise RuntimeError(f"被测环境启动失败：{command}")


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="被测项目 git 仓库路径")
    parser.add_argument("--bugs", required=True, help="bug 列表 JSON 文件")
    parser.add_argument("--target-base-url", required=True, help="运行着「修复前代码」的被测环境地址")
    parser.add_argument(
        "--deploy-cmd",
        default="",
        help="启动「修复前代码」的命令模板，可用 {worktree} / {port} 占位符（配合 --target-base-url 为空时使用）",
    )
    parser.add_argument("--project-name", default="backtest")
    parser.add_argument("--docs-dir", default="docs", help="相对仓库根的文档目录")
    parser.add_argument("--spec", default="", help="相对仓库根的 spec 文件（可选）")
    parser.add_argument("--out", default=str(REPO_ROOT / "var" / "backtest-report"))
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    bugs = [Bug(**item) for item in json.loads(Path(args.bugs).read_text(encoding="utf-8"))]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    outcomes: list[BugOutcome] = []
    for bug in bugs:
        print(f"▶ 回测 {bug.id}（{bug.commit}）")
        workdir = Path(tempfile.mkdtemp(prefix=f"shakedown-backtest-{bug.id}-"))
        target_process = None
        target_base_url = args.target_base_url
        try:
            worktree = make_worktree(repo, bug.commit, workdir / "src")
            if args.deploy_cmd:
                port = free_port()
                target_process = start_target(args.deploy_cmd, worktree, port)
                target_base_url = f"http://127.0.0.1:{port}"
            project_id = backtest_project(f"{args.project_name}-{bug.id}", target_base_url)
            docs = worktree / args.docs_dir
            if docs.exists():
                add_source(project_id, KnowledgeKind.MARKDOWN.value, str(docs), f"{bug.id} 文档")
            else:
                print("    ! 仓库里没有文档目录，跳过文档导入")
            spec_files = [p for p in (bug.spec_paths or [])]
            if not spec_files and args.spec:
                spec_files = [args.spec]
            for spec_rel in spec_files:
                spec_path = worktree / spec_rel
                if spec_path.exists():
                    add_source(project_id, KnowledgeKind.OPENAPI.value, str(spec_path), f"{bug.id} spec")

            with session_scope() as session:
                handlers.handle_plan_features(session, {"project_id": project_id})
                handlers.handle_generate_cases(session, {"project_id": project_id})
            results = run_case_suite(project_id, bug.id)
            run_id, results = results
            outcome = analyse(project_id, bug, results)
            # 把回测指标写进 run.backtest，看板上可以直接看到
            with session_scope() as session:
                run = session.get(Run, run_id)
                if run is not None:
                    run.backtest = {
                        "bug_id": bug.id,
                        "caught": outcome.caught,
                        "matched_cases": outcome.matched_cases,
                        "total_cases": outcome.total_cases,
                        "failed_cases": outcome.failed_cases,
                        "reason": outcome.reason,
                        "note": bug.note,
                        "paths": bug.paths,
                        "keywords": bug.keywords,
                    }
            outcomes.append(outcome)
            print(f"    {'✓ 抓到' if outcome.caught else '✗ 漏报'}：{outcome.reason}")
            remove_worktree(repo, worktree)
        finally:
            if target_process is not None:
                target_process.terminate()
                try:
                    target_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    target_process.kill()
            subprocess.run(["rm", "-rf", str(workdir)], check=False)

    caught = sum(1 for outcome in outcomes if outcome.caught)
    metrics = {
        "bugs": len(outcomes),
        "caught": caught,
        "hit_rate": round(caught / len(outcomes), 4) if outcomes else 0.0,
        "false_positives": sum(max(0, outcome.failed_cases - len(outcome.matched_cases)) for outcome in outcomes),
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }
    (out_dir / "backtest.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "backtest.html").write_text(render_html(outcomes, args.project_name), encoding="utf-8")
    print(f"▶ 指标：{json.dumps({k: v for k, v in metrics.items() if k != 'outcomes'}, ensure_ascii=False)}")
    print(f"▶ 报告：{out_dir / 'backtest.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
