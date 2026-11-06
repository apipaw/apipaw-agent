"""Agent 侧真实 token 上报自检。

用法（在仓库根目录执行）：
    ./.venv/Scripts/python.exe test_usage_report.py

覆盖：
  1. ``token_counter``：CJK / 英文 / 混合文本的计数与 usage 结构
  2. 本地 ``/v1/chat/completions`` 非流式：响应带真实 usage
  3. 本地 ``/v1/chat/completions`` 流式：结束帧带真实 usage
     （云端据此覆盖预估、按真实 token 精确计费）

测试完全离线：强制关闭遥测（不连生产云端），只监听本地临时端口。
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.request

# 必须在导入 main 之前设置：关闭遥测（不连生产云端）+ 强制 IDLE（避免让退检测拦截）
os.environ["APIPAW_DISABLE_TELEMETRY"] = "1"
os.environ["APIPAW_FORCE_IDLE"] = "1"

RESULTS = []


def _record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (" | " + detail if detail else ""))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _start_server(port):
    import uvicorn

    import main as agent_main

    config = uvicorn.Config(agent_main.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if getattr(srv, "started", False):
            return srv, thread
        time.sleep(0.1)
    raise RuntimeError("本地 Agent 服务启动超时")


def _check_counter():
    import token_counter

    _record("token_counter：CJK 文本约 1 字 1 token",
            token_counter.count_tokens("一二三四五") == 5,
            "count=%d" % token_counter.count_tokens("一二三四五"))
    _record("token_counter：英文文本明显少于字符数",
            0 < token_counter.count_tokens("hello world, how are you?") < 20,
            "count=%d" % token_counter.count_tokens("hello world, how are you?"))
    _record("token_counter：空文本计 0",
            token_counter.count_tokens("") == 0)
    usage = token_counter.build_usage(7, 11)
    _record("token_counter：usage 结构符合 OpenAI 规范",
            usage == {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
            json.dumps(usage))
    _record("token_counter：prompt 计数含消息协议开销",
            token_counter.count_prompt_tokens([{"role": "user", "content": "你好"}])
            > token_counter.count_tokens("你好"),
            "backend=%s" % token_counter.backend_name())


def _check_api(port):
    url = "http://127.0.0.1:%d/v1/chat/completions" % port
    prompt = "请用一句话解释什么是去中心化算力网络。"

    # ---- 非流式 ----
    body = json.loads(_post_json(url, {
        "model": "apipaw-gpt2-small",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }))
    usage = body.get("usage") or {}
    _record("非流式响应带 usage",
            bool(usage), json.dumps(usage))
    _record("非流式 usage 的 prompt_tokens > 0",
            int(usage.get("prompt_tokens") or 0) > 0,
            str(usage.get("prompt_tokens")))
    _record("非流式 usage 的 completion_tokens > 0",
            int(usage.get("completion_tokens") or 0) > 0,
            str(usage.get("completion_tokens")))
    _record("非流式 usage 的 total_tokens 等于两项之和",
            int(usage.get("total_tokens") or 0)
            == int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0),
            json.dumps(usage))

    # ---- 流式 ----
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "model": "apipaw-gpt2-small",
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    frames = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            frames.append(json.loads(payload))
        except ValueError:
            continue
    _record("流式响应有多个分片", len(frames) >= 2, "frames=%d" % len(frames))
    stream_usage = {}
    for frame in frames:
        if isinstance(frame.get("usage"), dict):
            stream_usage = frame["usage"]
    _record("流式结束帧带真实 usage", bool(stream_usage),
            json.dumps(stream_usage))
    _record("流式 usage 的 total_tokens 等于两项之和",
            int(stream_usage.get("total_tokens") or 0)
            == int(stream_usage.get("prompt_tokens") or 0) + int(stream_usage.get("completion_tokens") or 0),
            json.dumps(stream_usage))
    _record("流式与非流式的 prompt_tokens 一致（同一提示词）",
            int(stream_usage.get("prompt_tokens") or 0) == int(usage.get("prompt_tokens") or 0),
            "stream=%s non-stream=%s" % (stream_usage.get("prompt_tokens"),
                                         usage.get("prompt_tokens")))


def main():
    _check_counter()
    port = _free_port()
    print("[test] 本地 Agent 端口: %d" % port)
    try:
        srv, thread = _start_server(port)
    except Exception as exc:
        print("[test] 本地服务启动失败: %s" % exc)
        return 1
    try:
        _check_api(port)
    finally:
        srv.should_exit = True
        thread.join(timeout=10)

    failed = [r for r in RESULTS if not r[1]]
    print("\n[test] 用例 %d 个，通过 %d 个，失败 %d 个"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
