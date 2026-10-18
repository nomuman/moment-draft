import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, request, send_from_directory

from .projects import Problem


COOKIE = "moment_owner"


@dataclass
class Attempts:
    times: deque = field(default_factory=deque)
    failures: int = 0
    next_attempt: float = 0


class AdmissionLimits:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.clients = {}
        self.global_times = deque()

    def attempt(self, address, matched):
        with self.lock:
            now = self.clock()
            self.clients = {key: value for key, value in self.clients.items() if value.times and value.times[-1] > now - 300}
            while self.global_times and self.global_times[0] <= now - 60:
                self.global_times.popleft()
            entry = self.clients.get(address)
            if len(self.global_times) >= 100 or entry is None and len(self.clients) >= 1024:
                return 60
            if entry is None:
                entry = self.clients[address] = Attempts()
            while entry.times and entry.times[0] <= now - 60:
                entry.times.popleft()
            if entry.next_attempt > now:
                return max(1, math.ceil(entry.next_attempt - now))
            if len(entry.times) >= 5:
                return max(1, math.ceil(entry.times[0] + 60 - now))
            entry.times.append(now)
            self.global_times.append(now)
            if matched:
                entry.failures = 0
                entry.next_attempt = 0
            else:
                entry.failures = min(entry.failures + 1, 6)
                entry.next_attempt = now + min(60, 2 ** entry.failures)
            return 0


def register_admission(app, projects):
    config = projects.config
    if not config.public_trial:
        return
    app.extensions["moment_admission"] = True
    limits = AdmissionLimits()
    static = Path(__file__).parent / "static"
    host = urlsplit(config.origin).netloc

    @app.before_request
    def admit():
        if (request.host != host or not request.is_secure
                or request.headers.get("Origin", config.origin) != config.origin
                or request.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"}):
            raise Problem("この起点からはアクセスできません。", 403)
        if request.path == "/api/admission":
            if request.method != "POST":
                raise Problem("この操作には POST を使用してください。", 405)
            if request.headers.get("Origin") != config.origin:
                raise Problem("画面を開き直して、もう一度お試しください。", 403)
            if request.content_length is not None and request.content_length > 1024:
                raise Problem("アクセスコードの送信内容が大きすぎます。", 413)
            request.max_content_length = 1025
            if len(request.get_data()) > 1024:
                raise Problem("アクセスコードの送信内容が大きすぎます。", 413)
            data = request.get_json()
            if not isinstance(data, dict) or not isinstance(data.get("code"), str):
                raise Problem("アクセスコードを入力してください。")
            matched = secrets.compare_digest(data["code"].encode(), config.access_code.encode())
            wait = limits.attempt(request.remote_addr or "unknown", matched)
            if wait:
                response = jsonify(error="試行回数の上限です。少し待ってから再試行してください。")
                response.status_code = 429
                response.headers["Retry-After"] = str(wait)
                return response
            if not matched:
                raise Problem("アクセスコードを確認して、もう一度入力してください。", 401)
            response = jsonify(admitted=True)
            if projects.session(request.cookies.get(COOKIE)) is None:
                token, _ = projects.new_session()
                response.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True, secure=True, samesite="Strict")
            return response
        if request.path in {"/static/admission.js", "/static/admission.css"} and request.method in {"GET", "HEAD"}:
            return send_from_directory(static, request.path.rsplit("/", 1)[1])
        if request.path == "/healthz":
            return
        if projects.session(request.cookies.get(COOKIE)) is not None:
            return
        if request.path == "/" and request.method in {"GET", "HEAD"}:
            return send_from_directory(static, "admission.html")
        raise Problem("アクセスコードを入力して試用を始めてください。", 401)
