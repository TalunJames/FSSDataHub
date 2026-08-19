"""In-memory tail of the app's own log, for the diagnostic download.

Docker keeps stdout, but the operator should not need SSH and `docker logs`
to hand over evidence when something misbehaves. A ring buffer of the last
few thousand log lines (including the crawl engine's warnings and
tracebacks, which log through the standard logging module) costs a few
hundred KB and makes /api/logs/export self-contained.
"""

import collections
import logging
import threading

BUFFER = collections.deque(maxlen=3000)
_lock = threading.Lock()


class _Ring(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        with _lock:
            BUFFER.append(line)


def install():
    """Attach the ring to the root logger, once."""
    root = logging.getLogger()
    if any(isinstance(h, _Ring) for h in root.handlers):
        return
    handler = _Ring()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    # The root default (WARNING) would drop the INFO lines that make a
    # diagnostic readable. Handlers keep their own levels, so stderr output
    # does not get chattier because of this.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def tail(n=3000):
    with _lock:
        return list(BUFFER)[-n:]
