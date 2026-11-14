# 攻击用例包（Attack Pack）

目标：让平台不只验证「接口能跑」，还主动生成**有攻击语义**的用例。模式可注册、可组合、可独立演进；新增一类攻击不需要改生成器、API 或界面。

## 1. 运行方式

```http
GET  /api/projects/{id}/attack-pack                 # 快速报告（不查漏洞库）
POST /api/projects/{id}/attack-pack/scan            # 扫描（含 OSV / 工具链）
POST /api/projects/{id}/attack-pack/generate        # 生成；validate=true 时自动试运行
```

界面：执行 →「攻击用例包」。

生成是**幂等**的：同一条攻击用 `spec.attack.key` 标识，已存在就跳过，保留人工修改。供应链类结果直接落缺陷（`cluster_key=attack-pack:<key>`），不生成一条永远修不好的用例。

## 2. 内置模式

| id | 类别 | 检查什么 | 结果形态 |
| --- | --- | --- | --- |
| `open-redirect` | 重定向 | `next` / `returnUrl` 是否接受 `//evil.example` | 用例 |
| `session-revocation` | 会话 | 改密 / 重置口令后旧会话是否仍可用 | 用例 |
| `authz-matrix` | 越权 | 匿名 / 低权限账号访问管理页面 | 用例 |
| `csrf` | CSRF | 带外站 `Origin` 的管理写操作是否被拒绝 | 用例 |
| `path-traversal` | 路径穿越 | 下载接口的 `{name}` 是否接受 `../` | 用例 |
| `default-credentials` | 弱口令 | admin/admin、123456 等是否可登录 | 用例 |
| `idor-project` | 越权 | 普通账号能否读取管理员私有项目 | 用例 |
| `duplicate-submit` | 幂等 | 同名对象提交两次是否重复创建 | 用例 |
| `race-condition` | 竞态 | 5 个并发同名创建只能成功一次 | 用例 |
| `api-authz` | 越权 | 代码里的 `/api/v1/<资源>` 匿名 / 低权限访问 | 用例 |
| `cookie-flags` | 传输 | 会话 Cookie 是否 HttpOnly / SameSite / Secure | 用例 |
| `security-headers` | 传输 | XCTO / XFO / HTTPS 下的 HSTS | 用例 |
| `fuzz-json` | 输入 | 畸形 JSON 是否让写接口 500 | 用例 |
| `idor-differential` | 越权 | 两个普通账号交叉读取私有项目 | 用例 |
| `delete-authz` | 越权 | guest 成员删除他人项目 | 用例 |
| `token-revocation` | 会话 | 令牌吊销后是否立即失效 | 用例 |
| `fuzz-protocol` | 协议 | 畸形 pkt-line / dist-tag JSON / URL 字节是否 5xx | 用例 |
| `rate-limit` | 会话 | 连续错误登录是否 429 / 423 / 锁定 | 用例 |
| `ssrf` | SSRF | URL / webhook 参数是否可指向回环地址 | 用例（有 URL 参数时） |
| `upload-filename` | 上传 | 上传文件名是否接受 `../` | 用例（有上传接口时） |
| `dependency-vulns` | 供应链 | 清单依赖 + Go 工具链的 OSV 公告 / EOL | 缺陷 |

每条攻击用例都用**策略断言**（`evidence.source_type=policy`，如 `SEC-CSRF`）。归因器看到策略断言失败会直接判为缺陷并自动确认，不再写「待确认」。

## 3. 新增一个模式

在 `backend/app/security/patterns.py` 里加一个类即可：

```python
from app.security.registry import AttackPattern, CaseDraft, register

@register
class CookieWithoutHttpOnly(AttackPattern):
    id = "cookie-httponly"
    title = "会话 Cookie 缺少 HttpOnly"
    category = "session"
    severity = "medium"
    description = "脚本可读取会话 Cookie 时，XSS 可直接盗号。"

    def applicable(self, ctx):
        if ctx.login_endpoint is None:
            return False, "没有 POST /login"
        return True, ""

    def build(self, ctx):
        # 复用 ctx.login_endpoint / ctx.admin_headers() / _policy() 等工具
        return [CaseDraft(
            title="[攻击] 会话 Cookie 必须 HttpOnly",
            spec={"steps": [...], "dimension": "security", ...},
            tags=["attack-pack", "attack:cookie-httponly"],
            attack_key="cookie-httponly:login",
        )]
```

约束：

- `id` / `category` 必须在 `registry.CATEGORIES` 里登记；
- `attack_key` 必须稳定，用于幂等；
- 断言必须带 `policy` 证据，否则失败不会被判成缺陷；
- 模式只读上下文，不写库、不发请求（执行交给试运行）。

## 4. 依赖 / 工具链扫描

`app/security/deps.py`：

- 解析 `go.mod`、`package.json` + `package-lock.json`、`requirements.txt`、`pom.xml`；
- 调 OSV `querybatch`，按生态（Go / npm / PyPI / Maven）查已知漏洞；
- 直接依赖没有版本锁定（`^1.2.3`）时只报告「无法确定版本」，不硬猜；
- Go 版本低于当前维护线时报告工具链 EOL；
- 网络不可用时在报告里写 `OSV 不可用`，不把「查不到」当「没有漏洞」。

当前 dm-git 是零第三方依赖（`go.mod` 只有 stdlib），但 `go 1.22` 已停止安全维护，会落一条供应链缺陷。若后续引入依赖，扫描无需改代码。

## 5. 边界

- 攻击用例默认只做**可安全重放**的动作：创建一次性用户并在 cleanup 删除；不删除真实项目、不改配置、不清数据。
- 管理写操作只发**无效参数**（如 5 位口令）来验证拒绝逻辑，避免模式本身造成破坏。
- 每个模式生成的数量有上限（当前每类 1~5 条），避免一次性刷几百条用例。
- 需要被测环境就绪；环境失败会被归因成 `environment`，不会误判成漏洞。

## 6. 继续扩展的方向

模式不追求一次列全，按「能复用、能自动判定、失败有政策依据」三个条件逐步加：

| 方向 | 例子 | 需要的上下文 |
| --- | --- | --- |
| 对象级越权 | 把 `{id}` 改成相邻 ID，读/删别人的对象 | 资源 ID 生成方式 |
| SSRF | 代理仓库地址、webhook、导入 URL 指向内网 | 可配置外联地址的接口 |
| 上传 | 双扩展名、压缩炸弹、MIME 伪造 | 文件上传接口 |
| 反序列化 / 注入 | 模板、SQL、命令参数拼接 | 参数与代码证据 |
| 竞态（更多） | 并发最后管理员操作、并发改密 | 并发执行能力（已有 `parallel` 步骤） |
| 传输 | CSP / 缓存敏感页 | 响应头 |
| 身份 | JWT 算法混淆、`none`、过期校验、刷新令牌重放 | 令牌格式与校验代码 |
| 反序列化 | 模板、XML、JSON 绑定到危险类型 | 参数与代码证据 |
| 供应链 | OSV 已知漏洞、EOL 工具链、锁文件缺失 | 依赖清单 / 构建信息 |

新增模式时优先复用 `AttackContext` 的接口/账号查询与 `_policy()`、`_admin_step()`；
不要写死项目名、路径或账号，否则换一个 SUT 就失效。

## 7. 隔离安全实验室

用于**不可逆或会锁死管理面**的探测，不碰项目真实环境：

```http
POST /api/projects/{id}/security-lab/run
```

流程：复制代码 → `go build` → 临时数据目录 + 随机端口启动 → 建账号/夹具 →
执行探针 → 生成 Finding → 杀进程并删除临时目录。

当前探针：

- 同名用户组与个人命名空间冲突（会创建无法删除的组，所以只在实验室跑）；
- 最后管理员并发禁用（会锁死管理面，所以只在实验室跑）。

实验室产出的 Finding 由平台自己写成 `confirmed_by=auto:security-lab`，不是人工录入。
