"""Local HTTP server that serves the Live2D pet page and pet-control endpoints."""

from __future__ import annotations

import json
import logging
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from action_control import ActionHandler
import thread_registry


class Live2DRequestHandler(SimpleHTTPRequestHandler):
    """避免无控制台打包程序中的 stderr 写入导致请求被中断。"""

    # 由 MainWindow 在启动本地服务前注入；HTTP 线程只通过 Qt 信号回主线程，
    # 所以这里拿到的活对象不会被跨线程直接操作。
    app = None
    timeout = 5

    def log_message(self, format_string, *args):
        logging.info("Live2D HTTP: %s", format_string % args)

    # ---- 桌宠控制接口（供 mcp_servers/aissistant_pet_server.py 调用）----
    # 只做转发：真正的校验/裁剪/登记都在 ActionHandler.execute_tool 里，
    # 与本地直连模式的工具路径逐字一致，不重复实现逻辑。
    def _send_json(self, payload, status=200):
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            body = json.dumps({"success": False, "message": "结果无法序列化：%s" % exc}).encode("utf-8")
            status = 500
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except (UnicodeDecodeError, OSError):
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _route(self):
        return self.path.split("?", 1)[0].rstrip("/")

    def do_GET(self):
        if self._route() == "/v1/pet/params":
            from urllib.parse import parse_qs, urlparse

            keyword = (parse_qs(urlparse(self.path).query).get("keyword") or [""])[0]
            self._send_json(ActionHandler.execute_tool("list_live2d_params", {"keyword": keyword}))
            return
        super().do_GET()

    def do_POST(self):
        route = self._route()
        payload = self._read_json()
        if route == "/v1/pet/emote":
            self._send_json(ActionHandler.execute_tool("control_pet", payload))
            return
        if route == "/v1/pet/param":
            self._send_json(ActionHandler.execute_tool("set_live2d_param", payload))
            return
        self._send_json({"success": False, "message": "未知接口：%s" % route}, 404)


class LocalHTTPServer:
    def __init__(self, directory, port=8123):
        self.directory = directory
        self.port = port
        self.server = None
        self.thread = None

    def _candidate_ports(self):
        yield self.port
        for extra in range(self.port + 1, self.port + 10):
            yield extra

    def start(self):
        index_path = os.path.join(self.directory, "pet.html")
        if not os.path.isfile(index_path):
            raise FileNotFoundError(f"Live2D 页面资源缺失: {index_path}")
        handler = partial(Live2DRequestHandler, directory=self.directory)
        last_error = None
        for port in self._candidate_ports():
            try:
                server = ThreadingHTTPServer(("localhost", port), handler)
            except OSError as exc:
                last_error = exc
                logging.warning("本地HTTP端口 %s 被占用，尝试下一个端口", port)
                continue
            self.port = port
            self.server = server
            self.thread = thread_registry.spawn(
                self.server.serve_forever, name="Live2D-HTTP", group="live2d_server"
            )
            logging.info("本地HTTP服务器已启动: http://localhost:%s，目录：%s", self.port, self.directory)
            return
        raise last_error or OSError("本地HTTP服务器无法启动")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def get_url(self, path=""):
        return f"http://localhost:{self.port}/{path.lstrip('/')}"
