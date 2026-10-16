"""Start the app: database, background jobs, local web server, browser."""
from __future__ import annotations

import argparse
import logging
import webbrowser
from pathlib import Path

from app import db
from app.config import PROJECT_ROOT, load_config
from app.pipeline import make_ctx
from app.scheduler import LiveRunner, Runner
from app.server import make_server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="etf-return-leaders")
    ap.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.toml")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-jobs", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=cfg.data_dir / "app.log", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db_path = args.db or cfg.data_dir / "tracker.db"
    ctx = make_ctx(cfg, db.connect(db_path))
    if not args.no_jobs:
        Runner(ctx).start()
        LiveRunner(ctx.fork()).start()          # own thread and connection: live prices never wait for long jobs
    server = make_server(lambda: db.connect_read_only(db_path), args.port if args.port is not None else cfg.port, progress=ctx.progress,
                         senate_enabled=cfg.senate_enabled)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"ETF Return Leaders running at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
