"""Private temporary files with bounded cleanup and active-job leases."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from uuid import uuid4


class SessionWorkspace:
    def __init__(self, root=None, *, clock=time.time, idle_seconds=6 * 3600):
        self._root = Path(root).resolve() if root else None
        self.clock = clock
        self.idle_seconds = idle_seconds
        self._active = {}
        self._lock = threading.RLock()
        self._last_sweep = 0

    @property
    def root(self):
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="student_ai_assistant_")).resolve()
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._root

    def _directory(self, workspace_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(workspace_id)):
            raise ValueError("Invalid session workspace")
        directory = self.root / workspace_id
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise ValueError("Invalid session workspace path")
        return directory

    def ensure(self, state):
        with self._lock:
            workspace_id = state.setdefault("_workspace_id", uuid4().hex)
            directory = self._directory(workspace_id)
            directory.mkdir(mode=0o700, exist_ok=True)
            os.utime(directory, (self.clock(), self.clock()))
            self.cleanup_stale()
            return directory

    def new_path(self, state, suffix=".bin"):
        if not re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", suffix):
            suffix = ".bin"
        return self.ensure(state) / f"{uuid4().hex}{suffix}"

    @contextmanager
    def lease(self, state):
        with self._lock:
            directory = self.ensure(state)
            self._active[directory.name] = self._active.get(directory.name, 0) + 1
        try:
            yield directory
        finally:
            with self._lock:
                self._active[directory.name] -= 1
                if not self._active[directory.name]:
                    del self._active[directory.name]
                if directory.exists():
                    os.utime(directory, (self.clock(), self.clock()))

    def clear(self, state, *, keep=None):
        with self._lock:
            workspace_id = state.get("_workspace_id")
            if not workspace_id:
                return
            directory = self._directory(workspace_id)
            if self._active.get(workspace_id):
                raise ValueError("The session has an active job")
            if not directory.exists():
                return
            if keep is None:
                shutil.rmtree(directory)
                state.pop("_workspace_id", None)
            else:
                protected = Path(keep).resolve()
                if protected.parent != directory.resolve():
                    raise ValueError("Prepared file is outside this session")
                for child in directory.iterdir():
                    if child.resolve() != protected and child.is_file():
                        child.unlink()

    def cleanup_stale(self):
        with self._lock:
            now = self.clock()
            if now - self._last_sweep < 60:
                return
            self._last_sweep = now
            for child in list(self.root.iterdir())[:64]:
                if not re.fullmatch(r"[0-9a-f]{32}", child.name):
                    continue
                directory = self._directory(child.name)
                if (directory.is_dir() and not self._active.get(child.name)
                        and now - directory.stat().st_mtime > self.idle_seconds):
                    shutil.rmtree(directory)


SESSION_FILES = SessionWorkspace()
