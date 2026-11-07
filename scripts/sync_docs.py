"""
scripts/sync_docs.py — 中英双语指南的全端同步器

背景
----
v1.0.4 之前，中英双语使用指南分散在三处手写：README、GitHub Release 正文、
发行包内的《使用说明.md》。三处各改各的，结果 ZIP 停留在旧版文案——
这就是发布事故的根源。

本脚本把 ``docs/quickstart.md`` 确立为**唯一真源**，并把它的渲染结果
分发到所有消费端：

    docs/quickstart.md
        │
        ├─► README.md         （注入 <!-- QUICKSTART:BEGIN --> 标记区）
        ├─► ZIP 内《使用说明.md》（package_release.py 调用 render_template）
        └─► GitHub Release 正文（--push-release，按需上传）

模板里用 ``{{VERSION}}`` / ``{{ZIP_NAME}}`` 两个占位符，渲染时替换成
实际版本号与压缩包名。模板**不随版本变化**——版本号由代码单一维护
（``agent_config.AGENT_VERSION``），文案改动一律改模板。

用法::

    python scripts/sync_docs.py --apply          # 把模板写进 README
    python scripts/sync_docs.py --check          # 只校验，不写盘（CI 用）
    python scripts/sync_docs.py --apply --push-release   # 顺带上传 Release 正文
    python scripts/sync_docs.py --apply --push-asset     # 顺带重传发行包

``--check`` 会在任何一端不一致时返回非零退出码，可直接接入 CI 闸门。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

# 仓库根目录（本文件位于 <root>/scripts/ 下）
REPO_ROOT = Path(__file__).resolve().parent.parent

# 让 package_release.py 能直接复用本模块的渲染逻辑
sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_release import (  # noqa: E402
    QUICKSTART_NAME,
    build_zip_name,
    render_quickstart,
)

# 客户端版本号（与心跳上报 / 云端注册表保持一致）
sys.path.insert(0, str(REPO_ROOT))
from agent_config import AGENT_VERSION  # noqa: E402

# README 中的注入标记区
README_PATH = REPO_ROOT / "README.md"
MARKER_BEGIN = "<!-- QUICKSTART:BEGIN -->"
MARKER_END = "<!-- QUICKSTART:END -->"

# 发行包顶层目录前缀
ZIP_PREFIX = f"APIPaw-Agent-v{AGENT_VERSION}"


def zip_name(version: str) -> str:
    """发行包文件名。"""
    return build_zip_name(version)


def render_template(version: str) -> str:
    """
    渲染双语指南模板。

    复用 ``package_release.render_quickstart``——它是全端唯一的渲染实现，
    保证 README / 发行包 / Release 正文三处逐字节一致。
    """
    return render_quickstart(version)


def _readme_region(text: str) -> Optional[Tuple[int, int]]:
    """在 README 文本中定位标记区，返回 (起始, 结束) 偏移。找不到返回 None。"""
    begin = text.find(MARKER_BEGIN)
    end = text.find(MARKER_END)
    if begin == -1 or end == -1:
        return None
    if end < begin:
        raise SystemExit("[FATAL] README 标记区顺序颠倒")
    return begin, end


def build_readme_region(version: str) -> str:
    """构造要写入 README 的标记区内容（含标记本身）。"""
    rendered = render_template(version)
    return f"{MARKER_BEGIN}\n\n{rendered}\n{MARKER_END}"


def apply_to_readme(version: str) -> bool:
    """
    把渲染后的模板写入 README 的标记区。

    返回 True 表示确实发生了改动。
    """
    if not README_PATH.is_file():
        raise SystemExit(f"[FATAL] 找不到 README：{README_PATH}")

    text = README_PATH.read_text(encoding="utf-8")
    region = build_readme_region(version)

    located = _readme_region(text)
    if located is None:
        # 首次注入：放在语言导航之后的第一个分隔线下方
        anchor = f"{MARKER_BEGIN}\n"
        nav_idx = text.find("**语言 / Language**")
        if nav_idx == -1:
            raise SystemExit("[FATAL] README 中找不到语言导航锚点")
        sep_idx = text.find("\n---\n", nav_idx)
        if sep_idx == -1:
            raise SystemExit("[FATAL] README 中找不到导航后的分隔线")
        insert_at = sep_idx + len("\n---\n")
        new_text = text[:insert_at] + "\n" + region + "\n\n" + text[insert_at:]
    else:
        begin, end = located
        new_text = text[:begin] + region + text[end + len(MARKER_END):]

    if new_text == text:
        print("[ok] README 已是最新，无需改动")
        return False

    README_PATH.write_text(new_text, encoding="utf-8", newline="\n")
    print("[ok] README 已同步双语指南")
    return True


def check_readme(version: str) -> bool:
    """校验 README 的标记区是否与模板一致。"""
    if not README_PATH.is_file():
        raise SystemExit(f"[FATAL] 找不到 README：{README_PATH}")

    text = README_PATH.read_text(encoding="utf-8")
    located = _readme_region(text)
    if located is None:
        print("[FAIL] README 中找不到同步标记区")
        return False

    begin, end = located
    current = text[begin:end + len(MARKER_END)]
    expected = build_readme_region(version)
    if current == expected:
        print("[ok] README 与模板一致")
        return True

    print("[FAIL] README 与模板不一致 —— 请执行：python scripts/sync_docs.py --apply")
    return False


def check_zip(version: str, zip_path: Path) -> bool:
    """校验发行包内的《使用说明.md》是否与模板一致。"""
    if not zip_path.is_file():
        print(f"[FAIL] 发行包不存在：{zip_path}")
        return False

    with zipfile.ZipFile(zip_path) as zf:
        entry = f"{ZIP_PREFIX}/{QUICKSTART_NAME}"
        if entry not in zf.namelist():
            print(f"[FAIL] 发行包内缺少 {QUICKSTART_NAME}")
            return False
        packaged = zf.read(entry).decode("utf-8")

    if packaged == render_template(version):
        print("[ok] 发行包内说明与模板一致")
        return True

    print("[FAIL] 发行包内说明与模板不一致 —— 请重新执行 package_release.py")
    return False


def _github_token() -> Optional[str]:
    """从环境变量或 Git Credential Manager 取 token。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token

    # GCM：git credential fill
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=b"protocol=https\nhost=github.com\n\n",
            capture_output=True,
            cwd=str(REPO_ROOT),
        )
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001 - 取不到 token 不是致命错误，交由上层判断
        pass
    return None


def _api_request(
    method: str, url: str, token: str, payload: Optional[bytes] = None,
    content_type: str = "application/json",
) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": content_type,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "apipaw-sync-docs",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise SystemExit(f"[FATAL] GitHub API {method} {url} → HTTP {exc.code}: {detail}")


def push_release_body(version: str, tag: str = "v1.0.4") -> None:
    """把渲染后的模板覆盖到 GitHub Release 正文。"""
    token = _github_token()
    if not token:
        raise SystemExit("[FATAL] 找不到 GitHub token（设置 GITHUB_TOKEN 环境变量）")

    rel_url = f"https://api.github.com/repos/apipaw/apipaw-agent/releases/tags/{tag}"
    release = _api_request("GET", rel_url, token)
    release_id = release["id"]

    body = render_template(version)
    payload = json.dumps({"body": body}).encode("utf-8")
    _api_request("PATCH", f"https://api.github.com/repos/apipaw/apipaw-agent/releases/{release_id}",
                 token, payload)
    print(f"[ok] Release 正文已覆盖（{tag}）")


def push_release_asset(version: str, tag: str = "v1.0.4") -> None:
    """重新构建并替换 Release 资产（先删旧、再传新）。"""
    token = _github_token()
    if not token:
        raise SystemExit("[FATAL] 找不到 GitHub token（设置 GITHUB_TOKEN 环境变量）")

    out_dir = REPO_ROOT / "dist"
    zip_path = out_dir / zip_name(version)
    if not zip_path.is_file():
        print("[..] 发行包不存在，先执行 package_release.py")
        import subprocess

        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "package_release.py")], check=True)
    if not zip_path.is_file():
        raise SystemExit("[FATAL] 发行包构建失败")

    rel_url = f"https://api.github.com/repos/apipaw/apipaw-agent/releases/tags/{tag}"
    release = _api_request("GET", rel_url, token)
    release_id = release["id"]

    # 删掉同名旧资产
    for asset in release.get("assets", []):
        if asset["name"] == zip_name(version):
            _api_request("DELETE",
                         f"https://api.github.com/repos/apipaw/apipaw-agent/releases/assets/{asset['id']}",
                         token)
            print(f"[ok] 已删除旧资产 {asset['name']}")

    # 上传新资产
    upload_url = (
        "https://uploads.github.com/repos/apipaw/apipaw-agent/releases/"
        f"{release_id}/assets?name={urllib.parse.quote(zip_name(version))}"
    )
    with zip_path.open("rb") as fh:
        data = fh.read()
    result = _api_request("POST", upload_url, token, data, content_type="application/zip")
    print(f"[ok] 资产已上传：{result['name']}（{result['size']} 字节）")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync_docs",
        description="把 docs/quickstart.md 同步到 README / 发行包 / GitHub Release",
    )
    parser.add_argument("--version", default=None, help="版本号（默认读 agent_config）")
    parser.add_argument("--apply", action="store_true", help="把模板写入 README")
    parser.add_argument("--check", action="store_true", help="只校验各端是否一致")
    parser.add_argument("--push-release", action="store_true", help="覆盖 GitHub Release 正文")
    parser.add_argument("--push-asset", action="store_true", help="重传 Release 资产")
    parser.add_argument("--tag", default="v1.0.4", help="目标 Release 标签")
    args = parser.parse_args(argv)

    version = args.version or AGENT_VERSION
    out_dir = REPO_ROOT / "dist"

    if args.apply:
        apply_to_readme(version)

    if args.push_release:
        push_release_body(version, args.tag)

    if args.push_asset:
        push_release_asset(version, args.tag)

    if args.check or not (args.apply or args.push_release or args.push_asset):
        ok = check_readme(version)
        ok = check_zip(version, out_dir / zip_name(version)) and ok
        if not ok:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
