#!/usr/bin/env python3
"""Local launcher for the combined Mudah + iBilik room alert service.

This is the small, neutral entry point for running the service from a checkout
without Docker. It:

1. loads the repository-root ``.env`` with a stdlib parser (no new dependency),
2. runs the existing alert CLI, defaulting ``--config`` to ``config.json`` and
   falling back to the tracked ``config.example.json`` when it is absent,
3. never prints secret values.

Precedence is explicit: a variable already set in the real environment always
wins, and ``.env`` only fills the ones that are unset. Use it to supply
``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID`` and ``MUDAH_AUTOMATION_ALLOWED``.

Examples (run from the repository root)::

    py run_alerts.py                 # poll hourly forever
    py run_alerts.py --once          # one cycle, then exit
    py run_alerts.py --dry-run       # report matches, send nothing

With ``MUDAH_AUTOMATION_ALLOWED`` unset or ``0``, the Mudah side is skipped with
a warning and any configured iBilik searches keep polling. The standalone Mudah
finder (``mudah-finder/mudah.py``) keeps its own, unchanged hard gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SERVICE_DIR = _ROOT / "mudah-alerts"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import mudah_alerts  # type: ignore[import-not-found]  # noqa: E402  (path set above)


def default_config_path() -> Path:
    """Prefer an edited ``config.json``; fall back to the tracked example."""

    local = _ROOT / "config.json"
    if local.is_file():
        return local
    return _ROOT / "config.example.json"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    loaded = mudah_alerts.load_dotenv(_ROOT / ".env")
    if loaded:
        # Report the count only; key values (which may be secrets) are never read
        # back or logged here.
        print(
            f"run_alerts: loaded {len(loaded)} key(s) from .env "
            "(real environment wins)",
            file=sys.stderr,
        )

    if "--config" not in args:
        args = ["--config", str(default_config_path()), *args]

    return mudah_alerts.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
