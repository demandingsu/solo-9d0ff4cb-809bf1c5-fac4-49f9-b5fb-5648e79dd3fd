#!/usr/bin/env python3
"""本地样本目录站（仅标准库）。

- /            → html/ 下的静态样本页（正常目录、残缺目录）
- /mutable/... → 可变目录：前 MUTABLE_FLIP_AFTER 次页面请求返回 v1（更新前），
                 之后的请求返回 v2（更新后），模拟“核对工具翻页抓取途中，
                 目录被编目员更新”的场景
- /mutable/_reset  → 重置为更新前版本（请求计数清零）
- /mutable/_status → 查看当前请求计数与将提供的版本

环境变量：
- PORT               监听端口（默认 80）
- MUTABLE_FLIP_AFTER 第几次 /mutable/ 页面请求后应用目录更新（默认 4）
"""
from __future__ import annotations

import json
import os
import posixpath
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
STATIC_ROOT = BASE / "html"
MUTABLE_ROOT = BASE / "mutable"
PORT = int(os.environ.get("PORT", "80"))
FLIP_AFTER = int(os.environ.get("MUTABLE_FLIP_AFTER", "4"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


class MutableState:
    """记录 /mutable/ 页面请求次数，决定提供更新前（v1）还是更新后（v2）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def hit(self) -> tuple[str, int]:
        """一次可变目录页面请求：返回 (应提供的版本, 第几次请求)。"""
        with self._lock:
            self._count += 1
            n = self._count
        return ("v1" if n <= FLIP_AFTER else "v2"), n

    def reset(self) -> None:
        with self._lock:
            self._count = 0

    def status(self) -> dict:
        with self._lock:
            n = self._count
        return {
            "requests": n,
            "flip_after": FLIP_AFTER,
            "next_version": "v1" if n + 1 <= FLIP_AFTER else "v2",
        }


STATE = MutableState()

RESET_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>演示已重置</title>
<style>body{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#24292f}</style>
</head>
<body>
<h1>演示已重置</h1>
<p>可变目录已回到「更新前」版本，请求计数已清零。下一次翻页抓取将在途中触发目录更新。</p>
<p><a href="/mutable/">打开更新演示目录</a> ｜ <a href="/mutable/_status">查看状态</a> ｜ <a href="/">返回主目录</a></p>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "sample-catalog/1.1"

    def log_message(self, fmt, *args):  # 保持容器日志简洁可查
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    # —— 响应辅助 ——
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _not_found(self, path: str) -> None:
        self._send_html(404, f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>404</title></head>"
                             f"<body><h1>404 Not Found</h1><p>{path}</p></body></html>")

    def _serve_file(self, root: Path, rel: str) -> None:
        rel = posixpath.normpath("/" + rel).lstrip("/")
        root_resolved = root.resolve()
        p = (root_resolved / rel).resolve()
        if p != root_resolved and root_resolved not in p.parents:
            self._not_found(rel)
            return
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file():
            self._not_found(rel)
            return
        ctype = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        self._send(200, p.read_bytes(), ctype)

    # —— 路由 ——
    def do_GET(self) -> None:  # noqa: N802（stdlib 约定）
        path = urlsplit(self.path).path
        if path == "/mutable":
            self.send_response(301)
            self.send_header("Location", "/mutable/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/mutable/_reset":
            STATE.reset()
            self._send_html(200, RESET_PAGE)
            return
        if path == "/mutable/_status":
            self._send(
                200,
                json.dumps(STATE.status(), ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        if path.startswith("/mutable/"):
            version, _ = STATE.hit()
            self._serve_file(MUTABLE_ROOT / version, path[len("/mutable/"):])
            return
        self._serve_file(STATIC_ROOT, path)

    do_HEAD = do_GET


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"样本目录站已启动：http://0.0.0.0:{PORT}/ （第 {FLIP_AFTER} 次可变目录请求后应用目录更新）", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
