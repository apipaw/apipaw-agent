"""
agent_config.py — APIPaw Agent 配置文件管理

职责：
1. 定义 ``agent.config.json`` 的数据模型与默认配置
2. 线程安全的读取 / 写入 / 局部更新
3. 配置损坏时自动回退到默认值并保留现场文件

设计要点：
- 原子写入（临时文件 + os.replace），避免掉电/并发写入产生半截 JSON
- 永不因配置问题让进程启动失败：任何异常都退回默认配置
- 单例 + 读写锁，热更新时其他线程读到的一定是完整 JSON
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from console import enable_utf8_output

enable_utf8_output()

try:  # pydantic v2
    from pydantic import BaseModel, Field, ValidationError
except ImportError:  # pragma: no cover - 依赖缺失时降级为无校验模式
    BaseModel = object  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
    ValidationError = ValueError  # type: ignore[assignment]


CONFIG_FILE_NAME = "agent.config.json"

# 原子替换的重试参数（Windows 目标文件被瞬时占用时会 WinError 5）
_REPLACE_RETRY_ATTEMPTS = 20
_REPLACE_RETRY_DELAY_SEC = 0.05

# 客户端版本号（与心跳上报 / 云端注册表保持一致）
# v1.0.9（阶段六 P2 · 模块 A4）：**断流**语义收紧 ——
#   推理**跑到一半**后端挂掉（连接被掐 / 未收到 done 帧）⇒ 隧道回
#   ``backend_stream_broken`` 且**不回** RESPONSE_DONE，云端据此按
#   D6「断流不计费」免单。此前半截输出会被当成**完整回答**送到云端，
#   云端照常计费（拿半句话收全价）—— 这是真金白银的记账漏洞。
#   开跑前就挂（压根连不上）仍回 ``backend_unavailable``，两者**不混**；
#   同一 request_id 只回**一次**错误帧，绝不静默重试。
# v1.0.8（阶段六 P1 · 模块 A/B）：**模型解析**口径收紧 ——
#   云端点名了模型而本机没有 ⇒ **不再**静默回落默认模型（否则会把任务
#   派给一台装错模型的机器，且账单与实际服务不符）；本机只有唯一模型
#   时才允许顶替；本机**一个模型都没有** ⇒ 明确报错，让云端换一台节点。
#   心跳帧的 ``models`` 清单照旧上报（云端据此做模型感知选点）。
# v1.0.7（阶段六 P0 · 模块 A）：接入**真实推理引擎** ——
#   新增统一推理后端抽象层 ``inference_backend.py``（Ollama 底座已落地，
#   vLLM / llama.cpp 预留同接口实现位）；云端派单**不再**返回占位文本，
#   后端不可用时明确回 ``backend_unavailable``（绝不回落模拟回答）；
#   心跳帧新增 ``models``（本地可用模型清单）与 ``inference_backend``。
# v1.0.6（阶段五 P4 · 模块 B）：声明支持**派单协议 v2**（盲派单载荷）——
#   INIT 帧新增 ``protocol_version: 2``，并能解开云端下发的加密模型指令
#   （``model_cipher``）。老版本云端不下发该字段 ⇒ 走老载荷，完全兼容。
# v1.0.5（阶段五 P3）：WS 隧道帧新增硬件显存上报（vram_total_gb / vram_used_gb /
#   vram_free_gb / gpu_temp_c），云端据此判定节点能力等级（C0~C3）。
#   帧内只增字段 ⇒ 老版本云端完全不受影响。
# v1.0.13（阶段十三 P2）：**免命令行一键拉取 Ollama 模型** ——
#   新增 ``/api/ollama/tags``（本机已装模型 + 在线状态 + 拉取进度）、
#   ``/api/ollama/recommended``（云端推荐模型列表代理）、
#   ``/api/ollama/pull``（后台线程拉取，成功后自动写盘
#   ``backend_model`` + ``model_name``）与 ``/api/ollama/bind``
#   （设为当前接单模型）；面板新增「本地 Ollama 模型管理」区块。
#   ⚠️ 拉取走**后台线程**（几百 MB 下载绝不能占住事件循环）；
#   ⚠️ 模型名**严格白名单**校验（防路径穿越 / 参数注入）；
#   ⚠️ **只跟本机 127.0.0.1 说话**，绝不外呼（本机模块不含凭证）。
# v1.0.12（阶段八 P0/P2）：
#   ① **根治 AgentConfig 单例污染缺陷**：显式传 ``config_path`` 时返回
#      **独立实例**（老实现里 ``__new__`` 无视构造参数，永远返回单例 ⇒
#      ``AgentConfig(config_path=tmp)`` 被静默吞掉，``update()`` 直接改写
#      **用户真实的** ``agent.config.json``，且**无任何报错**）；
#      另增 ``AgentConfig.reset_instance()`` 供测试丢弃单例；
#   ② 面板支持 **URL 参数预填** ``?node_id=xxx&node_secret=yyy``
#      （控制台「一键新增算力节点」的凭证弹窗给出该链接，点开即填好；
#      填完即从地址栏抹掉明文密钥，不留在浏览器历史里）。
# v1.0.11（阶段七 P1/P2）：
#   ① 心跳/WS 帧上报**本地模型清单** ``models`` + ``backend_model`` +
#      ``inference_backend``（HTTP 通道此前只带 ``model_name`` ⇒ 云端
#      ``servable_models=0``，得手工跑脚本绑模型）；
#   ② 新增**强制待命模式**配置项 ``force_idle``（可持久化、面板可勾选、
#      热生效）—— 此前只能靠环境变量 ``APIPAW_FORCE_IDLE=1``（改 start.bat）。
# v1.0.10（阶段六 P3 · 模块 A2）：OpenAI 兼容推理后端（vLLM / llama.cpp）**落地** ——
#   新增 ``OpenAICompatBackend``（SSE 解析 + 真实 usage + 断流语义与 Ollama 同构）；
#   ``auto`` 探测支持多引擎（Ollama 优先，OpenAI 兼容引擎需**显式配置地址**
#   才进候选，避免误认本机其它 HTTP 服务）；DONE 帧新增 ``perf``
#   （节点自测的 TTFT / 吐字速度），云端据此用**真实**性能口径。
#   （阶段十五 P2）：新增**云端黄金记忆 ⇒ 本机一键合成进化模型**闭环 ——
#   云端公开端点下发标准 Ollama Modelfile（脱敏后的黄金 Few-Shot），
#   本机 ``POST /api/create`` 合成派生模型（零训练、零显卡），
#   可一键设为当前接单模型；面板新增「🧬 云端黄金记忆本地模型进化」卡片。
#   （阶段十七 P2）：新增 **GPU 温控遥测** —— 心跳与 WS 帧携带
#   ``gpu_temp_c`` / ``gpu_fan_pct``（nvidia-smi **一次调用**取全温度/风扇/显存），
#   云端据此派生 ``overheat_warning``（阈值 75°C）并在业主节点卡片与智能告警中心
#   展示；本机面板新增 GPU 温度 / 风扇徽标（高温高亮）。
#   （阶段十八 P0）：新增 **PoI 签名收据**（Proof-of-Inference）——
#   每次推理完成后节点用 ``node_secret`` 对
#   ``request_id:model_id:prompt_tokens:completion_tokens:sha256(output)``
#   做 HMAC-SHA256 签名，随 ``RESPONSE_DONE`` 帧与 HTTP 心跳上送云端；
#   云端据此识别伪造收据 / 虚报 token / 低熵垃圾。
# v1.0.18（阶段二十 P0/P1/P2）：**局域网多机张量切片合体 + macOS 原生适配 +
#   多网关主备自动容灾** ——
#   · 新增 ``mesh_cluster.py``（Pipeline Mesh：局域网多机显存合体与层切片分配）；
#     配置项 ``mesh_peers`` / ``mesh_enabled`` / ``mesh_mode`` /
#     ``mesh_max_rtt_ms`` / ``mesh_total_layers``；
#   · 心跳与 WS 帧新增 ``mesh_enabled`` / ``cluster_vram_gb`` / ``mesh_shards`` /
#     ``mesh_rtt_ms`` / ``mesh_peers_count`` / ``layer_splits``；
#   · 新增 ``backup_gateway_urls``（多网关主备）+ ``gateway_failover_threshold``。
AGENT_VERSION = "1.0.18"

# 配置文件默认存放位置：项目根目录（与本文件同级）
PROJECT_ROOT = Path(__file__).resolve().parent


class AgentConfigModel(BaseModel):
    """``agent.config.json`` 的字段定义（带运行时校验）。"""

    # ── 本地服务 ──────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = Field(default=52415, ge=1024, le=65535)
    api_base_url: str = "http://127.0.0.1:52415/v1"
    model_name: str = "apipaw-gpt2-small"

    # ── 节点身份 ──────────────────────────────────────────
    agent_version: str = AGENT_VERSION
    node_id: str = "node_default_001"
    node_name: str = "APIPaw-Node"
    hardware_type: str = "desktop"  # desktop | laptop | server
    wallet_address: str = ""
    node_secret: str = ""

    # ── 显存与让退 ────────────────────────────────────────
    device: str = "cuda"
    max_vram_gb: float = Field(default=4.5, ge=0.1, le=1024.0)
    idle_threshold_seconds: float = Field(default=15.0, ge=0.5, le=3600.0)
    vram_yield_ms: int = Field(default=50, ge=10, le=5000)
    poll_interval_ms: int = Field(default=5, ge=1, le=1000)
    # 阶段七 P2：**强制待命模式** —— 忽略键鼠活动，全天候接单。
    # ⚠️ 此前只能靠环境变量 ``APIPAW_FORCE_IDLE=1``（改 start.bat 才能生效），
    #    面板上改不了 ⇒ 挂机党必须手工编辑脚本。这里做成**可持久化配置项**，
    #    面板勾选后立即生效（yield_monitor 每次判定都重读配置）。
    force_idle: bool = False

    # ── 云端联动 ──────────────────────────────────────────
    cloud_api_base: str = "https://cloud.apipaw.com"
    cloud_heartbeat_url: str = "https://cloud.apipaw.com/api/v1/nodes/heartbeat"
    cloud_ws_url: str = "wss://cloud.apipaw.com/api/v1/node/stream"
    heartbeat_interval_seconds: float = Field(default=10.0, ge=1.0, le=3600.0)
    disable_telemetry: bool = False

    # ── 托盘 ──────────────────────────────────────────────
    enable_tray: bool = True

    # ── 其它 ──────────────────────────────────────────────
    log_level: str = "INFO"
    # 本地 HF 分词器路径（可选）：填了且装了 transformers 时，token 计数与本地
    # 推理运行时完全一致；留空则退化为 tiktoken 或启发式计数。
    tokenizer_path: str = ""

    # ── 推理引擎（阶段六 P0 · 模块 A）───────────────────────
    # 后端类型：auto（自动探测 Ollama）/ ollama / vllm / llamacpp。
    # ⚠️ ``auto`` 探不到任何后端 ⇒ 节点**没有**推理引擎，
    #    云端派单会收到明确的 ``backend_unavailable``，**不会**拿到占位文本。
    inference_backend: str = "auto"
    # 后端地址（Ollama 默认 127.0.0.1:11434）。
    backend_base_url: str = "http://127.0.0.1:11434"
    # 后端模型名：留空则用派单载荷里的 model（再留空用 model_name 兜底）。
    backend_model: str = ""
    # 后端超时（替掉 ws_tunnel 里硬编码的 60s）。
    backend_timeout_sec: float = Field(default=60.0, ge=1.0, le=600.0)
    # 阶段六 P3 · 模块 A2：OpenAI 兼容后端（vLLM / llama.cpp）的地址。
    # ⚠️ **留空 = 不参与 auto 探测**。默认端口 8000/8080 常常已经被别的服务
    #    占着（APIPaw 自己的网关就在 8000），盲目探测会把不相干的服务
    #    误认成推理引擎。显式写进来 = 明确"这台机器上确实有 vLLM"。
    backend_vllm_base_url: str = ""
    backend_llamacpp_base_url: str = ""
    # OpenAI 兼容后端的可选 API Key（vLLM --api-key / llama.cpp --api-key）。
    backend_api_key: str = ""

    # ── 阶段二十 P0 · 局域网 Pipeline Mesh（多机显存合体）────
    # 协同节点列表，形如 ``["192.168.1.102:52415"]``（同局域网另一台 Agent）。
    # 留空 = 不组网（默认，绝不影响既有单机行为）。
    mesh_peers: List[str] = Field(default_factory=list)
    # 是否**启用**合体（面板可勾选，热生效）。
    mesh_enabled: bool = False
    # 分片模式：Pipeline Shards（张量切片）/ llama.cpp RPC。
    mesh_mode: str = "Pipeline Shards"
    # 局域网合体的 RTT 上限（毫秒）—— 超过就不合体。
    mesh_max_rtt_ms: float = Field(default=25.0, ge=1.0, le=1000.0)
    # 切层基准：模型总层数（80 层模型按各节点显存比例切分）。
    mesh_total_layers: int = Field(default=80, ge=8, le=4096)

    # ── 阶段二十 P2 · 多网关主备容灾 ────────────────────────
    # 备用网关地址列表（不含主网关）。主网关连续失败或返回 502/503/504 时
    # 自动故障转移；主网关恢复后切回。留空 = 单网关（行为完全不变）。
    backup_gateway_urls: List[str] = Field(default_factory=list)
    # 连续失败多少次才切到备用网关（避免一次网络抖动就搬家）。
    gateway_failover_threshold: int = Field(default=3, ge=1, le=100)


DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 52415,
    "api_base_url": "http://127.0.0.1:52415/v1",
    "model_name": "apipaw-gpt2-small",
    "agent_version": AGENT_VERSION,
    "node_id": "node_default_001",
    "node_name": "APIPaw-Node",
    "hardware_type": "desktop",
    "wallet_address": "",
    "node_secret": "",
    "device": "cuda",
    "max_vram_gb": 4.5,
    "idle_threshold_seconds": 15.0,
    "vram_yield_ms": 50,
    "poll_interval_ms": 5,
    "force_idle": False,
    "cloud_api_base": "https://cloud.apipaw.com",
    "cloud_heartbeat_url": "https://cloud.apipaw.com/api/v1/nodes/heartbeat",
    "cloud_ws_url": "wss://cloud.apipaw.com/api/v1/node/stream",
    "heartbeat_interval_seconds": 10.0,
    "disable_telemetry": False,
    "enable_tray": True,
    "log_level": "INFO",
    "tokenizer_path": "",
    "inference_backend": "auto",
    "backend_base_url": "http://127.0.0.1:11434",
    "backend_model": "",
    "backend_timeout_sec": 60.0,
    "backend_vllm_base_url": "",
    "backend_llamacpp_base_url": "",
    "backend_api_key": "",
    "mesh_peers": [],
    "mesh_enabled": False,
    "mesh_mode": "Pipeline Shards",
    "mesh_max_rtt_ms": 25.0,
    "mesh_total_layers": 80,
    "backup_gateway_urls": [],
    "gateway_failover_threshold": 3,
}


class AgentConfig:
    """
    线程安全的配置管理器。

    默认用法是**单例**（``get_config()`` 全局共享一份）::

        from agent_config import get_config
        cfg = get_config()
        cfg.update(node_name="my-node", wallet_address="0x...")
        print(cfg.port)

    ⚠️ **显式传入 ``config_path`` ⇒ 返回独立实例（不共享单例）**。

    背景（阶段八 P0 修掉的单例污染缺陷）：老实现里 ``__new__`` 无视构造
    参数、永远返回同一个单例，于是 ``AgentConfig(config_path=tmp)`` 会被
    **静默吞掉**，``update()`` 直接改写到**用户真实的**
    ``agent.config.json``（实测把真机配置的 ``force_idle`` /
    ``backend_model`` 冲掉了）。这类污染在测试里尤其致命 —— 跑一次单测
    就把生产/本机的真实配置改了，且**无任何报错**。

    现在的契约：

    - ``AgentConfig()`` / ``get_config()`` ⇒ 单例，路径 = 仓库内
      ``agent.config.json``（行为与旧版完全一致）；
    - ``AgentConfig(config_path=X)`` ⇒ **独立实例**，读写都只发生在 ``X``；
      与单例互不影响，也**绝不会**碰真机配置；
    - ``AgentConfig.reset_instance()`` ⇒ 测试用：丢弃当前单例，
      让下一次 ``get_config()`` 重新从默认路径加载。
    """

    _instance: Optional["AgentConfig"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, config_path: Optional[Path] = None, *args: Any, **kwargs: Any) -> "AgentConfig":
        # ⚠️ 显式给了路径 ⇒ **独立实例**（绝不复用/污染全局单例）
        if config_path is not None:
            instance = super().__new__(cls)
            instance._initialized = False
            instance._isolated = True
            return instance
        # 无参 ⇒ 经典单例（第一次调用决定配置路径）
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    instance._isolated = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.RLock()
        self._config_path = Path(config_path) if config_path else (PROJECT_ROOT / CONFIG_FILE_NAME)
        self._model = self._build_default_model()
        self._initialized = True
        self._load_or_create()

    @classmethod
    def reset_instance(cls) -> None:
        """丢弃当前单例（**仅测试用**）。

        让下一次 ``get_config()`` / ``AgentConfig()`` 重新从默认路径加载，
        避免测试之间互相污染。生产代码不应调用。

        ⚠️ 本模块有**两级缓存**：类属性 ``AgentConfig._instance`` 与模块级
        ``_config_instance``（``get_config()`` 用它）。两者都要清，
        否则清了一半仍然拿回旧实例。
        """
        global _config_instance
        with cls._instance_lock:
            cls._instance = None
        _config_instance = None

    # ───────────────────────── 内部工具 ─────────────────────────

    @staticmethod
    def _build_default_model() -> "AgentConfigModel":
        """构造默认模型；pydantic 缺失时退化为简单字典包装。"""
        try:
            return AgentConfigModel(**DEFAULT_CONFIG)
        except Exception:  # pragma: no cover
            return _SimpleNamespace(**dict(DEFAULT_CONFIG))  # type: ignore[return-value]

    def _model_to_dict(self) -> Dict[str, Any]:
        if hasattr(self._model, "model_dump"):
            return self._model.model_dump()
        if hasattr(self._model, "__dict__"):
            return dict(self._model.__dict__)
        return dict(DEFAULT_CONFIG)

    # ───────────────────────── 读写核心 ─────────────────────────

    def _load_or_create(self) -> None:
        if self._config_path.exists():
            self._load()
        else:
            self._create_default()
            self.save()

    def _load(self) -> None:
        """读取配置；失败则回退默认值并保留损坏文件。"""
        try:
            with open(self._config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            self._model = self._validate(raw)
            print(f"[agent_config] 已加载配置: {self._config_path}")
        except Exception as exc:  # noqa: BLE001 - 任何问题都不应导致启动失败
            backup = self._config_path.with_suffix(self._config_path.suffix + ".broken")
            try:
                shutil.copy2(self._config_path, backup)
                print(f"[agent_config] 配置损坏({exc})，已备份到 {backup.name}，使用默认配置")
            except Exception:  # pragma: no cover
                print(f"[agent_config] 配置损坏({exc})，使用默认配置")
            self._create_default()
            self.save()

    def _create_default(self) -> None:
        self._model = self._build_default_model()

    def _validate(self, raw: Dict[str, Any]) -> "AgentConfigModel":
        """校验并合并默认值（未知字段忽略，缺失字段取默认）。"""
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
        try:
            return AgentConfigModel(**merged)
        except ValidationError as exc:
            print(f"[agent_config] 配置校验失败({exc})，使用默认配置")
            return self._build_default_model()

    def save(self) -> None:
        """原子回写配置到磁盘。"""
        with self._lock:
            data = self._model_to_dict()
            tmp_path = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
            try:
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=4, ensure_ascii=False)
                    fh.write("\n")
                self._atomic_replace(tmp_path)
                print(f"[agent_config] 已保存配置: {self._config_path}")
            except Exception as exc:  # noqa: BLE001
                print(f"[agent_config] 配置保存失败: {exc}")
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:  # pragma: no cover
                    pass

    def _atomic_replace(self, tmp_path: Path) -> None:
        """把临时文件原子替换到目标路径（**对 Windows 的瞬时占用重试**）。

        ⚠️ 历史坑（2026-11-18 复现）：Windows 上 ``os.replace`` 在目标文件
        **正被其它句柄打开**（读方 / 杀软扫描 / 另一个线程）时会抛
        ``[WinError 5] 拒绝访问`` —— 表现为「配置保存失败」，进而让
        绑定 / 拉取模型的写回丢失（``test_phase13_p2_ollama`` 偶发红）。
        这里做有限次重试（总窗口约 1s），把**瞬时占用**与**真失败**区分开。
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(_REPLACE_RETRY_ATTEMPTS):
            try:
                os.replace(tmp_path, self._config_path)
                return
            except PermissionError as exc:  # WinError 5 / 占用中
                last_exc = exc
                time.sleep(_REPLACE_RETRY_DELAY_SEC)
            except OSError as exc:  # 其它 OS 错误：仍重试几次再放弃
                last_exc = exc
                time.sleep(_REPLACE_RETRY_DELAY_SEC)
        raise last_exc if last_exc else OSError("atomic replace failed")

    # ───────────────────────── 公开 API ─────────────────────────

    def update(self, **kwargs: Any) -> None:
        """更新指定字段并回写磁盘；未知字段直接忽略。"""
        with self._lock:
            data = self._model_to_dict()
            data.update({k: v for k, v in kwargs.items() if k in DEFAULT_CONFIG})
            self._model = self._validate(data)
            self.save()

    def reload(self) -> None:
        """从磁盘重新加载配置（供外部工具热更新）。"""
        with self._lock:
            self._load()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return self._model_to_dict()

    # 常用字段的便捷属性
    @property
    def host(self) -> str:
        return self._model.host

    @property
    def port(self) -> int:
        return self._model.port

    @property
    def api_base_url(self) -> str:
        return self._model.api_base_url

    @property
    def model_name(self) -> str:
        return self._model.model_name

    @property
    def node_id(self) -> str:
        return self._model.node_id

    @property
    def agent_version(self) -> str:
        return self._model.agent_version

    @property
    def node_name(self) -> str:
        return self._model.node_name

    @property
    def hardware_type(self) -> str:
        return self._model.hardware_type

    @property
    def wallet_address(self) -> str:
        return self._model.wallet_address

    @property
    def node_secret(self) -> str:
        return self._model.node_secret

    @property
    def device(self) -> str:
        return self._model.device

    @property
    def max_vram_gb(self) -> float:
        return self._model.max_vram_gb

    @property
    def idle_threshold_seconds(self) -> float:
        return self._model.idle_threshold_seconds

    @property
    def idle_threshold_ms(self) -> int:
        return int(self._model.idle_threshold_seconds * 1000)

    @property
    def vram_yield_ms(self) -> int:
        return self._model.vram_yield_ms

    @property
    def poll_interval_ms(self) -> int:
        return self._model.poll_interval_ms

    @property
    def force_idle(self) -> bool:
        return bool(self._model.force_idle)

    @property
    def cloud_api_base(self) -> str:
        return self._model.cloud_api_base

    @property
    def cloud_heartbeat_url(self) -> str:
        return self._model.cloud_heartbeat_url

    @property
    def cloud_ws_url(self) -> str:
        return self._model.cloud_ws_url

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self._model.heartbeat_interval_seconds

    @property
    def disable_telemetry(self) -> bool:
        return self._model.disable_telemetry

    @property
    def enable_tray(self) -> bool:
        return self._model.enable_tray

    @property
    def log_level(self) -> str:
        return self._model.log_level

    @property
    def tokenizer_path(self) -> str:
        return self._model.tokenizer_path

    @property
    def inference_backend(self) -> str:
        return self._model.inference_backend

    @property
    def backend_base_url(self) -> str:
        return self._model.backend_base_url

    @property
    def backend_model(self) -> str:
        return self._model.backend_model

    @property
    def backend_timeout_sec(self) -> float:
        return self._model.backend_timeout_sec

    @property
    def backend_vllm_base_url(self) -> str:
        return self._model.backend_vllm_base_url

    @property
    def backend_llamacpp_base_url(self) -> str:
        return self._model.backend_llamacpp_base_url

    @property
    def backend_api_key(self) -> str:
        return self._model.backend_api_key

    # ── 阶段二十 P0 · Pipeline Mesh ───────────────────────
    @property
    def mesh_peers(self) -> List[str]:
        value = getattr(self._model, "mesh_peers", []) or []
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return [str(p).strip() for p in value if str(p).strip()]

    @property
    def mesh_enabled(self) -> bool:
        return bool(getattr(self._model, "mesh_enabled", False))

    @property
    def mesh_mode(self) -> str:
        return str(getattr(self._model, "mesh_mode", "Pipeline Shards") or "Pipeline Shards")

    @property
    def mesh_max_rtt_ms(self) -> float:
        return float(getattr(self._model, "mesh_max_rtt_ms", 25.0) or 25.0)

    @property
    def mesh_total_layers(self) -> int:
        return int(getattr(self._model, "mesh_total_layers", 80) or 80)

    # ── 阶段二十 P2 · 多网关容灾 ─────────────────────────
    @property
    def backup_gateway_urls(self) -> List[str]:
        value = getattr(self._model, "backup_gateway_urls", []) or []
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return [str(p).strip() for p in value if str(p).strip()]

    @property
    def gateway_failover_threshold(self) -> int:
        return int(getattr(self._model, "gateway_failover_threshold", 3) or 3)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"AgentConfig(node_id={self.node_id!r}, node_name={self.node_name!r}, "
            f"hardware={self.hardware_type!r}, port={self.port}, "
            f"max_vram={self.max_vram_gb}GB, wallet={self.wallet_address!r})"
        )


class _SimpleNamespace:  # pragma: no cover - pydantic 缺失时的兜底
    """极简属性容器，仅在 pydantic 不可用时使用。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


_config_instance: Optional[AgentConfig] = None


def get_config() -> AgentConfig:
    """获取全局配置单例。"""
    global _config_instance
    if _config_instance is None:
        _config_instance = AgentConfig()
    return _config_instance


def config_file_path() -> Path:
    """返回当前生效的配置文件路径。"""
    return get_config()._config_path


__all__ = [
    "AGENT_VERSION",
    "AgentConfig",
    "AgentConfigModel",
    "DEFAULT_CONFIG",
    "CONFIG_FILE_NAME",
    "get_config",
    "config_file_path",
]
