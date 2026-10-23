"""Read-only exact Plex part observation."""

from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


_EPISODE_GUID = re.compile(r"^com\.plexapp\.agents\.shokorelay://(?P<series>[1-9][0-9]*)/-?[0-9]+/[1-9][0-9]*\?lang=[A-Za-z-]+$")


class PlexObserver:
    def __init__(self, transport: Any, *, source_mounts=None, source_aliases=None) -> None:
        self.transport = transport
        self.source_mounts = {}
        for native, local in (source_mounts or {}).items():
            prefix = PurePosixPath(native)
            root = Path(local).resolve(strict=True)
            if not prefix.is_absolute() or '..' in prefix.parts or str(prefix) == '/' or not root.is_dir():
                raise ValueError('explicit Plex source mappings required')
            self.source_mounts[prefix] = root
        self.source_aliases = []
        for native, retained in (source_aliases or {}).items():
            roots = (Path(native), Path(retained))
            if any(not root.is_absolute() or root.is_symlink() or '..' in root.parts for root in roots):
                raise ValueError('canonical local source aliases required')
            roots = tuple(root.resolve(strict=True) for root in roots)
            if any(not root.is_dir() or root == Path(root.anchor) for root in roots):
                raise ValueError('bounded source alias directories required')
            self.source_aliases.append(roots)

    @staticmethod
    def _source_token(source):
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0:
            raise ValueError('regular identifiable source required')
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _matches_source(self, resolved, source, token):
        if resolved != source:
            # Different paths must be the same relative source under an
            # explicitly declared local mount alias pair. Equal bytes alone
            # never establish this physical identity.
            if not any(
                resolved.is_relative_to(native) and source.is_relative_to(retained)
                and resolved.relative_to(native) == source.relative_to(retained)
                for native, retained in self.source_aliases
            ):
                return False
        return self._source_token(resolved) == token == self._source_token(source)

    @staticmethod
    def normalize_parts(payload: object) -> tuple[tuple[str, bool, str], ...]:
        metadata = payload.get("MediaContainer", {}).get("Metadata", []) if isinstance(payload, dict) else []
        parts: list[tuple[str, bool, str]] = []
        for item in metadata if isinstance(metadata, list) else []:
            guid = str(item.get("guid", "")) if isinstance(item, dict) else ""
            for media in item.get("Media", ()) if isinstance(item, dict) else ():
                for part in media.get("Part", ()) if isinstance(media, dict) else ():
                    path = part.get("file") if isinstance(part, dict) else None
                    if isinstance(path, str):
                        parts.append((path, part.get("accessible") is not False, guid))
        return tuple(sorted(set(parts)))

    @staticmethod
    def has_exact_playable_part(parts: tuple[tuple[str, bool, str], ...], relative: str, shoko_series_id: int) -> bool:
        target = "/tv2/" + relative.lstrip("/")
        matches = []
        for path, accessible, guid in parts:
            match = _EPISODE_GUID.fullmatch(guid)
            if path == target and accessible and match and int(match.group("series")) == shoko_series_id:
                matches.append((path, guid))
        return len(set(matches)) == 1

    def parts_for_section(self, section_id: int) -> tuple[tuple[str, bool, str], ...]:
        start = 0
        page_size = 500
        parts: list[tuple[str, bool, str]] = []
        for _ in range(100):
            path = (
                f"/library/sections/{int(section_id)}/all?type=4&includeGuids=1"
                f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={page_size}"
            )
            payload = self.transport.request_json("GET", path)
            container = payload.get("MediaContainer") if isinstance(payload, dict) else None
            if not isinstance(container, dict):
                raise ValueError("malformed Plex library response")
            parts.extend(self.normalize_parts(payload))
            try:
                total = int(container.get("totalSize", container.get("size", 0)))
                size = int(container.get("size", len(container.get("Metadata", ()))))
            except (TypeError, ValueError):
                raise ValueError("malformed Plex pagination") from None
            if start + size >= total:
                return tuple(sorted(set(parts)))
            if size <= 0:
                raise ValueError("Plex pagination did not advance")
            start += size
        raise ValueError("Plex pagination exceeded the safety limit")

    def compatibility(self) -> bool:
        payload = self.transport.request_json("GET", "/identity")
        container = payload.get("MediaContainer") if isinstance(payload, dict) else None
        return isinstance(container, dict) and isinstance(container.get("machineIdentifier"), str)

    def has_exact_destination(self, relative: str, shoko_series_id: int, section_ids: tuple[int, ...]) -> bool:
        matches = []
        for section_id in section_ids:
            matches.extend(self.parts_for_section(section_id))
        return self.has_exact_playable_part(tuple(matches), relative, shoko_series_id)

    def has_exact_identity_destination(self, relative, identity, section_ids, source) -> bool:
        """Verify modern native episode/owner/source relations, retaining old GUIDs.

        The stock owner reader hydrates show and season ancestry independently;
        an episode ID is never reused as a series ID or presentation coordinate.
        """
        from nagisa.reconciler.arrival_readers import StockPlexOwnerReader
        from nagisa.reconciler.arrival import PreservationError
        try:
            regular = identity.regular[0]
            if len(identity.regular) != 1:
                return False
            source_token = None
            if self.source_mounts:
                try:
                    source = Path(source).resolve(strict=True)
                    source_token = self._source_token(source)
                except (OSError, ValueError, TypeError):
                    pass
            if self.has_exact_destination(relative, regular.shoko_series_id, section_ids):
                return True
            if not self.source_mounts or source_token is None:
                return False
            if self._source_token(source) != source_token:
                return False
            expected_guid = f'tv.plex.agents.custom.shoko://episode/e{regular.shoko_episode_id}'
            sections = StockPlexOwnerReader(self.transport, section_ids)(regular.shoko_series_id)
            found = False
            qualified_targets = []
            for items in sections.values():
                matches = set()
                for item in items:
                    if item['guid'] != expected_guid:
                        continue
                    for media in item['Media']:
                        for part in media['Part']:
                            raw = part['file']
                            native = PurePosixPath(raw)
                            if '\\' in raw or '\x00' in raw or raw.startswith('//') or not native.is_absolute() or '..' in native.parts:
                                continue
                            candidates = []
                            for prefix, root in self.source_mounts.items():
                                if native.is_relative_to(prefix):
                                    target = root.joinpath(*native.relative_to(prefix).parts)
                                    resolved = target.resolve(strict=True)
                                    if self._matches_source(resolved, source, source_token) and target.is_file():
                                        candidates.append((target, resolved))
                            if len(candidates) == 1 and part.get('accessible') is not False and int(part['size']) == source_token[2]:
                                matches.add(str(item['ratingKey']))
                                qualified_targets.extend(candidates)
                if len(matches) > 1:
                    return False
                found = found or len(matches) == 1
            return found and self._source_token(source) == source_token and all(
                target.resolve(strict=True) == resolved
                and self._matches_source(resolved, source, source_token)
                for target, resolved in qualified_targets
            )
        except (PreservationError, OSError, ValueError, TypeError, KeyError, IndexError):
            return False
