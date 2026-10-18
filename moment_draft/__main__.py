import argparse
import json
import os
import signal

from waitress import create_server

from .config import Config
from .projects import Projects
from .retention import Retention
from .web import create_app
from .worker import Worker


def stop_service(server, retention, worker, projects, timeout):
    server.close()
    retention.stop()
    worker.stop()
    server.task_dispatcher.shutdown(timeout=timeout)
    if server.task_dispatcher.threads:
        raise RuntimeError("処理中の要求が解放されませんでした。保存先のロックはプロセス終了まで保持します。")
    projects.close()


def main():
    parser = argparse.ArgumentParser(description="moment draft local service")
    parser.add_argument("command", choices=("serve", "cleanup"), nargs="?", default="serve")
    parser.add_argument("--apply", action="store_true", help="期限切れのサービス内コピーを削除する")
    options = parser.parse_args()
    config = Config.environment().validate()
    host = os.environ.get("MOMENT_HOST", "127.0.0.1")
    port = int(os.environ.get("MOMENT_PORT", "8770"))
    config.bind(host, port, os.environ.get("MOMENT_CONTAINER_LOCAL") == "1")
    projects = Projects(config)
    if options.command == "cleanup":
        try:
            print(json.dumps({"dry_run": not options.apply, "expired_projects": projects.cleanup(dry_run=not options.apply)}))
        finally:
            projects.close()
        return
    app = create_app(projects)
    if config.public_trial and not app.extensions.get("moment_admission"):
        projects.close()
        raise RuntimeError("公開試用の入場制御が組み込まれていません。")
    worker = Worker(projects)
    proxy = {}
    if config.public_trial:
        proxy = {"trusted_proxy": config.trusted_proxy, "trusted_proxy_count": 1,
                 "trusted_proxy_headers": {"x-forwarded-proto", "x-forwarded-for"}}
    server = create_server(app, host=host, port=port, threads=6, max_request_body_size=config.chunk_bytes, max_request_header_size=16384, connection_limit=32, channel_timeout=60, clear_untrusted_proxy_headers=True, **proxy)

    def stop(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    retention = Retention(projects)
    worker.start()
    retention.start()
    try:
        print(f"moment draft {config.origin}", flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_service(server, retention, worker, projects, config.probe_seconds + 60)


if __name__ == "__main__":
    main()
