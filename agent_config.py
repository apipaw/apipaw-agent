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
from typing import Any, Dict, Optional

from console import enable_utf8_output

enable_utf8_output()

try:  # pydantic v2
    from pydantic import BaseModel, Field, ValidationError
except ImportError:  # pragma: no cover - 依赖缺失时降级为无校验模式
    BaseModel = object  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
    ValidationError = ValueError  # type: ignore[assignment]


CONFIG_FILE_NAME = "agent.config.json"

# 客户端版本号（与心跳上报 / 云端注册表保持一致）
AGENT_VERSION = "1.0.2"

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
    "cloud_api_base": "https://cloud.apipaw.com",
    "cloud_heartbeat_url": "https://cloud.apipaw.com/api/v1/nodes/heartbeat",
    "cloud_ws_url": "wss://cloud.apipaw.com/api/v1/node/stream",
    "heartbeat_interval_seconds": 10.0,
    "disable_telemetry": False,
    "enable_tray": True,
    "log_level": "INFO",
}


class AgentConfig:
    """
    线程安全的配置管理器（单例）。

    典型用法::

        from agent_config import get_config
        cfg = get_config()
        cfg.update(node_name="my-node", wallet_address="0x...")
        print(cfg.port)
    """

    _instance: Optional["AgentConfig"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "AgentConfig":
        # 接受并忽略构造参数：单例只允许第一次调用决定配置路径
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
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
                os.replace(tmp_path, self._config_path)
                print(f"[agent_config] 已保存配置: {self._config_path}")
            except Exception as exc:  # noqa: BLE001
                print(f"[agent_config] 配置保存失败: {exc}")
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:  # pragma: no cover
                    pass

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
