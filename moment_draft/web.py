import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .projects import Problem


COOKIE = "moment_owner"


def create_app(projects):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = projects.config.chunk_bytes
    static = Path(__file__).parent / "static"
    origin = projects.config.origin
    host = urlsplit(origin).netloc

    @app.before_request
    def boundaries():
        if request.host != host or request.headers.get("Origin", origin) != origin or request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise Problem("この起点からはアクセスできません。", 403)
        if request.path == "/healthz" or request.path == "/" or request.path.startswith("/static/"):
            return
        g.session = projects.session(request.cookies.get(COOKIE))
        if request.path == "/api/session" and request.method == "GET":
            return
        if g.session is None:
            raise Problem("このブラウザで作品を開き直してください。", 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Origin") != origin or not secrets.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), g.session["csrf"].encode()):
                raise Problem("画面を更新して、もう一度お試しください。", 403)
        if request.is_json and request.content_length is not None and request.content_length > 65536:
            raise Problem("操作内容が大きすぎます。", 413)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.errorhandler(Problem)
    def problem(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error="送信内容またはアクセス先を確認してください。"), exc.code

    @app.errorhandler(Exception)
    def unexpected(exc):
        app.logger.error("request failed: %s", type(exc).__name__)
        return jsonify(error="処理を完了できませんでした。少し待って再試行してください。"), 500

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise Problem("操作内容を確認してください。")
        return data

    def owner():
        return g.session["owner"]

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        return send_from_directory(static, "index.html")

    @app.get("/static/<name>")
    def static_file(name):
        if name not in {"app.js", "style.css"}:
            raise Problem("見つかりません。", 404)
        return send_from_directory(static, name)

    @app.get("/api/session")
    def session():
        token = None
        if g.session is None:
            token, g.session = projects.new_session()
        response = jsonify(csrf=g.session["csrf"], projects=projects.list(owner()),
                           limits={"chunk_bytes": projects.config.chunk_bytes, "asset_bytes": projects.config.asset_bytes, "project_bytes": projects.config.project_bytes, "max_assets": projects.config.max_assets, "retention_seconds": projects.config.retention_seconds})
        if token:
            response.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True, samesite="Strict", secure=urlsplit(origin).scheme == "https")
        return response

    @app.get("/api/projects")
    def list_projects():
        return jsonify(projects=projects.list(owner()))

    @app.post("/api/projects")
    def new_project():
        return jsonify(projects.create(owner())), 201

    @app.get("/api/projects/<project>")
    def get_project(project):
        return jsonify(projects.view(owner(), project))

    @app.post("/api/projects/<project>/assets")
    def register(project):
        data = body()
        return jsonify(projects.register(owner(), project, data.get("name"), data.get("size"))), 201

    @app.get("/api/projects/<project>/assets/<asset>")
    def asset_status(project, asset):
        return jsonify(projects.asset_status(owner(), project, asset))

    @app.delete("/api/projects/<project>/assets/<asset>")
    def discard_asset(project, asset):
        return jsonify(projects.discard_asset(owner(), project, asset))

    @app.put("/api/projects/<project>/assets/<asset>/chunks")
    def append(project, asset):
        try:
            offset = int(request.headers.get("Upload-Offset", ""))
        except ValueError:
            raise Problem("送信位置を指定してください。") from None
        if request.mimetype != "application/octet-stream":
            raise Problem("素材はバイナリで送信してください。", 415)
        with projects.upload_slot(owner()):
            return jsonify(projects.append(owner(), project, asset, offset, request.get_data(), request.headers.get("X-Chunk-SHA256")))

    @app.post("/api/projects/<project>/assets/<asset>/finish")
    def finish(project, asset):
        with projects.upload_slot(owner()):
            return jsonify(projects.finish(owner(), project, asset))

    @app.post("/api/projects/<project>/draft")
    def draft(project):
        return jsonify(projects.revise(owner(), project, body().get("revision")))

    @app.post("/api/projects/<project>/edits")
    def edit(project):
        data = body()
        if not isinstance(data.get("change"), dict):
            raise Problem("編集内容を確認してください。")
        return jsonify(projects.revise(owner(), project, data.get("revision"), data["change"]))

    @app.post("/api/projects/<project>/jobs")
    def enqueue(project):
        data = body()
        return jsonify(projects.enqueue(owner(), project, data.get("revision"), data.get("request_key"))), 202

    @app.get("/api/projects/<project>/jobs/<job>")
    def job_status(project, job):
        with projects.lock:
            return jsonify(projects.public_job(projects.job(projects.db, owner(), project, job)))

    @app.post("/api/projects/<project>/jobs/<job>/<action>")
    def job_action(project, job, action):
        return jsonify(projects.job_action(owner(), project, job, action))

    @app.get("/api/music")
    def music():
        return jsonify(tracks=[])

    def private_response(project, asset=None, job=None):
        path, _ = projects.private_file(owner(), project, asset, job)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = request.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested) if len(requested) <= 128 else None
            if not match or not any(match.groups()):
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start > end or start >= size or not first and int(last) == 0:
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
        if job:
            mime = "video/mp4"
        else:
            with path.open("rb") as handle:
                header = handle.read(16)
            mime = "image/jpeg" if header.startswith(b"\xff\xd8") else "image/png" if header.startswith(b"\x89PNG") else "image/webp" if header[:4] == b"RIFF" else "video/mp4"
        response_headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if status == 206:
            response_headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if job and request.args.get("download") == "1":
            response_headers["Content-Disposition"] = 'attachment; filename="moment-draft.mp4"'
        owner_id = owner()

        def stream():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    with projects.lock:
                        try:
                            projects.owned(projects.db, owner_id, project)
                        except Problem:
                            return
                    data = handle.read(min(1024 * 1024, remaining))
                    if not data:
                        return
                    remaining -= len(data)
                    yield data

        return Response(stream() if request.method != "HEAD" else b"", status=status, mimetype=mime, headers=response_headers)

    @app.get("/api/projects/<project>/assets/<asset>/source")
    def source(project, asset):
        return private_response(project, asset=asset)

    @app.get("/api/projects/<project>/jobs/<job>/artifact")
    def artifact(project, job):
        return private_response(project, job=job)

    return app
