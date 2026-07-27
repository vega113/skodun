import os
from pathlib import Path


def oracle_dir() -> Path | None:
    """Path to the porting-oracle checkout from $SKODUN_ORACLE_DIR, or None."""
    raw = os.environ.get("SKODUN_ORACLE_DIR")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None
