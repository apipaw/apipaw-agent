# PAWMODEL Specification v1.0

> 规范版本：`1.0.0` ｜ 客户端版本：`1.0.3` ｜ 状态：Draft for Open-Source Release ｜ 许可证：MIT

`PAWMODEL.md` 是 APIPaw 网络中开源大模型的描述规范文件。模型创作者只需在 HuggingFace
仓库或 GitHub 中包含此文件，即可自动接入 APIPaw 网络并赚取 **15% 版税** 收益。

本文档定义三件事：

1. **元数据格式** —— 一段可被机器解析的 YAML 前言（front matter）
2. **模型层数切片与显存需求** —— 决定模型如何被切分到多台挂机节点上跑
3. **创作者版税钱包绑定** —— 决定收益归属到哪个 Web3 钱包

---

## 1. 标准 YAML 元数据示例

模型仓库根目录放置名为 `PAWMODEL.md` 的文件，文件**开头**是 YAML 元数据块，其后是任意格式的
Markdown 说明正文（正文会被 APIPaw 模型目录页渲染）。

```yaml
pawmodel_version: "1.0.0"
model_name: "DeepSeek-R1-Distill-Qwen-1.5B"
author: "DeepSeek / APIPaw Community"
license: "MIT"

# 硬件与显存推荐
hardware_requirements:
  minimum_vram_gb: 1.8
  recommended_vram_gb: 3.0
  supported_backends: ["cuda", "metal", "vllm", "llama.cpp"]

# 张量切片与流水线配置
pipeline_config:
  total_layers: 28
  hidden_size: 1536
  activation_dtype: "FP16"
  tensor_schema: "INT8_DYNAMIC"

# 创作者版税规范 (场景 B)
royalty_pricing:
  enabled: true
  royalty_payout_address: "0x71C7656EC7a82e887C2186A1a941215E1c2966e"  # 创作者 Web3 钱包
  royalty_usd_per_1k_tokens: 0.0002
```

### 1.1 字段参考

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pawmodel_version` | str | ✅ | 本规范版本，必须是语义化版本；不兼容变更时递增主版本号 |
| `model_name` | str | ✅ | 模型展示名，也是调度侧路由的主键之一 |
| `author` | str | ✅ | 作者 / 组织名 |
| `license` | str | ✅ | 权重许可证，必须允许商业分发（MIT / Apache-2.0 / CC-BY-4.0 等） |
| `hardware_requirements.minimum_vram_gb` | float | ✅ | 单节点**最低**可承载该模型的显存（GB），决定最小可切分粒度 |
| `hardware_requirements.recommended_vram_gb` | float | ✅ | 推荐显存（GB），用于节点亲和性打分 |
| `hardware_requirements.supported_backends` | list[str] | ✅ | 推理后端白名单：`cuda` / `metal` / `vllm` / `llama.cpp` |
| `pipeline_config.total_layers` | int | ✅ | **Transformer 层总数**，是切片的基本单位 |
| `pipeline_config.hidden_size` | int | ✅ | 隐层维度，用于估算单层激活值显存 |
| `pipeline_config.activation_dtype` | str | ✅ | 激活值精度：`FP32` / `FP16` / `BF16` |
| `pipeline_config.tensor_schema` | str | ✅ | 权重量化方案：`FP16` / `INT8_DYNAMIC` / `INT4_AWQ` / `INT4_GPTQ` |
| `royalty_pricing.enabled` | bool | ✅ | 是否参与创作者版税结算 |
| `royalty_pricing.royalty_payout_address` | str | ✅ | **创作者 Web3 钱包地址**（EVM，`0x` + 40 位十六进制），收益结算到该地址 |
| `royalty_pricing.royalty_usd_per_1k_tokens` | float | ✅ | 每 1K tokens 的创作者版税（USD），与基价**叠加**而非替换 |

> `royalty_payout_address` 必须是**创作者本人的**钱包。以上示例地址仅为格式示范，
> 真实发布时必须替换为作者自有的收款地址。

### 1.2 最小可发布示例

只挂靠开源基础模型（场景 A，不参与创作者版税）时，可省略整个 `royalty_pricing` 块：

```yaml
pawmodel_version: "1.0.0"
model_name: "Qwen2.5-0.5B-Instruct"
author: "Qwen Team"
license: "Apache-2.0"
hardware_requirements:
  minimum_vram_gb: 1.0
  recommended_vram_gb: 1.6
  supported_backends: ["cuda", "llama.cpp"]
pipeline_config:
  total_layers: 24
  hidden_size: 896
  activation_dtype: "FP16"
  tensor_schema: "INT8_DYNAMIC"
```

---

## 2. 模型层数切片规范（Layer Slicing）

APIPaw 是**分布式推理网络**：一个大模型可以横跨多台挂机节点协同执行。
`pipeline_config.total_layers` 是切片的唯一权威依据。

### 2.1 切片规则

| 规则 | 说明 |
|---|---|
| **R1 等分** | 层数按节点数**尽量均分**，余数优先分配给 `hidden_size` 较大的前段（注意力层显存开销更高） |
| **R2 整层不可拆** | 一层 Transformer 不可跨节点拆分；层是调度的最小单位 |
| **R3 首尾驻留** | Layer 0（embedding）与最后一层（lm_head + norm）默认与倒数第一层共置，避免跨节点传 embedding |
| **R4 节点数上限** | 切片节点数 ≤ `min(在线节点数, ceil(total_layers / 2))`，保证每节点至少 2 层 |
| **R5 版本锁定** | `total_layers` 与权重版本一一对应；改动权重必须同步更新元数据 |

以 `DeepSeek-R1-Distill-Qwen-1.5B`（28 层）为例，拓扑按在线节点数均分：

```
2 节点:  Node A = Layer 0-13   Node B = Layer 14-27
3 节点:  Node A = Layer 0-9    Node B = Layer 10-19   Node C = Layer 20-27
```

调度侧会结合节点的 `vram_free_gb` 与 `hardware_type` 做**亲和性打分**：
显存更充裕、类型更匹配的节点优先承载更重的分段。

### 2.2 显存需求估算

单节点承载 `L` 层所需的显存近似为：

```
vram_required_gb ≈ total_weight_gb × (L / total_layers)
                 + activation_gb × (L / total_layers)
                 + fixed_overhead_gb

activation_gb     ≈ 2 × hidden_size × batch_tokens × 2 bytes / 1e9
fixed_overhead_gb ≈ 0.4   # CUDA context + 运行时 + KV cache 预留
total_weight_gb   ≈ params_billion × bytes_per_param
```

`bytes_per_param` 由 `tensor_schema` 决定：`FP16` = 2，`INT8_DYNAMIC` = 1，
`INT4_AWQ` / `INT4_GPTQ` = 0.5。

**约定**：`hardware_requirements.minimum_vram_gb` 必须 ≥ 上式在**单节点承载全部层**时的
估算值 ÷ `recommended_vram_gb` 对应的最大可承载层数比例，即：该值应保证**单节点**可以
独立跑完模型（作为节点离线时的降级路径），同时也保证切片后每段的实际占用
≤ 节点的 `vram_free_gb`。

---

## 3. 创作者版税钱包绑定规范（场景 B）

### 3.1 身份口径

> **创作者身份 = `PAWMODEL.md` 中的 `royalty_pricing.royalty_payout_address`（钱包地址）**，
> 而不是平台账号 ID。

这条口径贯穿全链路：

- **模型上架**：云端解析 `PAWMODEL.md` → 以 `royalty_payout_address` 作为 `author_wallet`
  写入 `models` 表（`tier='CREATOR'`），模型随即出现在买家端的模型下拉里
- **结算**：`settle_cost` 在算力基价上**叠加** 15% 创作者版税
- **提现**：提现流水落库（`creator_withdrawals`，`PENDING` 状态），扣减可提余额

### 3.2 结算口径

场景 B（社区创作者模型）的买家实付为 **基价 × 1.15**，拆分如下：

| 归属 | 比例 | 说明 |
|---|---|---|
| 节点（算力提供方） | 85% | 承担实际推理的挂机节点 |
| **创作者（版税）** | **15%** | 结算到 `royalty_payout_address` |
| 平台 | 15% | 调度、计费、网关与运维 |

场景 A（开源基础模型）为 节点 85% / 平台 15%，**不叠加**创作者版税。

> 版税是**叠加**而非替换：不修改算力基价，只在其上乘 1.15。任何结算口径变更都必须
> 同步修改云端 `db.py` 的 `settle_cost` 与 `MODEL_CATALOG`。

### 3.3 钱包地址要求

| 要求 | 规则 |
|---|---|
| 格式 | EVM 地址：`0x` 前缀 + 40 位十六进制字符，大小写不敏感但推荐 checksum 混排 |
| 归属 | 必须是创作者**本人**持有、可正常接收资产的地址 |
| 不可用 | 空字符串、`0x000...0` 占位地址、交易所提现白名单外的托管地址 |
| 变更 | 需在仓库中更新 `PAWMODEL.md` 并**重新发布版本**，云端按版本刷新绑定关系 |
| 结算方式 | 当前为**落库结算**（`creator_withdrawals` 记录 PENDING 流水），暂不做链上转账 |

### 3.4 绑定校验清单

云端在模型上架前会执行以下校验，任一不通过则拒绝上架：

1. `PAWMODEL.md` 存在且 YAML 可解析
2. `pawmodel_version` 在当前网关支持范围内
3. `license` 属于允许商业分发的白名单
4. `total_layers` ≥ 1 且与权重实际层数一致
5. `royalty_payout_address` 通过地址格式校验（非空、非占位）
6. `minimum_vram_gb` ≥ 1.0（低于此值无法承载任何主流分片）

---

## 4. 版本演进

| 版本 | 变更 |
|---|---|
| `1.0.0` | 首版：元数据字段、层数切片、版税钱包绑定 |

网关会保留对历史主版本的解析能力。引入**破坏性字段变更**（删除必填字段、改变切片语义、
改变结算口径）时必须递增主版本号；新增可选字段则递增次版本号。

> 本表记录的是**模型元数据规范版本**（`pawmodel_version`）。
> **客户端发布版本**（`agent_version`）是另一条版本线，随 `apipaw/apipaw-agent` 仓库的
> tag 递增，当前为 `1.0.3`；两者互不绑定——客户端发版不必然改动本规范。

---

## 5. 相关文档

- [README.md](README.md) —— 节点客户端说明
- [CONTRIBUTING.md](CONTRIBUTING.md) —— 贡献与 PR 指引
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) —— 社区行为准则
