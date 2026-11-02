"""Operations CLI: `python -m salesdesk.admin backup | verify FILE | restore FILE --yes`.

Works on the database configured in `.env` (DB_PATH) and needs no bot tokens.
Stop the bot before `restore`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .db import Database, RestoreError, restore_database, verify_backup
from .domain import utcnow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="salesdesk-admin", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="write a verified snapshot now")
    backup.add_argument("--to", type=Path, help="target file (default: BACKUP_DIR/salesdesk-manual-<time>.db)")
    verify = sub.add_parser("verify", help="check that a snapshot is intact and usable")
    verify.add_argument("file", type=Path)
    restore = sub.add_parser("restore", help="replace the live database with a snapshot (stop the bot first)")
    restore.add_argument("file", type=Path)
    restore.add_argument("--yes", action="store_true", help="confirm that the current database will be replaced")
    args = parser.parse_args(argv)

    cfg = Settings.from_env(need_platform=False)
    try:
        if args.command == "backup":
            if not cfg.db_path.exists():
                raise RestoreError(f"database not found: {cfg.db_path}")
            stamp = utcnow().astimezone(cfg.timezone).strftime("%Y%m%d-%H%M%S")
            target = args.to or cfg.backup_dir / f"salesdesk-manual-{stamp}.db"
            Database(cfg.db_path).backup_to(target)
            print(f"Backup written and verified: {target}")
        elif args.command == "verify":
            print(f"OK: {args.file} is a valid SalesDesk snapshot (schema v{verify_backup(args.file)})")
        else:
            if not args.yes:
                print(f"This REPLACES {cfg.db_path} with {args.file}. The bot must be stopped. Re-run with --yes.", file=sys.stderr)
                return 2
            saved = restore_database(args.file, cfg.db_path)
            print(f"Restored {cfg.db_path} from {args.file}." + (f" Previous database kept as {saved}" if saved else ""))
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
