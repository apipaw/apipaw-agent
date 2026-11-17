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
from typing import Any, Dict, Optional

try:
    import requests
except ImportError as exc:  # 缺依赖时降级：心跳失败，但不拖垮节点服务
    requests = None  # type: ignore[assignment]
    _REQUESTS_IMPORT_ERROR = str(exc)
else:
    _REQUESTS_IMPORT_ERROR = None

from agent_config import AGENT_VERSION, get_config
from console import enable_utf8_output
from yield_monitor import NodeState, get_yield_monitor

enable_utf8_output()

# nvidia-smi 查询字段：温度 / 风扇转速 / 显存已用 / 显存总量（MiB）
#
# ⚠️ 阶段十七 P2：风扇转速（fan.speed）并入**同一次**子进程调用 ——
# 分两次查会让每个心跳周期多跑一次 nvidia-smi（Windows 上单次 100~300ms，
# 且会短暂占用 GPU 查询通道）。
_NVIDIA_SMI_FIELDS = "temperature.gpu,fan.speed,memory.used,memory.total"
_NVIDIA_SMI_TIMEOUT_SEC = 3.0
# 云端在 Cloudflare 之后，本地到 CF 链路抖动大（实测单次 GET 成功耗时 2~16s，
# 偶发 TLS 握手超时）。心跳超时给足余量，避免"链路其实通、但被 5s 超时掐断"。
_HTTP_CONNECT_TIMEOUT_SEC = 10.0
_HTTP_TIMEOUT_SEC = 20.0
# 单次上报内的 HTTP 层重试（心跳幂等，可安全重试）
_HTTP_RETRIES = 2

# Cloudflare 侧对 Python 默认客户端 UA（python-urllib / python-requests）有额外
# 拦截风险，表现为 TLS 握手超时（_ssl.c:983/993）；上报统一伪装成浏览器 UA。
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _env_flag_disabled() -> bool:
    """环境变量 ``APIPAW_DISABLE_TELEMETRY`` 关闭遥测。"""
    return os.getenv("APIPAW_DISABLE_TELEMETRY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _ws_primary_active() -> bool:
    """WebSocket 主通道是否在线（在线时心跳由 WS 承担，HTTP 只作兜底）。"""
    try:
        from ws_tunnel import is_ws_connected

        return bool(is_ws_connected())
    except Exception:  # noqa: BLE001 - ws_tunnel 不可用时退回 HTTP 心跳
        return False


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


def _env_float(name: str) -> Optional[float]:
    """读取环境变量并转成 float；无效或未设置 ⇒ ``None``（绝不抛异常）。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _mock_thermals_enabled() -> bool:
    """是否处于「模拟温控注入」模式。

    注入模式下**完全跳过** nvidia-smi 子进程 —— 验收环境（无独显 / CI）
    因此能稳定复现高温告警，且不会因为本机真值而漂移。
    """
    return (
        os.getenv("APIPAW_MOCK_GPU_TEMP_C", "").strip() != ""
        or os.getenv("APIPAW_MOCK_GPU_FAN_PCT", "").strip() != ""
    )


def _sample_nvidia_smi() -> Optional[Dict[str, Optional[float]]]:
    """一次 ``nvidia-smi`` 调用取回温度 / 风扇 / 显存。

    返回 ``None`` 表示命令不可用或输出无法解析（调用方据此降级）。
    **绝不抛异常** —— 采集失败绝不能打断心跳。
    """
    smi = _nvidia_smi_path()
    if not smi:
        return None
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
    except Exception:  # noqa: BLE001 - 采集失败降级
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    line = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    sample: Dict[str, Optional[float]] = {
        "gpu_temp_c": None,
        "gpu_fan_pct": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_free_gb": None,
    }
    # 温度：数值或 "[N/A]"
    try:
        sample["gpu_temp_c"] = float(parts[0])
    except (TypeError, ValueError):
        pass
    # 风扇转速：部分型号（多卡/被动散热）回报 "[N/A]" ⇒ 保留 None
    try:
        sample["gpu_fan_pct"] = float(parts[1])
    except (TypeError, ValueError):
        pass
    try:
        used_mib = float(parts[2])
        total_mib = float(parts[3])
        sample["vram_used_gb"] = round(used_mib / 1024.0, 3)
        sample["vram_total_gb"] = round(total_mib / 1024.0, 3)
        sample["vram_free_gb"] = round(max(0.0, total_mib - used_mib) / 1024.0, 3)
    except (TypeError, ValueError):
        pass
    return sample


def probe_gpu_thermals() -> Dict[str, Optional[float]]:
    """采集 GPU **温度与风扇转速**（阶段十七 P2）。

    返回::

        {
            "gpu_temp_c": 52.0,     # 摄氏度，None 表示不可用
            "gpu_fan_pct": 61.0,    # 风扇转速百分比，None 表示不可用
        }

    规则：
      1. **绝不抛异常** —— 无 NVIDIA 独显 / 命令不可用 / 输出异常一律回退 ``None``；
      2. ``APIPAW_MOCK_GPU_TEMP_C`` / ``APIPAW_MOCK_GPU_FAN_PCT`` 可注入模拟值
         （注入模式**完全跳过** nvidia-smi，保证验收可复现）；
      3. 温度 ≥ 75°C 由**云端**判定为 ``overheat_warning``（口径两边一致）。
    """
    result: Dict[str, Optional[float]] = {
        "gpu_temp_c": None,
        "gpu_fan_pct": None,
    }
    if _mock_thermals_enabled():
        result["gpu_temp_c"] = _env_float("APIPAW_MOCK_GPU_TEMP_C")
        result["gpu_fan_pct"] = _env_float("APIPAW_MOCK_GPU_FAN_PCT")
        return result
    sample = _sample_nvidia_smi()
    if sample:
        result["gpu_temp_c"] = sample.get("gpu_temp_c")
        result["gpu_fan_pct"] = sample.get("gpu_fan_pct")
    return result


def collect_gpu_metrics() -> Dict[str, Optional[float]]:
    """
    采集 GPU 指标。

    返回::

        {
            "gpu_temp_c": 52.0,        # 摄氏度，None 表示不可用
            "gpu_fan_pct": 61.0,       # 风扇转速百分比（阶段十七 P2）
            "vram_used_gb": 3.2,       # 已用显存（GB）
            "vram_total_gb": 6.0,      # 显存总量（GB）
            "vram_free_gb": 2.8,       # 剩余显存（GB）
        }
    """
    metrics: Dict[str, Optional[float]] = {
        "gpu_temp_c": None,
        "gpu_fan_pct": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "vram_free_gb": None,
    }

    # ── 1. nvidia-smi（温度 / 风扇 / 显存，一次调用取全）──
    if _mock_thermals_enabled():
        # 注入模式：跳过子进程（验收环境可复现，且不会因本机真值漂移）
        metrics.update(probe_gpu_thermals())
    else:
        sample = _sample_nvidia_smi()
        if sample:
            metrics.update(sample)
            if sample.get("vram_total_gb") is not None:
                return metrics

    # ── 2. torch（显存兜底；温度 torch 拿不到，保持 None）──
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


# ---- 阶段五 P3：GPU 指标缓存（WS 隧道帧与 HTTP 心跳共用）----
# 阶段五 P3 之后，节点要额外上报**硬件**显存（云端据此判能力等级 C0~C3）。
# 这条数据要同时出现在两个通道里：WS 隧道帧（主通道）与 HTTP 心跳体。
# 若两处各采一次，每个心跳周期就会跑两次 ``nvidia-smi``（Windows 上单次
# 100~300ms，且会短暂占用 GPU 查询通道）—— 故这里加一层短 TTL 缓存。
_GPU_METRICS_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_GPU_METRICS_TTL_SEC = 5.0
_gpu_metrics_lock = threading.Lock()


def collect_gpu_metrics_cached(
    max_age_sec: float = _GPU_METRICS_TTL_SEC,
) -> Dict[str, Optional[float]]:
    """带 TTL 缓存的 GPU 指标采集（阶段五 P3）。

    与 :func:`collect_gpu_metrics` 返回**同样形状**的字典，只是短时间内的
    重复调用直接复用上一次结果。TTL 取 5s —— 心跳周期是 10s，两条通道
    （WS 帧 + HTTP 体）在同一周期内**必然命中缓存**，nvidia-smi 仍只跑一次。

    ⚠️ 采集失败时返回的字典里各字段为 ``None``（不是抛异常）——
    与 :func:`collect_gpu_metrics` 的降级语义保持一致：节点照常上报，
    只是云端拿不到显存 ⇒ 云端保持旧口径结算（**不会**因为缺数据而降薪）。
    """
    with _gpu_metrics_lock:
        cached = _GPU_METRICS_CACHE.get("value")
        age = time.time() - float(_GPU_METRICS_CACHE.get("at") or 0.0)
        if cached is not None and age < float(max_age_sec):
            return cached  # type: ignore[return-value]

    metrics = collect_gpu_metrics()
    with _gpu_metrics_lock:
        _GPU_METRICS_CACHE["at"] = time.time()
        _GPU_METRICS_CACHE["value"] = metrics
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
        # 复用 Session：连接池 + TLS 握手复用，避免每次心跳重新握手被 WAF 掐断
        self._session: Optional[Any] = None

    # ───────────────────────── 采集与组装 ─────────────────────────

    def build_payload(self) -> Dict[str, Any]:
        """组装心跳报文体。"""
        monitor = get_yield_monitor()
        # 阶段五 P3：走缓存版 —— 与 WS 隧道帧共用同一份 nvidia-smi 结果。
        gpu = collect_gpu_metrics_cached()
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
            # 阶段十七 P2：风扇转速（云端据此派生高温告警，阈值 75°C）
            "gpu_fan_pct": gpu.get("gpu_fan_pct"),
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
        # 阶段七 P1：**模型信息**必须随心跳上报。
        # 此前 HTTP 心跳只带 ``model_name``，云端拿不到"本地到底装了哪些模型"
        # ⇒ 只发 HTTP 心跳的节点永远 ``servable_models=0``，还得手工跑脚本绑模型。
        # 这里补齐：``models``（本地可用清单，空数组 = 确认没有推理引擎）、
        # ``backend_model``（本地引擎 tag）、``inference_backend``（后端类型）。
        payload.update(self._model_payload(cfg))
        return payload

    @staticmethod
    def _model_payload(cfg: Any) -> Dict[str, Any]:
        """采集本地模型信息（**任何异常都降级成空**，绝不让心跳失败）。"""
        out: Dict[str, Any] = {
            "models": [],
            "backend_model": str(getattr(cfg, "backend_model", "") or ""),
            "inference_backend": str(getattr(cfg, "inference_backend", "") or ""),
        }
        try:
            import inference_backend as _ib
            snapshot = _ib.describe_backend(cfg, probe_timeout_sec=1.0, use_cache=True)
            models = snapshot.get("models") or []
            cleaned = [str(m).strip() for m in models if str(m).strip()]
            out["models"] = cleaned[:64]
            active = str(snapshot.get("active") or "")
            if active:
                out["inference_backend"] = active
        except Exception as exc:  # noqa: BLE001 - 模型探测失败不影响心跳
            print(f"[telemetry] 读取本地模型清单失败（按空清单上报）: {exc}")
        return out

    # ───────────────────────── 上报 ─────────────────────────

    def _get_session(self) -> Any:
        """获取（并缓存）上报用 HTTP Session，浏览器 UA 写在 Session 默认头里。"""
        if self._session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": _BROWSER_USER_AGENT,
                "Accept": "application/json",
            })
            # 链路抖动时的兜底：连接级重试 + 退避
            retry = requests.adapters.Retry(
                total=_HTTP_RETRIES,
                connect=_HTTP_RETRIES,
                read=_HTTP_RETRIES,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
            )
            adapter = requests.adapters.HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._session = session
        return self._session

    def send_once(self, payload: Optional[Dict[str, Any]] = None) -> bool:
        """上报一次心跳，返回是否成功。"""
        if requests is None:
            self._failed_count += 1
            self._last_error = f"requests 未安装: {_REQUESTS_IMPORT_ERROR}"
            print(f"[telemetry] 缺少 requests 依赖，心跳不可用: {_REQUESTS_IMPORT_ERROR}")
            return False

        body = payload if payload is not None else self.build_payload()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Node-Id": str(self._cfg.node_id),
        }
        if self._cfg.node_secret:
            headers["Authorization"] = f"Bearer {self._cfg.node_secret}"

        try:
            resp = self._get_session().post(
                self._endpoint,
                data=data,
                headers=headers,
                timeout=(_HTTP_CONNECT_TIMEOUT_SEC, _HTTP_TIMEOUT_SEC),
            )
        except requests.exceptions.RequestException as exc:
            self._failed_count += 1
            self._last_error = str(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - 网络异常必须被吞掉
            self._failed_count += 1
            self._last_error = str(exc)
            return False

        ok = 200 <= resp.status_code < 300
        if ok:
            self._consecutive_failures = 0
            self._sent_count += 1
            self._last_sent_at = time.time()
        else:
            self._failed_count += 1
            self._last_error = f"HTTP {resp.status_code}"
            print(f"[telemetry] 心跳被拒绝: HTTP {resp.status_code}")
        return ok

    def _backoff_seconds(self) -> float:
        """失败退避：10s → 20s → 40s … 上限 60s。"""
        if self._consecutive_failures <= 0:
            return self._interval
        return min(60.0, self._interval * (2 ** min(self._consecutive_failures, 4)))

    def _loop(self) -> None:
        ws_primary_announced = False
        while not self._stop_event.is_set():
            # WebSocket 是主通信通道：WS 在线时心跳由隧道上报，
            # 这里只负责在 WS 掉线期间用 HTTP 兜底。
            if _ws_primary_active():
                if not ws_primary_announced:
                    print("[telemetry] WebSocket 主通道已连接，HTTP 心跳转为兜底")
                    ws_primary_announced = True
                self._stop_event.wait(self._interval)
                continue
            if ws_primary_announced:
                print("[telemetry] WebSocket 主通道已断开，恢复 HTTP 心跳上报")
                ws_primary_announced = False
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
        if self.is_disabled():
            print("[telemetry] 遥测已关闭（disable_telemetry / APIPAW_DISABLE_TELEMETRY）")
            return
        if self._thread is not None and self._thread.is_alive():
            print("[telemetry] 心跳线程已在运行")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="TelemetryThread", daemon=True)
        self._thread.start()
        print(f"[telemetry] 启动｜周期 {self._interval:.0f}s｜目标 {self._endpoint}")

    def is_disabled(self) -> bool:
        """遥测是否被关闭（配置文件 / 环境变量 / 命令行开关）。"""
        return bool(
            self._runtime_disabled or self._cfg.disable_telemetry or _env_flag_disabled()
        )

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
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响退出
                print(f"[telemetry] 关闭 HTTP 会话失败: {exc}")
            self._session = None
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
            "channel": "websocket" if _ws_primary_active() else "http",
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
    "collect_gpu_metrics_cached",
    "probe_gpu_thermals",
    "get_reporter",
]
