"""Content-addressed file store (PRD §6.3).

Layout: <files_dir>/<sha256[:2]>/<sha256[2:4]>/<sha256>
Identical bytes deduplicate naturally; revision/relisting semantics live in
the database, not the filesystem.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    local_path: Path
    size_bytes: int
    deduplicated: bool


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def put_bytes(self, data: bytes) -> StoredFile:
        sha256 = hashlib.sha256(data).hexdigest()
        dest = self._path_for(sha256)
        if dest.exists():
            return StoredFile(sha256=sha256, local_path=dest, size_bytes=dest.stat().st_size, deduplicated=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp in same dir then rename.
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, dest)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return StoredFile(sha256=sha256, local_path=dest, size_bytes=len(data), deduplicated=False)

    def put_file(self, src: Path) -> StoredFile:
        h = hashlib.sha256()
        size = 0
        with src.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        sha256 = h.hexdigest()
        dest = self._path_for(sha256)
        if dest.exists():
            return StoredFile(sha256=sha256, local_path=dest, size_bytes=dest.stat().st_size, deduplicated=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.parent / f".tmp_{sha256}"
        try:
            shutil.copyfile(src, tmp_path)
            os.replace(tmp_path, dest)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        return StoredFile(sha256=sha256, local_path=dest, size_bytes=size, deduplicated=False)

    def open(self, sha256: str) -> Path:
        p = self._path_for(sha256)
        if not p.exists():
            raise FileNotFoundError(f"No stored file with sha256={sha256}")
        return p

    def verify(self, sha256: str) -> bool:
        """Recompute hash on disk and compare. Used for corruption checks."""
        p = self._path_for(sha256)
        if not p.exists():
            return False
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == sha256
