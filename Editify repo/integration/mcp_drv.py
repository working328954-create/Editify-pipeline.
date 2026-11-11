"""Stdio JSON-RPC driver for the DaVinci Resolve MCP server (compound server).

Usage:  python mcp_drv.py < batch.jsonl
Each stdin line: {"tool": "<name>", "params": {...}}  -> one tools/call.
Prints one JSON result line per call; image content saved under out/.
"""
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time

MCP_ROOT = r"C:\Users\ASUS\AppData\Local\davinci-resolve-mcp"
PY = os.path.join(MCP_ROOT, "venv", "Scripts", "python.exe")
HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "out")

CALL_TIMEOUT = 300  # first call attaches to Resolve; give it room


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(OUTDIR, exist_ok=True)
    proc = subprocess.Popen(
        [PY, "src/server.py"], cwd=MCP_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
    )
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()

    next_id = 0

    def rpc(method, params=None):
        nonlocal next_id
        next_id += 1
        msg = {"jsonrpc": "2.0", "id": next_id, "method": method}
        if params is not None:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        deadline = time.time() + CALL_TIMEOUT
        while time.time() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("id") == next_id:
                return data
        return {"error": {"code": -1, "message": "timeout waiting for response"}}

    init = rpc("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "mcp_drv", "version": "0.1"},
    })
    if "error" in init or "result" not in init:
        print(json.dumps({"phase": "initialize", **{k: v for k, v in init.items() if k != "id"}}))
        proc.terminate()
        return 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()

    call_no = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        req = json.loads(raw)
        call_no += 1
        res = rpc("tools/call", {"name": req["tool"], "arguments": req.get("params", {})})
        out = {"tool": req["tool"], "id": res.get("id")}
        if "error" in res:
            out["rpc_error"] = res["error"]
        else:
            content = res.get("result", {}).get("content", [])
            texts, images = [], []
            for i, c in enumerate(content):
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "image":
                    fn = os.path.join(OUTDIR, "img_%03d_%02d_%s.png" % (call_no, i, req["tool"].replace("/", "_")))
                    try:
                        with open(fn, "wb") as f:
                            f.write(base64.b64decode(c.get("data", "")))
                        images.append(fn)
                    except Exception as e:
                        images.append("save_failed: %s" % e)
            out["text"] = "\n".join(texts)
            if images:
                out["images"] = images
            out["isError"] = res.get("result", {}).get("isError", False)
        print(json.dumps(out))
        sys.stdout.flush()

    proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
