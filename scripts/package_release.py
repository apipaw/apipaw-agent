"""
scripts/package_release.py — 构建 Windows 发行版 zip

用途
----
把仓库裁剪成一份"小白解压即用"的干净发行包，产物形如::

    APIPaw-Agent-v1.0.4-Windows.zip
    └── APIPaw-Agent-v1.0.4/
        ├── install.bat          # 一键装依赖
        ├── start.bat            # 前台启动（可见日志）
        ├── start_silent.vbs     # 后台静默启动（适合开机自启）
        ├── main.py / web_ui.py / ...
        ├── requirements.txt
        ├── 使用说明.md           # 小白向极简说明（不是仓库那篇长 README）
        └── LICENSE

设计取舍
--------
- **白名单而非黑名单**：显式列出要打包的文件，而不是"排除掉某些目录"。
  将来仓库新增文件时，发行包内容不会意外变化——发版内容必须可预期。
- **排除开发资产**：测试代码、探针、CI 脚本、.git、虚拟环境、
  以及含 node_secret 的 agent.config.json 一律不进包。
- **不放仓库的 README.md**：那篇是给开发者看的（含协议、架构、
  内网细节），对新用户是噪音；包内另放一份《使用说明.md》。
- **保留 LICENSE**：MIT 协议要求随分发附带许可证。
- **批处理脚本保持 CRLF**：cmd.exe 解析 LF-only 的 .bat 会出现
  "was unexpected at this time"。脚本统一按 CRLF 重新落盘。

用法::

    python scripts/package_release.py                  # 用 agent_config 里的版本号
    python scripts/package_release.py --version 1.0.5  # 手动指定版本号
    python scripts/package_release.py --out-dir dist   # 指定输出目录
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

# 仓库根目录（本文件位于 <root>/scripts/ 下）
REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 打包白名单 ──────────────────────────────────────────────
# 运行时核心模块：缺任何一个 main.py 都无法启动
RUNTIME_MODULES: Tuple[str, ...] = (
    "main.py",
    "agent_config.py",
    "console.py",
    "telemetry.py",
    "token_counter.py",
    "tray_icon.py",
    "web_ui.py",
    "ws_tunnel.py",
    "yield_monitor.py",
)

# 一键脚本：Windows 用户主要用这三个
WINDOWS_SCRIPTS: Tuple[str, ...] = (
    "install.bat",
    "start.bat",
    "start_silent.vbs",
)

# 跨平台安装脚本（一并带上，方便 macOS / Linux 用户）
CROSS_PLATFORM_SCRIPTS: Tuple[str, ...] = (
    "install.ps1",
    "install.sh",
)

# 其他必带文件
SUPPORT_FILES: Tuple[str, ...] = (
    "requirements.txt",
    "LICENSE",
)

# 必须以 CRLF 落盘的文件（cmd.exe / WScript 对 LF-only 敏感）
CRLF_FILES: Tuple[str, ...] = (
    "install.bat",
    "start.bat",
    "start_silent.vbs",
)

# 包内用户说明的模板来源（单一真源，与 README / Release 正文同源）
QUICKSTART_TEMPLATE = REPO_ROOT / "docs" / "quickstart.md"

# 包内用户说明的文件名
QUICKSTART_NAME = "使用说明.md"

# 发行包文件名规则
ZIP_NAME_PREFIX = "APIPaw-Agent-"
ZIP_NAME_SUFFIX = "-Windows.zip"


def build_zip_name(version: str) -> str:
    """按版本号生成发行包文件名。"""
    return f"{ZIP_NAME_PREFIX}v{version}{ZIP_NAME_SUFFIX}"


def render_quickstart(version: str) -> str:
    """
    渲染双语指南模板（**全端唯一真源**）。

    内容来自 ``docs/quickstart.md``——它同时是 README 标记区、发行包内
    《使用说明.md》、GitHub Release 正文的来源。改文案只改这一个文件，
    其余各端由 ``scripts/sync_docs.py`` 统一分发，杜绝多处手写漂移。
    """
    if not QUICKSTART_TEMPLATE.is_file():
        raise SystemExit(f"[FATAL] 找不到说明模板：{QUICKSTART_TEMPLATE}")

    text = QUICKSTART_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{VERSION}}", version)
    text = text.replace("{{ZIP_NAME}}", build_zip_name(version))
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        raise SystemExit(f"[FATAL] 说明模板仍有未替换的占位符：{sorted(set(leftover))}")
    return text


def _read_bytes(path: Path) -> bytes:
    """读取文件内容（保留原始字节，不做换行转换）。"""
    return path.read_bytes()


def _to_crlf(data: bytes) -> bytes:
    """把文本统一成 CRLF：先归一为 LF，再逐行补 CR。"""
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized.replace(b"\n", b"\r\n")


def collect_files() -> Dict[str, Path]:
    """按白名单收集待打包文件，返回 {包内相对路径: 源文件路径}。"""
    files: Dict[str, Path] = {}

    for name in RUNTIME_MODULES + WINDOWS_SCRIPTS + CROSS_PLATFORM_SCRIPTS + SUPPORT_FILES:
        src = REPO_ROOT / name
        if not src.is_file():
            print(f"[warn] 缺少文件，跳过：{name}")
            continue
        files[name] = src

    return files


def verify_no_secrets(files: Dict[str, Path]) -> None:
    """
    发行包安全闸门：确认没有把本地凭证带进包。

    agent.config.json 本身就不在白名单里，但再加一道内容级检查，
    防止将来某个模块不小心内联了密钥。

    注意：本函数刻意**不硬编码任何真实密钥**——本仓库是公开仓库，
    任何写死的密钥都会随源码泄露。这里改成两类动态检测：
      1. 读取本机 agent.config.json 里真实的 node_secret 值，逐字节比对；
      2. 用正则匹配"疑似密钥字面量"（形如 node_secret = "xxxx"）。
    """
    import re

    # ① 动态：本机真实凭证值（若本机存在配置文件）
    secrets = set()
    local_cfg = REPO_ROOT / "agent.config.json"
    if local_cfg.is_file():
        try:
            data = json.loads(local_cfg.read_text(encoding="utf-8"))
            for key in ("node_secret", "api_key", "access_token"):
                value = data.get(key)
                if isinstance(value, str) and len(value) >= 8:
                    secrets.add(value.encode("utf-8"))
        except (OSError, ValueError):
            pass

    # ② 静态：疑似密钥字面量（占位符/示例除外）
    #    形如 node_secret = "xxxx" / "node_secret": "xxxx" 的赋值
    literal_pattern = re.compile(
        rb"(?i)(?:node_secret|api_key|access_token|secret)\s*[=:]\s*"
        rb"[\"'][A-Za-z0-9_\-]{12,}[\"']"
    )
    #    形如 APIPAW_NODE_SECRET=xxxx 的环境变量赋值（大小写敏感，
    #    避免误伤 os.environ["SOMETHING"] 这类正常引用）
    env_pattern = re.compile(
        rb"(?:APIPAW_[A-Z_]*)?"
        rb"(?:NODE_SECRET|API_KEY|ACCESS_TOKEN|SECRET|TOKEN)"
        rb"\s*=\s*[\"']?[A-Za-z0-9_\-]{12,}"
    )
    placeholder = re.compile(rb"(?i)(xxxx|your[_-]|<[a-z_]+>|placeholder|example)")

    for rel, src in files.items():
        blob = src.read_bytes()
        for secret in secrets:
            if secret in blob:
                raise SystemExit(f"[FATAL] 发行包内发现本机真实凭证：{rel}")
        for match in literal_pattern.finditer(blob):
            if not placeholder.search(match.group(0)):
                raise SystemExit(
                    f"[FATAL] 发行包内发现疑似硬编码密钥：{rel} → {match.group(0)[:60]!r}"
                )
        for match in env_pattern.finditer(blob):
            if not placeholder.search(match.group(0)):
                raise SystemExit(
                    f"[FATAL] 发行包内发现疑似硬编码环境变量密钥：{rel} → {match.group(0)[:60]!r}"
                )

    print("[ok] 密钥扫描通过：发行包不含任何本地凭证")


def build_zip(version: str, out_dir: Path) -> Path:
    """构建发行 zip，返回产物路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = build_zip_name(version)
    zip_path = out_dir / zip_name
    prefix = f"APIPaw-Agent-v{version}"

    files = collect_files()
    verify_no_secrets(files)

    # 已有同名产物先删掉，避免 zip 追加旧内容
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, src in sorted(files.items()):
            data = _read_bytes(src)
            if rel in CRLF_FILES:
                data = _to_crlf(data)
            zf.writestr(f"{prefix}/{rel}", data)
            print(f"  + {rel}  ({len(data)} bytes)")

        quickstart = render_quickstart(version).encode("utf-8")
        zf.writestr(f"{prefix}/{QUICKSTART_NAME}", quickstart)
        print(f"  + {QUICKSTART_NAME}  ({len(quickstart)} bytes)")

    size_kb = zip_path.stat().st_size / 1024.0
    print(f"[ok] 发行包已生成：{zip_path}  ({size_kb:.1f} KB)")
    return zip_path


def smoke_test(zip_path: Path, version: str) -> None:
    """
    冒烟测试：解压到临时目录，确认关键文件齐全、.bat 是 CRLF、
    且 Python 模块能被正常 import（语法/依赖无硬伤）。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            zf.extractall(tmpdir)

        prefix = f"APIPaw-Agent-v{version}"
        root = tmpdir / prefix
        if not root.is_dir():
            raise SystemExit(f"[FATAL] 解压后缺少顶层目录 {prefix}/")

        # 归档清单级校验：绝不能把本地凭证配置打进发行包。
        # 注意必须在 import 自检之前做——agent_config 首次 import 时
        # 会在工作目录自动生成一份默认 agent.config.json，那属于运行期
        # 产物，不是打包内容，不能据此误判。
        for entry in names:
            base = entry.rsplit("/", 1)[-1]
            if base == "agent.config.json":
                raise SystemExit(f"[FATAL] 发行包混入了 agent.config.json：{entry}")

        for name in RUNTIME_MODULES + WINDOWS_SCRIPTS + SUPPORT_FILES:
            if not (root / name).is_file():
                raise SystemExit(f"[FATAL] 发行包缺少必需文件：{name}")
        if not (root / QUICKSTART_NAME).is_file():
            raise SystemExit(f"[FATAL] 发行包缺少 {QUICKSTART_NAME}")

        # 说明文档必须是最新双语版，且占位符已全部替换
        guide = (root / QUICKSTART_NAME).read_text(encoding="utf-8")
        for marker in ("Quick Start | 傻瓜式安装指南", "install.bat", "start.bat"):
            if marker not in guide:
                raise SystemExit(f"[FATAL] {QUICKSTART_NAME} 内容陈旧，缺少「{marker}」")
        for stale in ("{{VERSION}}", "{{ZIP_NAME}}"):
            if stale in guide:
                raise SystemExit(f"[FATAL] {QUICKSTART_NAME} 仍有未替换的占位符 {stale}")

        for name in CRLF_FILES:
            blob = (root / name).read_bytes()
            if b"\r\n" not in blob:
                raise SystemExit(f"[FATAL] {name} 不是 CRLF 换行，cmd.exe 会解析失败")
            if b"\n" in blob.replace(b"\r\n", b""):
                raise SystemExit(f"[FATAL] {name} 混入了 LF 裸换行")

        # 用当前解释器做一次 import 自检（只验证可导入，不启动服务）
        import subprocess

        probe = (
            "import sys; sys.path.insert(0, %r); "
            "import main, agent_config, web_ui, ws_tunnel, telemetry, "
            "token_counter, tray_icon, yield_monitor, console; "
            "print('import ok', agent_config.AGENT_VERSION)"
        ) % (str(root),)
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise SystemExit("[FATAL] 发行包 import 自检失败")
        print(f"[ok] 冒烟测试通过：{proc.stdout.strip()}")

        # import 自检会在工作目录生成默认配置，清理掉，避免误解
        generated = root / "agent.config.json"
        if generated.exists():
            generated.unlink()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="package_release",
        description="构建 APIPaw Agent 的 Windows 发行版 zip",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="版本号（默认读取 agent_config.AGENT_VERSION）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "dist"),
        help="输出目录（默认 <repo>/dist）",
    )
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        help="跳过冒烟测试（不推荐）",
    )
    args = parser.parse_args(argv)

    version = args.version
    if not version:
        sys.path.insert(0, str(REPO_ROOT))
        from agent_config import AGENT_VERSION

        version = AGENT_VERSION

    out_dir = Path(args.out_dir).resolve()
    print(f"[package] 版本 v{version} → {out_dir}")

    zip_path = build_zip(version, out_dir)

    if not args.no_smoke_test:
        smoke_test(zip_path, version)

    return 0


if __name__ == "__main__":
    sys.exit(main())
