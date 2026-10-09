"""Observability + sponsor-usage counters.

- W&B Weave tracing for every LLM-backed step (judge, voice, interview, ticket).
- Thread-safe counters for what the app pulls from each part of the stack
  (VAST S3/DB, NVIDIA Cosmos/YOLO via VSS, W&B inference), surfaced at /api/stack.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections import Counter
from typing import Any, Callable

_lock = threading.Lock()
_counts: Counter = Counter()
_started = time.time()

WEAVE_ENABLED = False
WEAVE_URL: str | None = None
_weave = None


def bump(key: str, n: int = 1) -> None:
    with _lock:
        _counts[key] += n


def snapshot() -> dict[str, Any]:
    with _lock:
        c = dict(_counts)
    return {"counters": c, "uptime_sec": round(time.time() - _started), "weave": {"enabled": WEAVE_ENABLED, "url": WEAVE_URL}}


def init() -> bool:
    """Start Weave tracing if W&B creds are present. Never raises."""
    global WEAVE_ENABLED, WEAVE_URL, _weave
    if os.environ.get("WEAVE_DISABLED") == "1":
        return False
    team, project = os.environ.get("WANDB_TEAM"), os.environ.get("WANDB_PROJECT")
    if not (team and project and os.environ.get("WANDB_API_KEY")):
        return False
    try:
        logging.disable(logging.WARNING)
        import weave  # type: ignore

        weave.init(f"{team}/{project}")
        _weave = weave
        WEAVE_ENABLED = True
        WEAVE_URL = f"https://wandb.ai/{team}/{project}/weave"
        print(f"[weave] tracing -> {WEAVE_URL}", flush=True)
    except Exception as e:  # weave missing or offline: run untraced
        print(f"[weave] disabled: {e}", flush=True)
        WEAVE_ENABLED = False
    finally:
        logging.disable(logging.NOTSET)
    return WEAVE_ENABLED


def op(name: str) -> Callable:
    """Decorator: a Weave op when tracing is on, otherwise a plain call.

    Binds lazily so modules can be imported before init() has run.
    """

    def deco(fn: Callable) -> Callable:
        traced: dict[str, Callable] = {}

        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> Any:
            if _weave is not None:
                if "fn" not in traced:
                    try:
                        traced["fn"] = _weave.op(name=name)(fn)
                    except Exception:
                        traced["fn"] = fn
                return traced["fn"](*a, **k)
            return fn(*a, **k)

        return wrapper

    return deco
