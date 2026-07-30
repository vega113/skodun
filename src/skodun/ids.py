"""Review identifiers: one definition, importable from anywhere.

This module exists for a dependency reason, not a tidiness one. THREE writers
now mint a review id -- the foreground pipeline, the store's `reserve_prepush`
transaction, and the dispatcher's durable pre-record failure records -- and the
store must not import the pipeline (the pipeline imports the store). A private
`pipeline._new_id` therefore could not be shared, and a second copy of it in
`store.py` would be a second copy of the one property that makes an id safe:
uniqueness inside a single process-second.

Nothing here reads config, a clock the caller can control, or the store. It is
deliberately the smallest module in the package.
"""

from __future__ import annotations

import os
import time
import uuid


def new_review_id(prefix: str = "sk_") -> str:
    """`<prefix><utcstamp>_<pid>_<uuid8>`.

    The uuid component is mandatory. Second-resolution time plus pid collides
    for two ids minted in the same process-second -- which a multi-ref push and
    the foreground review loop both do routinely -- and every writer upserts by
    id, so the second one would silently overwrite the first's row (a live
    reservation, in the dispatcher's case).
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}{stamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
