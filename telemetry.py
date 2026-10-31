"""
telemetry.py — 节点遥测与心跳上报

职责：
1. 采集本机 GPU 指标（显存占用 / 显存总量 / GPU 温度）
2. 每 10 秒向云端 ``/api/v1/nodes/heartbeat`` 上报节点状态
3. 网络异常不阻塞主流程：失败自动退避重试，永远不抛出到调用方

指标采集策略（按优先级）：
1. ``nvidia-smi`` 子进程 —— 不依赖 torch，Windows / Linux 通用
2. ``torch.cuda`` —— 已安装 torch 时使用
3. 兜底返回 ``None``，心跳照常上报（字段缺失而非报错）
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from agent_config import get_config
from console import enable_utf8_output
from yield_monitor import NodeState, get_yield_monitor

enable_utf8_output()

AGENT_VERSION = "1.0.0"

# nvidia-smi 查询字段：温度 / 显存已用 / 显存总量（MiB）
_NVIDIA_SMI_FIELDS = "temperature.gpu,memory.used,memory.total"
_NVIDIA_SMI_TIMEOUT_SEC = 3.0
_HTTP_TIMEOUT_SEC = 5.0


def _env_flag_disabled() -> bool:
    """环境变量 ``APIPAW_DISABLE_TELEMETRY`` 关闭遥测。"""
    return os.getenv("APIPAW_DISABLE_TELEMETRY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _nvidia_smi_path() -> Optional[str]:
    """定位 nvidia-smi 可执行文件。"""
    env_path = os.getenv("NVIDIA_SMI_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    found = shutil.which("nvidia-smi")
    if found:
        return found
    # Windows 常见安装位置
    default = r"C:\Windows\System32\nvidia-smi.exe"
    if os.path.exists(default):
        return default
    return None


def collect_gpu_metrics() -> Dict[str, Optional[float]]:
    """
    采集 GPU 指标。

    返回::

        {
            "gpu_temp_c": 52.0,        # 摄氏度，None 表示不可用
            "vram_used_gb": 3.2,       # 已用显存（GB）
            "vram_total_gb": 6.0,      # 显存总量（GB）
            "vram_free_gb": 2.8,       # 剩余显存（GB）
        }
    """
    metrics: Dict[str, Optional[float]] = {
        "gpu_temp_c": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_free_gb": None,
    }

    # ── 1. nvidia-smi ──
    smi = _nvidia_smi_path()
    if smi:
        try:
            proc = subprocess.run(
                [
                    smi,
                    f"--query-gpu={_NVIDIA_SMI_FIELDS}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=_NVIDIA_SMI_TIMEOUT_SEC,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                line = proc.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    temp = float(parts[0])
                    used_mib = float(parts[1])
                    total_mib = float(parts[2])
                    metrics["gpu_temp_c"] = temp
                    metrics["vram_used_gb"] = round(used_mib / 1024.0, 3)
                    metrics["vram_total_gb"] = round(total_mib / 1024.0, 3)
                    metrics["vram_free_gb"] = round(
                        max(0.0, total_mib - used_mib) / 1024.0, 3
                    )
                    return metrics
        except Exception:  # noqa: BLE001 - 采集失败降级到 torch
            pass

    # ── 2. torch ──
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            metrics["vram_used_gb"] = round((total_bytes - free_bytes) / (1024**3), 3)
            metrics["vram_total_gb"] = round(total_bytes / (1024**3), 3)
            metrics["vram_free_gb"] = round(free_bytes / (1024**3), 3)
    except Exception:  # noqa: BLE001 - torch 可选
        pass

    return metrics


class TelemetryReporter:
    """
    心跳上报器。

    使用后台守护线程按固定周期上报；网络异常只做退避重试，不影响节点服务。
    """

    def __init__(self, endpoint: Optional[str] = None, interval_seconds: Optional[float] = None):
        self._cfg = get_config()
        self._endpoint = endpoint or self._cfg.cloud_heartbeat_url
        self._interval = float(interval_seconds or self._cfg.heartbeat_interval_seconds)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._last_error: Optional[str] = None
        self._last_sent_at: Optional[float] = None
        self._sent_count = 0
        self._failed_count = 0
        # 运行时开关（CLI --no-telemetry）：只影响本次进程，不写回配置文件
        self._runtime_disabled = False

    # ───────────────────────── 采集与组装 ─────────────────────────

    def build_payload(self) -> Dict[str, Any]:
        """组装心跳报文体。"""
        monitor = get_yield_monitor()
        gpu = collect_gpu_metrics()
        cfg = self._cfg

        payload: Dict[str, Any] = {
            "node_id": cfg.node_id,
            "node_name": cfg.node_name,
            "hardware_type": cfg.hardware_type,
            "wallet_address": cfg.wallet_address,
            "status": monitor.state.name,
            "status_label": monitor.state_label,
            "model_name": cfg.model_name,
            "max_vram_gb": cfg.max_vram_gb,
            "gpu_temp_c": gpu["gpu_temp_c"],
            "vram_used_gb": gpu["vram_used_gb"],
            "vram_total_gb": gpu["vram_total_gb"],
            "vram_free_gb": gpu["vram_free_gb"],
            "yield_count": monitor.yield_count,
            "last_yield_latency_ms": monitor.last_yield_latency_ms,
            "agent_version": AGENT_VERSION,
            "platform": platform.system(),
            "uptime_seconds": round(time.time() - _process_start_time(), 1),
            "timestamp": int(time.time()),
        }
        return payload

    # ───────────────────────── 上报 ─────────────────────────

    def send_once(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        """上报一次心跳，返回是否成功。"""
        body = payload if payload is not None else self.build_payload()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"apipaw-agent/{AGENT_VERSION}",
            "X-Node-Id": str(self._cfg.node_id),
        }
        if self._cfg.node_secret:
            headers["Authorization"] = f"Bearer {self._cfg.node_secret}"

        request = urllib.request.Request(
            self._endpoint, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    self._consecutive_failures = 0
                    self._sent_count += 1
                    self._last_sent_at = time.time()
                else:
                    self._failed_count += 1
                    self._last_error = f"HTTP {resp.status}"
                return ok
        except urllib.error.HTTPError as exc:
            self._failed_count += 1
            self._last_error = f"HTTP {exc.code}"
            print(f"[telemetry] 心跳被拒绝: HTTP {exc.code}")
            return False
        except Exception as exc:  # noqa: BLE001 - 网络异常必须被吞掉
            self._failed_count += 1
            self._last_error = str(exc)
            return False

    def _backoff_seconds(self) -> float:
        """失败退避：10s → 20s → 40s … 上限 60s。"""
        if self._consecutive_failures <= 0:
            return self._interval
        return min(60.0, self._interval * (2 ** min(self._consecutive_failures, 4)))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            ok = self.send_once()
            if ok:
                self._consecutive_failures = 0
                wait = self._interval
            else:
                self._consecutive_failures += 1
                wait = self._backoff_seconds()
                print(
                    f"[telemetry] 心跳上报失败({self._last_error})，{wait:.0f}s 后重试"
                )
            self._stop_event.wait(wait)

    # ───────────────────────── 生命周期 ─────────────────────────

    def start(self) -> None:
        """启动心跳线程。"""
        if self._runtime_disabled or self._cfg.disable_telemetry or _env_flag_disabled():
            print("[telemetry] 遥测已关闭（disable_telemetry / APIPAW_DISABLE_TELEMETRY）")
            return
        if self._thread is not None and self._thread.is_alive():
            print("[telemetry] 心跳线程已在运行")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="TelemetryThread", daemon=True)
        self._thread.start()
        print(f"[telemetry] 启动｜周期 {self._interval:.0f}s｜目标 {self._endpoint}")

    def disable(self) -> None:
        """运行时关闭心跳上报（不修改配置文件，进程退出后自动失效）。"""
        self._runtime_disabled = True
        self.stop()

    def stop(self) -> None:
        """停止心跳线程。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        print("[telemetry] 心跳线程已停止")

    def status(self) -> Dict[str, Any]:
        """心跳线程状态快照。"""
        return {
            "endpoint": self._endpoint,
            "interval_seconds": self._interval,
            "sent_count": self._sent_count,
            "failed_count": self._failed_count,
            "last_error": self._last_error,
            "last_sent_at": self._last_sent_at,
            "running": bool(self._thread and self._thread.is_alive()),
        }


_PROCESS_START_TIME = time.time()


def _process_start_time() -> float:
    return _PROCESS_START_TIME


# ──────────────────────────────────────────────────────────────
# 全局单例
# ──────────────────────────────────────────────────────────────
_reporter_instance: Optional[TelemetryReporter] = None
_reporter_lock = threading.Lock()


def get_reporter() -> TelemetryReporter:
    """获取全局心跳上报器单例。"""
    global _reporter_instance
    if _reporter_instance is None:
        with _reporter_lock:
            if _reporter_instance is None:
                _reporter_instance = TelemetryReporter()
    return _reporter_instance


__all__ = [
    "AGENT_VERSION",
    "TelemetryReporter",
    "collect_gpu_metrics",
    "get_reporter",
]
