"""Consistent online SQLite backup. Restore only into a new database path."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, default=root/'data'/'delivery.sqlite3')
parser.add_argument('--destination', type=Path)
args = parser.parse_args()
destination = args.destination or root/'backups'/('delivery-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
if not args.source.is_file() or destination.exists():
    raise SystemExit('Source must exist and destination must be a new file.')
destination.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(f'file:{args.source}?mode=ro', uri=True) as source, sqlite3.connect(destination) as target:
    source.backup(target)
    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('Backup integrity check failed')
print('Verified backup:',destination)
