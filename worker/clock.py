"""The one shared ``Clock`` type/default used across ``worker/``.

Extracted to its own module (rather than living in ``worker/execution.py``
as it did before this phase) specifically to avoid a circular import: both
``worker/execution.py`` (which wires up heartbeating around execution) and
``worker/heartbeat.py`` (which the former imports) need it, and neither
should have to import the other just for this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
