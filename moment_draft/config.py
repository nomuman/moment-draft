import ipaddress
import os
import re
from dataclasses import dataclass, field
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
    public_trial: bool = False
    access_code: str = field(default="", repr=False)
    trusted_proxy: str = ""

    def validate(self):
        parsed = urlsplit(self.origin)
        if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("MOMENT_ORIGIN は起点だけを指定してください。")
        if self.public_trial:
            hostname = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.port is not None
                    or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname)
                    or self.origin != "https://" + parsed.netloc or parsed.netloc != hostname):
                raise ValueError("公開試用には正規の HTTPS ドメイン起点が必要です。")
            if (not 32 <= len(self.access_code) <= 256 or len(set(self.access_code)) < 16
                    or not self.access_code.isascii() or any(c.isspace() for c in self.access_code)
                    or any(word in self.access_code.lower() for word in ("example", "changeme", "replace", "default"))):
                raise ValueError("公開試用にはランダム生成した32文字以上のアクセスコードが必要です。")
            try:
                proxy = ipaddress.ip_address(self.trusted_proxy)
            except ValueError:
                raise ValueError("MOMENT_TRUSTED_PROXY に単一の既知の IP を指定してください。") from None
            if proxy.is_unspecified or proxy.is_multicast or str(proxy) != self.trusted_proxy:
                raise ValueError("MOMENT_TRUSTED_PROXY に単一の既知の IP を指定してください。")
        elif parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ローカル版はループバックのHTTP起点だけで起動できます。")
        elif self.trusted_proxy:
            raise ValueError("プロキシの信頼設定は公開試用でのみ利用できます。")
        if not 0 < self.chunk_bytes <= 8 * 1024**2:
            raise ValueError("チャンク上限は8MiB以内です。")
        if not 0 < self.retention_seconds <= 7 * 86400:
            raise ValueError("保存期間は正の値で7日以内にしてください。")
        return self

    def bind(self, host, port, container_local=False):
        self.validate()
        if not 1 <= port <= 65535:
            raise ValueError("MOMENT_PORT は1から65535で指定してください。")
        if self.public_trial:
            if container_local:
                raise ValueError("公開試用とローカルコンテナ設定は併用できません。")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise ValueError("公開試用の待受先は明示的な IP にしてください。") from None
            return
        if host not in {"127.0.0.1", "localhost", "::1"} and not (host == "0.0.0.0" and container_local):
            raise ValueError("公開起動には明示的な公開試用設定が必要です。")
        if port != (urlsplit(self.origin).port or 80):
            raise ValueError("MOMENT_PORT と MOMENT_ORIGIN のポートを一致させてください。")

    @classmethod
    def environment(cls, environ=None):
        environ = os.environ if environ is None else environ
        for flag in ("MOMENT_PUBLIC_TRIAL", "MOMENT_CONTAINER_LOCAL"):
            if environ.get(flag, "0") not in {"0", "1"}:
                raise ValueError(f"{flag} は0か1で指定してください。")
        days = int(environ.get("MOMENT_RETENTION_DAYS", "7"))
        if not 1 <= days <= 7:
            raise ValueError("MOMENT_RETENTION_DAYS は1から7で指定してください。")
        return cls(Path(environ.get("MOMENT_DATA_DIR", ".local/service-data")).resolve(),
                   ffmpeg=environ.get("MOMENT_FFMPEG", "ffmpeg"),
                   origin=environ.get("MOMENT_ORIGIN", "http://127.0.0.1:8770"),
                   retention_seconds=days * 86400,
                   public_trial=environ.get("MOMENT_PUBLIC_TRIAL") == "1",
                   access_code=environ.get("MOMENT_ACCESS_CODE", ""),
                   trusted_proxy=environ.get("MOMENT_TRUSTED_PROXY", ""))
