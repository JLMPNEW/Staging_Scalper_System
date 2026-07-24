import sqlite3
from pathlib import Path


source_path = Path(r"C:\Users\josel\Documents\STAGING\DB\industrials.sqlite")
backup_path = Path(
    r"C:\tmp\industrials_before_machinery_20260722_refresh_20260723.sqlite"
)
if backup_path.exists():
    raise FileExistsError(backup_path)

source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(backup_path)
try:
    source.backup(destination, pages=8192, sleep=0.05)
    result = destination.execute("PRAGMA quick_check").fetchone()
    print(
        {
            "backup": str(backup_path),
            "bytes": backup_path.stat().st_size,
            "quick_check": result[0] if result else "",
        }
    )
finally:
    destination.close()
    source.close()
