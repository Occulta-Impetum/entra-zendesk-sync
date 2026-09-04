"""Logging helpers for console and scheduled-task execution."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = REPO_ROOT / "logs"


class TeeStream:
    """Mirror text output to the original console stream and a log file."""

    def __init__(self, console: TextIO, log_file: TextIO) -> None:
        self.console = console
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.console.write(text)
        self.log_file.write(text)
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.console.isatty()

    @property
    def encoding(self) -> str | None:
        return getattr(self.console, "encoding", None)


class ConsoleLogTee:
    """Context manager that mirrors stdout/stderr to one timestamped log file."""

    def __init__(self, *, prefix: str = "sync", log_dir: str | Path | None = None) -> None:
        self.log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = self.log_dir / f"{prefix}_{timestamp}.log"
        self._handle: TextIO | None = None
        self._stdout: TextIO | None = None
        self._stderr: TextIO | None = None

    def __enter__(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", buffering=1)
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = TeeStream(self._stdout, self._handle)  # type: ignore[assignment]
        sys.stderr = TeeStream(self._stderr, self._handle)  # type: ignore[assignment]
        return self.path

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        if self._stdout is not None:
            sys.stdout = self._stdout
        if self._stderr is not None:
            sys.stderr = self._stderr
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
