from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from agents import config
from agents.adapters.sqlite_store import SqliteStore
from agents.supervisor import build_default_supervisor


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )


def format_status(store: SqliteStore) -> str:
    depth = store.depth()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    lines = [
        "queue:  " + "  ".join(f"{k}={v}" for k, v in sorted(depth.items())),
        f"spend:  ${store.cost_since(cutoff):.2f} in the last hour "
        f"(ceiling ${config.HOURLY_BUDGET_USD:.2f})",
        "",
    ]
    runs = store.recent(15)
    if not runs:
        lines.append("No runs yet.")
        return "\n".join(lines)

    lines.append(f"{'STARTED':<21} {'WORKER':<22} {'STATUS':<15} {'COST':>7}  FILES")
    for r in runs:
        files = ", ".join(r.files_changed[:3]) or "-"
        lines.append(f"{r.started_at[:19]:<21} {r.worker:<22} {r.status:<15} "
                     f"{r.cost_usd:>7.2f}  {files}")
    return "\n".join(lines)


def format_events(store: SqliteStore, limit: int) -> str:
    with store._conn() as c:
        rows = c.execute(
            "SELECT created_at, type, source, depth, consumed_at, payload FROM events"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    out = [f"{'WHEN':<21} {'EVENT':<28} {'SOURCE':<16} {'D':<2} {'STATE':<9} PAYLOAD"]
    for r in reversed(rows):
        state = "consumed" if r["consumed_at"] else "pending"
        payload = json.dumps(json.loads(r["payload"]), sort_keys=True)[:70]
        out.append(f"{r['created_at'][:19]:<21} {r['type']:<28} {r['source']:<16} "
                   f"{r['depth']:<2} {state:<9} {payload}")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    # -v is declared on both the top-level parser and every subcommand, so
    # `agents -v up` and `agents up -v` both work. argparse otherwise binds a
    # top-level flag strictly before the subcommand, which is not how anyone types it.
    # The subcommand copy uses SUPPRESS so that omitting -v after the subcommand
    # leaves the attribute unset rather than overwriting a -v given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="agents", description="Autonomous agent layer")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="run the supervisor until stopped", parents=[common])
    sub.add_parser("status", help="queue depth, recent runs, spend", parents=[common])
    events = sub.add_parser("events", help="recent events", parents=[common])
    events.add_argument("--limit", type=int, default=30)
    events.add_argument("--follow", action="store_true")
    sub.add_parser("stop", help="ask a running supervisor to drain and exit", parents=[common])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "up":
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        supervisor = build_default_supervisor()
        try:
            asyncio.run(supervisor.run())
        except KeyboardInterrupt:
            supervisor.request_stop()
            print("\nstopping (in-flight work will finish)", file=sys.stderr)
        return 0

    if args.command == "stop":
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.STOP_FLAG.write_text("stop\n")
        print(f"stop flag written to {config.STOP_FLAG}")
        return 0

    store = SqliteStore(config.DB_PATH, ledger_path=config.LEDGER_PATH)
    if args.command == "status":
        print(format_status(store))
        return 0

    if args.command == "events":
        if not args.follow:
            print(format_events(store, args.limit))
            return 0
        import time
        seen = 0
        try:
            while True:
                text = format_events(store, args.limit)
                if len(text) != seen:
                    print("\033[2J\033[H" + text)
                    seen = len(text)
                time.sleep(2)
        except KeyboardInterrupt:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
