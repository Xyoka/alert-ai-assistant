from __future__ import annotations

from pathlib import Path
import os
import time


class LockError(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: str | Path, stale_seconds: int = 7200) -> None:
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_stale_lock()
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LockError(f"Another run is active: {self.path}") from exc
        os.write(self._fd, str(os.getpid()).encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _clear_stale_lock(self) -> None:
        if not self.path.exists():
            return
        age = time.time() - self.path.stat().st_mtime
        if age > self.stale_seconds:
            self.path.unlink()


