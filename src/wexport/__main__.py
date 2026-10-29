"""Console entry point that guarantees UTF-8 output on Windows.

The frozen launcher runs with a hidden `pythonw`-style console; when a console
is attached (``--console``), Chinese text must not raise UnicodeEncodeError.
"""
from __future__ import annotations

import sys


def _fix_streams() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _fix_streams()
    from wexport.app import main as app_main
    return app_main()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
