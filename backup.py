"""Run from project directory. Creates a consistent SQLite backup, not a raw WAL copy."""
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from storage import Store
load_dotenv(Path(__file__).with_name('.env'))
dest = Path(os.getenv('BACKUP_DIR', './backups'))
dest.mkdir(parents=True, exist_ok=True)
path = dest / f'manual-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3'
Store(os.getenv('DB_PATH', './data/bot.sqlite3')).backup(str(path))
os.chmod(path, 0o600)
print(f'Backup created: {path}')
