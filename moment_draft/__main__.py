import argparse
import json
import os
import signal

from waitress import create_server

from .config import Config
from .projects import Projects
from .web import create_app
from .worker import Worker


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
    worker = Worker(projects)
    server = create_server(create_app(projects), host=host, port=port, threads=6, max_request_body_size=config.chunk_bytes, max_request_header_size=16384, connection_limit=32, channel_timeout=60, clear_untrusted_proxy_headers=True)

    def stop(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    try:
        print(f"moment draft {config.origin}", flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        worker.stop()
        projects.close()


if __name__ == "__main__":
    main()
