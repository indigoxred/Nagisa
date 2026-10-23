"""Private crash-safe journal for Seerr guard request IDs, never credentials."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class JsonSessionJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.name in ('', '.', '..'):
            raise ValueError('absolute guard journal path required')
        if not self.path.parent.is_dir() or self.path.parent.is_symlink():
            raise ValueError('guard journal directory is invalid')

    def load(self) -> dict:
        if self.path.is_symlink():
            raise ValueError('symlink guard journal refused')
        if not self.path.exists():
            return {}
        if not self.path.is_file() or self.path.stat().st_size > 1024 * 1024:
            raise ValueError('guard journal is not a bounded regular file')
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError('guard journal cannot be read') from None
        if not isinstance(value, dict):
            raise ValueError('guard journal has invalid root')
        return value

    def save(self, value: dict) -> None:
        if not isinstance(value, dict) or self.path.is_symlink():
            raise ValueError('invalid guard journal target or value')
        raw = json.dumps(value, separators=(',', ':'), sort_keys=True).encode('utf-8')
        if len(raw) > 1024 * 1024:
            raise ValueError('guard journal exceeds bound')
        fd, name = tempfile.mkstemp(prefix='.seerr-journal-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as file:
                os.chmod(name, 0o600)
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
