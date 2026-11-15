#!/usr/bin/env python3
"""本地 HTTP 服务：用 Python 标准库提供界面和 JSON 接口。

启动：python3 -m app.server --port 8000
数据文件默认在 data/ledger.sqlite3，可用 --db 指定。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ledger import Ledger, LedgerError, NotFoundError  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DB = BASE_DIR.parent / "data" / "ledger.sqlite3"
MAX_BODY = 2 * 1024 * 1024


class Api:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.ledger = Ledger(self.db_path)

    def route(self, method: str, path: str, query: dict[str, list[str]], body: bytes):
        """返回 (status, content_type, payload bytes, extra headers)。"""
        if not path.startswith("/api/"):
            return None

        def arg(name: str):
            values = query.get(name)
            return values[0] if values else None

        try:
            payload = self._handle(method, path, arg, body)
        except LedgerError as exc:
            return self._json(exc.status, exc.to_dict())
        except json.JSONDecodeError:
            return self._json(400, {"error": "请求内容不是合法的 JSON"})
        except Exception as exc:  # pragma: no cover - 兜底，避免服务崩溃
            print(f"[error] {method} {path}: {exc}", file=sys.stderr)
            return self._json(500, {"error": f"服务器内部错误：{exc}"})
        if isinstance(payload, tuple):
            return payload
        return self._json(200, payload)

    @staticmethod
    def _json(status: int, data) -> tuple:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return status, "application/json; charset=utf-8", raw, {}

    @staticmethod
    def _csv(text: str, filename: str) -> tuple:
        return (
            200,
            "text/csv; charset=utf-8",
            text.encode("utf-8"),
            {"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _handle(self, method: str, path: str, arg, body: bytes):
        data = {}
        if body:
            try:
                data = json.loads(body.decode("utf-8"))
            except UnicodeDecodeError:
                raise LedgerError("请求内容不是 UTF-8 文本")
            if not isinstance(data, dict):
                raise LedgerError("请求内容必须是 JSON 对象")

        L = self.ledger

        if path == "/api/health" and method == "GET":
            return {"ok": True, "db": str(self.db_path)}

        if path == "/api/inventory" and method == "GET":
            seq = arg("seq")
            return L.inventory(int(seq) if seq not in (None, "") else None)

        if path == "/api/history" and method == "GET":
            limit = arg("limit")
            return L.history(int(limit) if limit else None)

        if path == "/api/operation" and method == "GET":
            op_id = arg("id")
            if not op_id:
                raise LedgerError("缺少操作序号")
            return L.operation_detail(int(op_id))

        if path == "/api/tube" and method == "GET":
            code = arg("code")
            if not code:
                raise LedgerError("缺少管编号")
            seq = arg("seq")
            return L.tube_trace(code, int(seq) if seq not in (None, "") else None)

        if path == "/api/batch" and method == "GET":
            batch = arg("batch")
            if not batch:
                raise LedgerError("缺少批次")
            seq = arg("seq")
            return L.batch_trace(batch, int(seq) if seq not in (None, "") else None)

        if path == "/api/recalls" and method == "GET":
            return L.recalls()

        if path == "/api/recall" and method == "GET":
            recall_id = arg("id")
            if not recall_id:
                raise LedgerError("缺少召回号")
            return L.recall_detail(int(recall_id))

        if path == "/api/suggest-codes" and method == "GET":
            count = int(arg("count") or 3)
            return {"codes": L.suggest_codes(count)}

        if path == "/api/intake" and method == "POST":
            return L.register_intake(
                data.get("code"), data.get("batch"), data.get("volume"), data.get("note", "")
            )

        if path == "/api/split" and method == "POST":
            return L.split(data.get("inputs"), data.get("outputs"), data.get("note", ""))

        if path == "/api/retire" and method == "POST":
            return L.retire(data.get("tubes"), data.get("note", ""))

        if path == "/api/lyophilize" and method == "POST":
            return L.lyophilize(data.get("tubes"), data.get("note", ""))

        if path == "/api/reconstitute" and method == "POST":
            return L.reconstitute(data.get("tubes"), data.get("note", ""))

        if path == "/api/recall" and method == "POST":
            return L.recall(data.get("batch"), data.get("note", ""))

        if path == "/api/export/inventory" and method == "GET":
            return self._csv(L.inventory_csv(), "inventory.csv")

        if path == "/api/export/recalls" and method == "GET":
            return self._csv(L.recalls_csv(), "recalls.csv")

        raise NotFoundError(f"接口 {path} 不存在")


class Handler(BaseHTTPRequestHandler):
    server_version = "SampleLedger/1.0"
    api: Api

    def log_message(self, fmt, *args):  # 安静一点，避免刷屏
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, content_type, payload, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            exc = LedgerError("请求内容过大")
            exc.status = 413
            raise exc
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                body = self._read_body() if method == "POST" else b""
                result = self.api.route(method, path, query, body)
                self._send(*result)
                return
            self._serve_static(path)
        except LedgerError as exc:
            self._send(*Api._json(getattr(exc, "status", 400), exc.to_dict()))
        except Exception as exc:  # pragma: no cover
            self._send(*Api._json(500, {"error": f"服务器内部错误：{exc}"}))

    def _serve_static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._send(403, "text/plain; charset=utf-8", b"forbidden")
            return
        if not target.is_file():
            self._send(404, "text/plain; charset=utf-8", "找不到页面".encode("utf-8"))
            return
        content_type, _ = mimetypes.guess_type(str(target))
        content_type = content_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send(200, content_type, target.read_bytes())


def make_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 8000):
    api = Api(Path(db_path))
    handler = type("BoundHandler", (Handler,), {"api": api})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地样品台账")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据文件路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args(argv)
    server = make_server(args.db, args.host, args.port)
    print(f"样品台账已启动：http://{args.host}:{args.port}")
    print(f"数据文件：{Path(args.db).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
