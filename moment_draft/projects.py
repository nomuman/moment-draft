import fcntl
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict

from . import timeline


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Projects:
    def __init__(self, config, clock=time.time):
        self.config = config.validate()
        self.clock = clock
        self.root = config.data_dir.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.volume_lock = (self.root / "service.lock").open("a+b")
        try:
            fcntl.flock(self.volume_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.volume_lock.close()
            raise RuntimeError("この保存先では既にサービスが動作しています。") from None
        self.lock = threading.RLock()
        self.uploads = {}
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sessions (owner TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(owner), created REAL NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, metadata TEXT, identity TEXT, digest TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS chunks (asset TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE, offset INTEGER NOT NULL, length INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(asset, offset));
            CREATE TABLE IF NOT EXISTS revisions (project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, plan TEXT NOT NULL, music TEXT NOT NULL, PRIMARY KEY(project, sequence));
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, revision INTEGER NOT NULL, request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, token TEXT, cancel INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL, UNIQUE(project, request_key));
            CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner);
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
        """)
        self.recover()

    def close(self):
        self.db.close()
        fcntl.flock(self.volume_lock, fcntl.LOCK_UN)
        self.volume_lock.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def directory(self, project):
        return self.root / "projects" / project

    def asset_path(self, project, asset):
        return self.directory(project) / "assets" / asset

    def artifact_path(self, project, job):
        return self.directory(project) / "outputs" / (job + ".mp4")

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='failed', error='処理が中断されました。再試行できます。', token=NULL WHERE state='running'")
            db.execute("UPDATE assets SET state='receiving', error=NULL WHERE state='verifying'")
            for row in db.execute("SELECT * FROM assets WHERE state='receiving'"):
                path = self.asset_path(row["project"], row["id"])
                if not path.exists() and row["offset"]:
                    db.execute("UPDATE assets SET state='failed', error='保存した素材が見つかりません。' WHERE id=?", (row["id"],))
                elif path.exists():
                    if path.stat().st_size < row["offset"]:
                        db.execute("UPDATE assets SET state='failed', error='素材の保存が中断しました。選び直してください。' WHERE id=?", (row["id"],))
                    else:
                        with path.open("r+b") as handle:
                            handle.truncate(row["offset"])
                            handle.flush()
                            os.fsync(handle.fileno())
        for row in self.db.execute("SELECT id FROM projects"):
            project = self.directory(row[0])
            attempts = project / "attempts"
            if not project.is_symlink() and attempts.is_dir() and not attempts.is_symlink():
                shutil.rmtree(attempts)

    def session(self, token):
        if not isinstance(token, str) or len(token) != 43:
            return None
        owner = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE owner=?", (owner,)).fetchone()
            return dict(row) if row else None

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        owner = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (owner, csrf, self.clock()))
        return token, {"owner": owner, "csrf": csrf}

    def owned(self, db, owner, project):
        row = db.execute("SELECT * FROM projects WHERE id=? AND owner=? AND expires>?", (project, owner, self.clock())).fetchone()
        if row is None:
            raise Problem("作品が見つからないか、保存期限が切れています。", 404)
        return row

    def asset(self, db, owner, project, asset):
        self.owned(db, owner, project)
        row = db.execute("SELECT * FROM assets WHERE id=? AND project=?", (asset, project)).fetchone()
        if row is None:
            raise Problem("素材が見つかりません。", 404)
        return row

    def space(self, required=0):
        if shutil.disk_usage(self.root).free - required < self.config.free_floor:
            raise Problem("保存先の空き容量が足りません。管理者が容量を確保してから再試行してください。", 507)

    def create(self, owner):
        with self.transaction() as db:
            active = db.execute("SELECT COUNT(*) FROM projects WHERE owner=? AND expires>?", (owner, self.clock())).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            if active >= self.config.owner_projects or total >= self.config.host_projects:
                raise Problem("保存できる作品数の上限です。保存期限後の整理をお待ちください。", 429)
            self.space()
            project = secrets.token_hex(16)
            now = self.clock()
            db.execute("INSERT INTO projects(id,owner,created,expires) VALUES(?,?,?,?)", (project, owner, now, now + self.config.retention_seconds))
            (self.directory(project) / "assets").mkdir(parents=True, mode=0o700)
            (self.directory(project) / "outputs").mkdir(mode=0o700)
            sync_directory(self.directory(project))
            sync_directory(self.directory(project).parent)
        return self.view(owner, project)

    def list(self, owner):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,created,expires,revision FROM projects WHERE owner=? AND expires>? ORDER BY created DESC", (owner, self.clock()))]

    def register(self, owner, project, name, size):
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name:
            raise Problem("ファイル名を確認してください。")
        if type(size) is not int or not 0 < size <= self.config.asset_bytes:
            raise Problem("素材は1点8GiB以内で選んでください。")
        with self.transaction() as db:
            self.owned(db, owner, project)
            count, reserved = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM assets WHERE project=?", (project,)).fetchone()
            host_reserved = db.execute("SELECT COALESCE(SUM(size),0) FROM assets").fetchone()[0]
            unfilled = db.execute("SELECT COALESCE(SUM(size-offset),0) FROM assets WHERE state IN ('receiving','verifying')").fetchone()[0]
            if count >= self.config.max_assets or reserved + size > self.config.project_bytes or host_reserved + size > self.config.host_bytes:
                raise Problem("素材数または保存容量の上限です。別の作品に分けるか素材を減らしてください。", 413)
            self.space(unfilled + size)
            asset = secrets.token_hex(16)
            db.execute("INSERT INTO assets(id,project,name,size,state) VALUES(?,?,?,?,'receiving')", (asset, project, name, size))
        return self.asset_status(owner, project, asset)

    def asset_status(self, owner, project, asset):
        with self.lock:
            row = self.asset(self.db, owner, project, asset)
            result = self.public_asset(row)
            result["receipts"] = [dict(r) for r in self.db.execute("SELECT offset,length,digest FROM chunks WHERE asset=? ORDER BY offset", (asset,))]
            return result

    @contextmanager
    def upload_slot(self, owner):
        with self.lock:
            if self.uploads.get(owner, 0) >= self.config.upload_slots:
                raise Problem("同時に送れる素材は2点までです。少し待って再試行してください。", 429)
            self.uploads[owner] = self.uploads.get(owner, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.uploads[owner] -= 1

    def append(self, owner, project, asset, offset, data, expected_digest=None):
        if type(offset) is not int or offset < 0 or not 0 < len(data) <= self.config.chunk_bytes:
            raise Problem("送信位置またはチャンクサイズを確認してください。", 413)
        digest = hashlib.sha256(data).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            raise Problem("送信内容の照合に失敗しました。再送してください。", 409)
        with self.transaction() as db:
            row = self.asset(db, owner, project, asset)
            receipt = db.execute("SELECT * FROM chunks WHERE asset=? AND offset=?", (asset, offset)).fetchone()
            if receipt:
                if receipt["digest"] != digest or receipt["length"] != len(data):
                    raise Problem("同じ送信位置に異なる内容は送れません。", 409)
                return {"offset": row["offset"], "digest": digest}
            if row["state"] != "receiving" or offset != row["offset"] or offset + len(data) > row["size"]:
                raise Problem("送信位置が変わりました。素材の状態を読み直してください。", 409)
            self.space(len(data))
            path = self.asset_path(project, asset)
            with path.open("r+b" if path.exists() else "w+b") as handle:
                handle.truncate(offset)
                handle.seek(offset)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(path.parent)
            db.execute("INSERT INTO chunks VALUES(?,?,?,?)", (asset, offset, len(data), digest))
            db.execute("UPDATE assets SET offset=?,error=NULL WHERE id=?", (offset + len(data), asset))
        return {"offset": offset + len(data), "digest": digest}

    def finish(self, owner, project, asset):
        from .media import inspect
        with self.transaction() as db:
            row = self.asset(db, owner, project, asset)
            if row["state"] == "ready":
                return self.public_asset(row)
            if row["state"] != "receiving" or row["offset"] != row["size"]:
                raise Problem("素材の送信がまだ完了していません。", 409)
            db.execute("UPDATE assets SET state='verifying' WHERE id=?", (asset,))
            path = self.asset_path(project, asset)
        try:
            original = identity(path)
            source = inspect(path, asset, row["name"], self.config.probe_seconds)
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if identity(path) != original:
                raise ValueError("素材が検証中に変わりました。")
            with self.transaction() as db:
                self.owned(db, owner, project)
                db.execute("UPDATE assets SET state='ready',metadata=?,identity=?,digest=?,error=NULL WHERE id=? AND state='verifying'", (encoded(source), encoded(original), digest, asset))
        except Exception:
            with self.transaction() as db:
                db.execute("UPDATE assets SET state='failed',error=? WHERE id=?", ("素材を確認できません。JPEG・PNG・WebPまたはSDRのMP4・MOVを選んでください。", asset))
        return self.asset_status(owner, project, asset)

    @staticmethod
    def public_asset(row):
        result = {key: row[key] for key in ("id", "name", "size", "offset", "state", "error")}
        result["source"] = json.loads(row["metadata"]) if row["metadata"] else None
        return result

    def view(self, owner, project):
        with self.lock:
            row = self.owned(self.db, owner, project)
            result = {key: row[key] for key in ("id", "created", "expires", "revision")}
            result["assets"] = [self.public_asset(r) for r in self.db.execute("SELECT * FROM assets WHERE project=? ORDER BY rowid", (project,))]
            revision = self.db.execute("SELECT plan,music FROM revisions WHERE project=? AND sequence=?", (project, row["revision"])).fetchone()
            result["plan"] = json.loads(revision["plan"]) if revision else None
            result["music"] = json.loads(revision["music"]) if revision else {"id": "off", "level": 0}
            result["jobs"] = [self.public_job(r) for r in self.db.execute("SELECT * FROM jobs WHERE project=? ORDER BY created DESC", (project,))]
            completed = next((job for job in result["jobs"] if job["state"] == "succeeded" and job["revision"] == row["revision"]), None)
            result["artifact"] = f"/api/projects/{project}/jobs/{completed['id']}/artifact" if completed else None
            return result

    def sources(self, db, project):
        return [timeline.Source(**json.loads(r[0])) for r in db.execute("SELECT metadata FROM assets WHERE project=? AND state='ready' ORDER BY rowid", (project,))]

    def revise(self, owner, project, expected, change=None):
        if type(expected) is not int:
            raise Problem("編集の版を指定してください。")
        with self.transaction() as db:
            row = self.owned(db, owner, project)
            if row["revision"] != expected:
                raise Problem("別の画面で編集されました。最新の内容を読み直してください。", 409)
            sources = self.sources(db, project)
            try:
                if change is None:
                    plan = timeline.baseline(sources)
                else:
                    revision = db.execute("SELECT plan FROM revisions WHERE project=? AND sequence=?", (project, expected)).fetchone()
                    if revision is None:
                        raise Problem("先に初案を作ってください。", 409)
                    plan = timeline.edit(timeline.from_dict(json.loads(revision[0])), change, sources)
            except ValueError as exc:
                raise Problem(str(exc)) from exc
            data = asdict(plan)
            data["revision"] = expected + 1
            if plan.frame_count > 3600:
                raise Problem("書き出しは120秒までです。")
            db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project, expected + 1, encoded(data), encoded({"id": "off", "level": 0})))
            db.execute("UPDATE projects SET revision=? WHERE id=?", (expected + 1, project))
        return self.view(owner, project)

    def enqueue(self, owner, project, revision, key):
        if type(revision) is not int or not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise Problem("書き出しの版とリクエストIDを確認してください。")
        payload_hash = hashlib.sha256(encoded({"revision": revision}).encode()).hexdigest()
        with self.transaction() as db:
            row = self.owned(db, owner, project)
            old = db.execute("SELECT * FROM jobs WHERE project=? AND request_key=?", (project, key)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise Problem("同じリクエストIDは同じ内容で再送してください。", 409)
                return self.public_job(old)
            if row["revision"] != revision:
                raise Problem("最新の編集内容から書き出してください。", 409)
            rev = db.execute("SELECT * FROM revisions WHERE project=? AND sequence=?", (project, revision)).fetchone()
            if rev is None:
                raise Problem("初案を作ってから書き出してください。", 409)
            plan = timeline.from_dict(json.loads(rev["plan"]))
            if not 0 < plan.frame_count <= 3600:
                raise Problem("1場面以上、120秒以内で書き出してください。", 422)
            self.queue_space(db)
            existing = db.execute("SELECT COUNT(*) FROM jobs WHERE project=?", (project,)).fetchone()[0]
            if existing >= 50:
                raise Problem("この作品の書き出し回数の上限です。", 429)
            asset_ids = {scene.source_id for scene in plan.scenes if scene.included}
            assets = [dict(r) for r in db.execute("SELECT * FROM assets WHERE project=? AND state='ready'", (project,)) if r["id"] in asset_ids]
            if len(assets) != len(asset_ids):
                raise Problem("使用中の素材を確認できません。", 409)
            for asset in assets:
                self.checked_source(project, asset)
            snapshot = {"plan": json.loads(rev["plan"]), "music": json.loads(rev["music"]), "assets": assets}
            job = secrets.token_hex(16)
            db.execute("INSERT INTO jobs(id,project,revision,request_key,payload_hash,snapshot,state,created) VALUES(?,?,?,?,?,?,'queued',?)", (job, project, revision, key, payload_hash, encoded(snapshot), self.clock()))
            return self.public_job(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())

    def queue_space(self, db):
        if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.config.queue_limit:
            raise Problem("書き出しが混み合っています。少し待って再試行してください。", 429)
        self.space(256 * 1024**2)

    def checked_source(self, project, asset):
        path = self.asset_path(project, asset["id"])
        if path.is_symlink() or not path.is_file() or identity(path) != json.loads(asset["identity"]):
            raise Problem("保存した素材が変わりました。素材を選び直してください。", 409)
        return path

    @staticmethod
    def public_job(row):
        return {key: row[key] for key in ("id", "revision", "state", "attempt", "progress", "error", "created")}

    def job(self, db, owner, project, job):
        self.owned(db, owner, project)
        row = db.execute("SELECT * FROM jobs WHERE id=? AND project=?", (job, project)).fetchone()
        if row is None:
            raise Problem("書き出しが見つかりません。", 404)
        return row

    def job_action(self, owner, project, job, action):
        with self.transaction() as db:
            row = self.job(db, owner, project, job)
            if action == "cancel":
                if row["state"] == "queued":
                    db.execute("UPDATE jobs SET state='cancelled',cancel=1 WHERE id=?", (job,))
                elif row["state"] == "running":
                    db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job,))
            elif action == "retry":
                if row["state"] not in {"failed", "cancelled"} or row["attempt"] >= self.config.max_attempts:
                    raise Problem("この書き出しは再試行できません。", 409)
                self.queue_space(db)
                db.execute("UPDATE jobs SET state='queued',cancel=0,error=NULL,progress=0,token=NULL WHERE id=?", (job,))
            else:
                raise Problem("操作を確認してください。")
            return self.public_job(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())

    def claim(self):
        with self.transaction() as db:
            now = self.clock()
            db.execute("UPDATE jobs SET state='cancelled',cancel=1 WHERE state='queued' AND project IN (SELECT id FROM projects WHERE expires<=?)", (now,))
            row = db.execute("SELECT j.* FROM jobs j JOIN projects p ON p.id=j.project WHERE j.state='queued' AND p.expires>? ORDER BY j.created LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            db.execute("UPDATE jobs SET state='running',attempt=attempt+1,token=?,progress=0,error=NULL WHERE id=?", (token, row["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def active(self, job, progress=None):
        with self.transaction() as db:
            row = db.execute("SELECT j.*,p.expires FROM jobs j JOIN projects p ON p.id=j.project WHERE j.id=?", (job["id"],)).fetchone()
            valid = row and row["state"] == "running" and row["token"] == job["token"] and not row["cancel"] and row["expires"] > self.clock()
            if valid and progress is not None:
                db.execute("UPDATE jobs SET progress=? WHERE id=?", (max(row["progress"], min(.98, progress)), job["id"]))
            return bool(valid)

    def complete(self, job, temporary):
        with self.transaction() as db:
            row = db.execute("SELECT j.*,p.expires FROM jobs j JOIN projects p ON p.id=j.project WHERE j.id=?", (job["id"],)).fetchone()
            if row is None or row["state"] != "running" or row["token"] != job["token"] or row["cancel"] or row["expires"] <= self.clock():
                return False
            for asset in json.loads(job["snapshot"])["assets"]:
                self.checked_source(job["project"], asset)
            target = self.artifact_path(job["project"], job["id"])
            os.replace(temporary, target)
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
            sync_directory(target.parent)
            db.execute("UPDATE jobs SET state='succeeded',progress=1,error=NULL WHERE id=?", (job["id"],))
            return True

    def fail(self, job, cancelled=False):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state=?,error=? WHERE id=? AND token=? AND state='running'", ("cancelled" if cancelled else "failed", None if cancelled else "書き出しが完了しませんでした。素材と空き容量を確認して再試行してください。", job["id"], job["token"]))

    def private_file(self, owner, project, asset=None, job=None):
        with self.lock:
            current = self.owned(self.db, owner, project)
            if asset:
                row = self.asset(self.db, owner, project, asset)
                if row["state"] != "ready":
                    raise Problem("素材はまだ準備中です。", 404)
                return self.checked_source(project, row), row["name"]
            row = self.job(self.db, owner, project, job)
            if row["state"] != "succeeded" or row["revision"] != current["revision"]:
                raise Problem("現在の編集内容の動画はまだありません。", 404)
            path = self.artifact_path(project, job)
            if path.is_symlink() or not path.is_file():
                raise Problem("完成した動画が見つかりません。", 404)
            return path, "moment-draft.mp4"

    def cleanup(self, dry_run=True):
        with self.transaction() as db:
            expired = [row[0] for row in db.execute("SELECT id FROM projects WHERE expires<=?", (self.clock(),))]
            if dry_run:
                return expired
            for project in expired:
                db.execute("UPDATE jobs SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE project=? AND state IN ('queued','running')", (project,))
                if db.execute("SELECT 1 FROM jobs WHERE project=? AND state='running'", (project,)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM assets WHERE project=? AND state='verifying'", (project,)).fetchone():
                    continue
                directory = self.directory(project)
                if directory.is_symlink():
                    raise Problem("保存フォルダーを安全に確認できません。", 409)
                if directory.exists():
                    shutil.rmtree(directory)
                db.execute("DELETE FROM projects WHERE id=?", (project,))
            return expired
