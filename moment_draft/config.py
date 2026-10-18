import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    ffmpeg: str = "ffmpeg"
    origin: str = "http://127.0.0.1:8770"
    chunk_bytes: int = 8 * 1024**2
    asset_bytes: int = 8 * 1024**3
    project_bytes: int = 20 * 1024**3
    host_bytes: int = 100 * 1024**3
    free_floor: int = 10 * 1024**3
    max_assets: int = 100
    owner_projects: int = 10
    host_projects: int = 100
    queue_limit: int = 10
    upload_slots: int = 2
    retention_seconds: int = 7 * 86400
    render_seconds: int = 900
    probe_seconds: int = 30
    max_attempts: int = 3

    def validate(self):
        parsed = urlsplit(self.origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("この版はループバックのHTTP起点だけで起動できます。")
        if not 0 < self.chunk_bytes <= 8 * 1024**2:
            raise ValueError("チャンク上限は8MiB以内です。")
        if self.retention_seconds <= 0:
            raise ValueError("保存期間は1日以上にしてください。")
        return self

    def bind(self, host, port, container_local=False):
        self.validate()
        if host not in {"127.0.0.1", "localhost", "::1"} and not (host == "0.0.0.0" and container_local):
            raise ValueError("公開起動にはまだ対応していません。")
        if port != (urlsplit(self.origin).port or 80):
            raise ValueError("MOMENT_PORT と MOMENT_ORIGIN のポートを一致させてください。")

    @classmethod
    def environment(cls):
        return cls(Path(os.environ.get("MOMENT_DATA_DIR", ".local/service-data")).resolve(),
                   ffmpeg=os.environ.get("MOMENT_FFMPEG", "ffmpeg"),
                   origin=os.environ.get("MOMENT_ORIGIN", "http://127.0.0.1:8770"),
                   retention_seconds=int(os.environ.get("MOMENT_RETENTION_DAYS", "7")) * 86400)
