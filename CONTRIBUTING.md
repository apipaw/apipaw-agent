# 贡献指南 / Contributing Guide

> 感谢你为 APIPaw Agent 贡献代码。本文档说明**如何提交 PR、如何报告 Bug**，
> 以及**改动让退（yield）机制时必须遵守的红线**。

阅读本指南前，请先阅读 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) 与
[README.md](README.md)。

---

## 1. 开发环境

```bash
git clone https://github.com/apipaw/apipaw-agent.git
cd apipaw-agent
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py --no-tray --no-telemetry
```

**依赖分层（重要）**

| 依赖 | 是否必需 | 说明 |
|---|---|---|
| `fastapi` / `uvicorn` / `pydantic` | 必需 | 本地 OpenAI 规范网关 |
| `pystray` / `Pillow` | 可选 | 缺失时自动跳过托盘，其余功能不受影响 |
| `torch` | 可选 | 缺失时显存释放退化为 Python GC 回收 |

**任何 PR 都不得引入新的必需依赖**，除非在 PR 描述中说明理由并获得维护者认可。

### 编码约定

- Python 3.10+ 语法；类型注解齐全，关键分支补中文注释
- 模块职责单一，禁止跨模块直接访问私有成员（`_` 前缀）
- 日志统一用 `print(f"[模块名] ...")` 前缀，便于用户在控制台定位
- **日志不得包含**钱包地址明文、`node_secret`、任何用户文件内容

---

## 2. 让退机制红线（50ms SLA）

让退是本项目**最核心的用户承诺**：用户一碰键鼠，节点必须在 50ms 内让出显存。
改动 `yield_monitor.py` 时，以下规则**不可违背**：

| # | 红线 | 原因 |
|---|---|---|
| R1 | **先切状态，后释放显存** | `torch.cuda.empty_cache()` 可耗时数百毫秒，同步执行会出现"用户已回来但节点仍可接单"的危险窗口 |
| R2 | 显存释放必须在**独立线程**执行 | 释放耗时不得进入 50ms 预算 |
| R3 | 轮询间隔不得**大于** `poll_interval_ms`（默认 5ms） | 轮询越慢，检测延迟越高 |
| R4 | 不得移除 `timeBeginPeriod(1)` | Windows 默认计时精度 15.6ms，单这一项就吃掉三分之一预算 |
| R5 | 不得在让退路径上做**阻塞 IO / 网络请求 / 加锁等待** | 会直接击穿 SLA |
| R6 | 轮询线程内的异常**必须被捕获** | 一次日志异常就会让节点永久失去让退能力 |
| R7 | 日志打印必须能容忍 **GBK 控制台** | 不可编码字符要替换而不是抛 `UnicodeEncodeError`（见 `console.py`） |

> 若你的改动无法同时满足上述约束，请**先开 Issue 讨论**，不要直接提 PR。

**验证让退延迟**：

```bash
curl http://127.0.0.1:52415/health
# 查看 yield_metrics.last_yield_latency_ms 与 sla_violations
```

`sla_violations` 应当始终为 `0`。任何让 `sla_violations` 增长的改动都会被 CI 拒绝。

---

## 3. 提交 Pull Request

### 3.1 分支与提交信息

```bash
git checkout -b feat/your-feature
git commit -m "feat: 增加 xxx 能力"
git push origin feat/your-feature
```

提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

| 类型 | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 不改变行为的重构 |
| `perf` | 性能优化 |
| `docs` | 文档 |
| `test` | 测试 |
| `ci` | 流水线 |
| `chore` | 构建/杂项 |

### 3.2 PR 描述必须包含

1. **改动动机** —— 为什么需要这个改动
2. **影响范围** —— 改了哪些模块、是否触及让退机制红线
3. **验证方式** —— 至少贴出 `curl /health` 的实际输出
4. **关联 Issue**（如有）

**必须自测的检查项**：

```bash
python -m compileall -q -x '(\.venv|\\build|\\dist)' .
python .github/scripts/yield_smoke_test.py
python .github/scripts/health_check.py
```

### 3.3 CI 会自动执行

| Job | 内容 |
|---|---|
| `build-test (full)` | Python 3.10 + 完整依赖：编译检查、导入自检、健康检查、让退冒烟测试 |
| `build-test (minimal)` | Python 3.10 + **不装 pystray/Pillow**：验证托盘缺失时仍可启动 |
| `matrix-python` | Python 3.10 / 3.12 兼容性矩阵 |

CI 全绿是合并的**必要条件**。

---

## 4. 报告 Bug

请开 [Issue](https://github.com/apipaw/apipaw-agent/issues)，并附上：

| 项目 | 说明 |
|---|---|
| 运行环境 | 操作系统 / Python 版本 / GPU 型号 |
| 复现步骤 | 尽量最小化 |
| 实际行为 vs 预期行为 | 附上日志片段 |
| `/health` 输出 | **务必包含**，它是定位状态问题的第一手信息 |
| 配置文件 | `agent.config.json`（**请先删除 `node_secret` 与 `wallet_address`**） |

> ⚠️ 提交日志前请确认其中不含 `node_secret`、钱包地址或任何个人数据。

---

## 5. 贡献模型元数据（PAWMODEL.md）

若你希望自己的模型接入 APIPaw 网络，请在模型仓库根目录添加 `PAWMODEL.md`，
格式与字段含义见 [PAWMODEL.md](PAWMODEL.md)。

要点：

- YAML 元数据块必须在文件**开头**
- `royalty_pricing.royalty_payout_address` 必须是**你本人**的收款钱包
- `pipeline_config.total_layers` 必须与权重的实际层数一致
- 改变元数据结构属于**破坏性变更**，需递增 `pawmodel_version` 主版本号

---

## 6. 隐私底线

本项目运行在用户的个人电脑上，贡献者**不得**引入以下行为：

- ❌ 上报文件内容、剪贴板、键盘记录、屏幕截图
- ❌ 收集任何可识别用户身份的信息
- ❌ 在用户未同意时把数据发往第三方（云端心跳端点除外，且必须可关闭）
- ❌ 把 `node_secret` / 钱包地址写入日志或遥测之外的任何位置

---

## 7. 许可与版权

提交 PR 即表示你同意你的贡献将以 **MIT 许可证**发布。
请勿提交包含他人版权代码或未授权素材的内容。
