import argparse
import json
import re
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from media import Registry, render
from timeline import baseline, edit

HERE = Path(__file__).parent
MAX_BODY = 100_000


class State:
    def __init__(self, root, work, ffmpeg):
        self.registry = Registry(root)
        self.work = Path(work)
        self.ffmpeg = ffmpeg
        self.token = secrets.token_urlsafe(32)
        self.sources = []
        self.plan = None
        self.artifact = None
        self.render_state = {"status": "idle"}
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()

    def snapshot(self):
        return {
            "sources": [asdict(source) for source in self.sources],
            "plan": asdict(self.plan) if self.plan else None,
            "render": self.render_state,
        }


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MomentDraftLocal/1"

        def _safe(self):
            host = self.headers.get("Host", "")
            expected = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return host == expected and (origin is None or origin == f"http://{expected}")

        def _headers(self, status, kind, length, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()

        def _bytes(self, status, data, kind="application/json; charset=utf-8", extra=None):
            self._headers(status, kind, len(data), extra)
            self.wfile.write(data)

        def _json(self, status, value):
            self._bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def _error(self, status, message):
            self._json(status, {"error": message})

        def _authorized(self, query=None):
            return secrets.compare_digest(self.headers.get("X-Editor-Capability", "") if query is None else query, state.token)

        def do_GET(self):
            if not self._safe():
                return self._error(403, "このページから操作してください。")
            parsed = urlsplit(self.path)
            route = parsed.path
            if route in ("/", "/editor.js", "/editor.css"):
                name = "index.html" if route == "/" else route[1:]
                content = (HERE / name).read_bytes()
                if route == "/":
                    content = content.replace(b"__CAPABILITY__", state.token.encode("ascii"))
                kind = "text/html; charset=utf-8" if route == "/" else "text/javascript; charset=utf-8" if route.endswith(".js") else "text/css; charset=utf-8"
                return self._bytes(200, content, kind)
            if route == "/api/state":
                if not self._authorized():
                    return self._error(403, "このページから操作してください。")
                with state.lock:
                    return self._json(200, state.snapshot())
            match = re.fullmatch(r"/artifact/([A-Za-z0-9_-]{24})\.mp4", route)
            if match:
                query = parse_qs(parsed.query)
                if not self._authorized(query.get("cap", [""])[0]):
                    return self._error(403, "このページから操作してください。")
                with state.lock:
                    current = state.artifact
                    if not current or current[0] != match[1] or current[2] != state.plan.revision:
                        return self._error(404, "新しいプレビューを作成してください。")
                    path = current[1]
                return self._movie(path)
            return self._error(404, "ページが見つかりません。")

        def _movie(self, path):
            size = path.stat().st_size
            header = self.headers.get("Range")
            if header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
                if not match:
                    return self._error(416, "再生位置を確認してください。")
                start = int(match[1])
                end = int(match[2]) if match[2] else min(size - 1, start + 8_388_607)
                if start >= size or end < start or end >= size or end - start + 1 > 8_388_608:
                    return self._error(416, "再生位置を確認してください。")
                extra = {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"}
                status = 206
            else:
                start, end, status = 0, size - 1, 200
                extra = {"Accept-Ranges": "bytes"}
            self._headers(status, "video/mp4", end - start + 1, extra)
            with path.open("rb") as handle:
                handle.seek(start)
                left = end - start + 1
                while left:
                    block = handle.read(min(left, 1_048_576))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)

        def do_POST(self):
            if not self._safe() or not self._authorized():
                return self._error(403, "このページから操作してください。")
            if self.headers.get("Content-Type") != "application/json":
                return self._error(415, "編集内容を確認してください。")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                return self._error(413, "選択した素材が多すぎます。")
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self._error(400, "編集内容を確認してください。")
            route = urlsplit(self.path).path
            if route == "/api/select":
                if not state.render_lock.acquire(blocking=False):
                    return self._error(409, "書き出し完了後に素材を選び直してください。")
                try:
                    try:
                        sources = state.registry.match(body.get("files") if isinstance(body, dict) else None)
                        plan = baseline(sources)
                    except ValueError as exc:
                        return self._error(400, str(exc))
                    except (OSError, KeyError):
                        return self._error(400, "素材を読み取れません。フォルダーを確認してください。")
                    with state.lock:
                        state.sources = sources
                        state.plan = plan
                        state.artifact = None
                        state.render_state = {"status": "idle"}
                        result = state.snapshot()
                    return self._json(200, result)
                finally:
                    state.render_lock.release()
            if route == "/api/edit":
                with state.lock:
                    if state.plan is None:
                        return self._error(409, "先に素材を選んでください。")
                    if not isinstance(body, dict) or type(body.get("revision")) is not int or body["revision"] != state.plan.revision:
                        return self._error(409, "編集内容を更新してから再試行してください。")
                    try:
                        plan = edit(state.plan, body.get("change"), state.sources)
                    except ValueError as exc:
                        return self._error(400, str(exc))
                    state.plan = plan
                    state.artifact = None
                    state.render_state = {"status": "idle"}
                    return self._json(200, state.snapshot())
            if route == "/api/render":
                if not state.render_lock.acquire(blocking=False):
                    return self._error(409, "書き出し中です。完了をお待ちください。")
                try:
                    with state.lock:
                        if state.plan is None or not isinstance(body, dict) or type(body.get("revision")) is not int or body["revision"] != state.plan.revision:
                            return self._error(409, "最新の編集内容で再試行してください。")
                        plan = state.plan
                        state.render_state = {"status": "running", "revision": plan.revision}
                    try:
                        artifact, path = render(plan, state.registry, state.work, state.ffmpeg)
                    except (ValueError, OSError) as exc:
                        with state.lock:
                            if state.plan.revision == plan.revision:
                                state.render_state = {"status": "failed", "message": str(exc)}
                            return self._error(422, str(exc))
                    with state.lock:
                        if state.plan.revision != plan.revision:
                            state.render_state = {"status": "idle"}
                            return self._error(409, "編集中にプレビューが古くなりました。")
                        state.artifact = (artifact, path, plan.revision)
                        state.render_state = {"status": "ready", "artifact_id": artifact, "revision": plan.revision, "frame_count": plan.frame_count}
                        return self._json(200, state.snapshot())
                finally:
                    state.render_lock.release()
            return self._error(404, "操作が見つかりません。")

        def log_message(self, format, *args):
            pass

    return Handler


def serve(root, work, ffmpeg, port=8765):
    state = State(root, work, ffmpeg)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(state))
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-root", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.media_root, args.work_dir, args.ffmpeg, args.port)
