# 回测（有效性验证）

> 目标：用「已经修好的历史 bug」检验 Agent 是不是真的能测出 bug。
> 指标是本项目「好不好用」的核心判断依据。

## 思路

对每个历史 bug：

1. 把被测代码回退到**修复前**的 commit（`git worktree` 到 `commit^`）；
2. 只用**当时的文档 / spec**建知识源 —— **不导入代码**，模拟纯黑盒测试；
3. 让 Agent 生成用例并执行（对**修复前**的环境）；
4. 判断失败用例是否命中该 bug。

命中判定：失败用例的步骤 / 断言里出现了 bug 声明的接口路径或关键字。

## 用法

```bash
python scripts/backtest.py \
  --repo examples/backtest-repo \
  --bugs examples/backtest-bugs.json \
  --target-base-url "" \
  --deploy-cmd "{python} {worktree}/server.py --port {port}"
```

参数：

| 参数 | 说明 |
|---|---|
| `--repo` | 被测项目 git 仓库路径 |
| `--bugs` | bug 列表 JSON（见下） |
| `--target-base-url` | 已经跑着「修复前代码」的环境地址 |
| `--deploy-cmd` | 启动「修复前代码」的命令模板，占位符 `{worktree}` / `{port}` / `{python}` |
| `--docs-dir` | 仓库里文档目录（相对仓库根），默认 `docs` |
| `--spec` | 仓库里 spec 文件（相对仓库根），可选 |
| `--out` | 报告输出目录，默认 `var/backtest-report/` |

bug 列表格式：

```json
[
  {
    "id": "B1",
    "commit": "f624deb",
    "note": "创建订单未校验 amount 必须大于 0",
    "paths": ["/api/orders"],
    "keywords": ["amount", "负数", "零"],
    "doc_paths": ["docs/api.md"],
    "spec_paths": ["specs/swagger.json"]
  }
]
```

- `commit`：修复该 bug 的提交（脚本会回退到它的父提交）。
- `paths` / `keywords`：命中判定用的路径与关键字。
- `doc_paths` / `spec_paths`：回退后要导入的文档与 spec（相对仓库根）。

## 输出

- `var/backtest-report/backtest.json`：指标
- `var/backtest-report/backtest.html`：可视化报告

指标：

```json
{
  "bugs": 1,
  "caught": 1,
  "hit_rate": 1.0,
  "false_positives": 0,
  "outcomes": [
    {
      "bug": { "...": "..." },
      "caught": true,
      "matched_cases": ["[边界] 创建订单 - amount 负数"],
      "total_cases": 12,
      "failed_cases": 1,
      "reason": ""
    }
  ]
}
```

| 指标 | 含义 |
|---|---|
| `hit_rate` | 抓到的 bug 比例 = `caught / bugs` |
| `false_positives` | 失败用例中没对上任何已知 bug 的部分（疑似误报） |
| `outcomes[].reason` | 漏报原因分类：没生成用例 / 用例全过 / 失败用例不覆盖该 bug |

## 注意事项

- 回测要求「修复前的代码」能跑起来；用 `--deploy-cmd` 让脚本从 worktree 拉起服务。
- 每个 bug 会建一个临时 Shakedown 项目（名字 `{project-name}-{bug.id}`），互不干扰。
- 真实项目建议至少 5–10 个 bug，命中率才有统计意义。
- 误报数会同时受到「断言依据质量」影响：没有依据的断言不判失败，因此误报天然偏低 ——
  这正是设计意图，但也要注意别把「待确认」当成了「没问题」。
