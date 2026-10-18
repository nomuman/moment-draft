from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


FREE_FLOOR = 10 * 1024**3
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _check_origin(value: str, mode: str, port: int) -> Check:
    try:
        origin = urlsplit(value)
        hostname = origin.hostname
        origin_port = origin.port
    except ValueError:
        return Check("origin", "fail", "MOMENT_ORIGIN is invalid")
    if not hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        return Check("origin", "fail", "MOMENT_ORIGIN must contain only a scheme and host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if mode == "local":
        if origin.scheme != "http" or hostname not in LOOPBACK or origin_port != port:
            return Check("origin", "fail", "Local origin must be loopback HTTP on MOMENT_PORT")
    elif origin.scheme != "https" or address or "." not in hostname:
        return Check("origin", "fail", "Public origin must be a domain with HTTPS")
    return Check("origin", "pass", "Origin shape matches the selected mode")


def _check_storage(path: Path) -> list[Check]:
    if path.is_symlink() or not path.is_dir():
        return [Check("storage", "fail", "MOMENT_DATA_DIR must be an existing private directory")]
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        privacy = Check("storage", "fail", "MOMENT_DATA_DIR must be owned by this user with mode 0700")
    else:
        privacy = Check("storage", "pass", "Data directory ownership and mode are private")
    free = shutil.disk_usage(path).free
    if free < FREE_FLOOR:
        capacity = Check("free_space", "fail", "Less than 10 GiB is free on the data filesystem")
    else:
        capacity = Check("free_space", "pass", "At least 10 GiB is free on the data filesystem")
    return [privacy, capacity]


def evaluate(env: dict[str, str], mode: str) -> list[Check]:
    checks: list[Check] = []
    try:
        port = int(env.get("MOMENT_PORT", "8770"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        port = 0
        checks.append(Check("port", "fail", "MOMENT_PORT must be between 1 and 65535"))
    else:
        checks.append(Check("port", "pass", "Port is valid"))

    checks.append(_check_origin(env.get("MOMENT_ORIGIN", "http://127.0.0.1:8770"), mode, port))
    host = env.get("MOMENT_HOST", "127.0.0.1")
    container_local = env.get("MOMENT_CONTAINER_LOCAL") == "1"
    if mode == "local":
        valid_bind = host in {"127.0.0.1", "::1"} or (host == "0.0.0.0" and container_local)
        detail = "Local bind is loopback or explicitly marked for loopback-published container use"
    else:
        valid_bind = host in {"0.0.0.0", "::"} and not container_local
        detail = "Public bind is configured without the local container exception"
    checks.append(Check("bind", "pass" if valid_bind else "fail", detail if valid_bind else "MOMENT_HOST conflicts with the selected mode"))

    try:
        retention = int(env.get("MOMENT_RETENTION_DAYS", "7"))
    except ValueError:
        retention = 0
    valid_retention = 1 <= retention <= 7
    retention_detail = "Retention is within the provisional 1 to 7 day window" if valid_retention else "MOMENT_RETENTION_DAYS must be between 1 and 7"
    checks.append(Check("retention", "pass" if valid_retention else "fail", retention_detail))

    executable = env.get("MOMENT_FFMPEG", "ffmpeg")
    resolved = shutil.which(executable)
    ffmpeg_detail = "Configured FFmpeg path is executable" if resolved else "MOMENT_FFMPEG is not executable"
    checks.append(Check("ffmpeg", "pass" if resolved else "fail", ffmpeg_detail))

    data_dir = Path(env.get("MOMENT_DATA_DIR", "./data"))
    checks.extend(_check_storage(data_dir))
    if mode == "public":
        admission = bool(env.get("MOMENT_ACCESS_CODE", "").strip())
        admission_detail = "Admission code is configured" if admission else "MOMENT_ACCESS_CODE is required"
        checks.append(Check("admission", "pass" if admission else "fail", admission_detail))
        checks.append(Check("live_ingress", "pending", "Verify TLS, trusted proxy, secure cookies, and external admission before public use"))
        checks.append(Check("live_product", "pending", "Verify real browser, native render, recovery, expiry, and access denial on the target host"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only service configuration preflight")
    parser.add_argument("--mode", choices=("local", "public"), default="local")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = evaluate(dict(os.environ), args.mode)
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            print(f"{check.status.upper():7} {check.name}: {check.detail}")
    if any(check.status == "fail" for check in checks):
        return 1
    if any(check.status == "pending" for check in checks):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
