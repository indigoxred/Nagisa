"""Read-only normalization of exact Shoko hash identity."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote

from nagisa.reconciler.models import ShokoEpisodeIdentity, ShokoFileIdentity


class MissingTvdbEpisodeIdentity(ValueError):
    """The exact Shoko/TMDB episode exists but has no TVDB identity yet."""


class ShokoObserver:
    def __init__(self, transport: Any, *, allowed_versions: Iterable[str] = (), managed_roots=None, destination_root=None) -> None:
        self.transport = transport
        self.allowed_versions = frozenset(allowed_versions)
        self.managed_roots = {key: Path(root).resolve(strict=True) for key, root in (managed_roots or {}).items()}
        self.destination_root = Path(destination_root).resolve(strict=True) if destination_root is not None else None
        if self.managed_roots and (self.destination_root is None or not self.destination_root.is_dir()):
            raise ValueError('Shoko destination root mapping required')
        if any(type(key) is not int or key <= 0 or not root.is_dir() for key, root in self.managed_roots.items()):
            raise ValueError('Shoko managed root mapping is invalid')

    def _registered_locations(self, row):
        """Translate stock managed-folder paths to this retained destination.

        New Shoko paths start with a slash relative to their ManagedFolderID.
        Missing generated VFS alternates are harmless; source escapes are not.
        """
        if not self.managed_roots:
            return row
        locations = row.get('Locations')
        if not isinstance(locations, list):
            raise ValueError('Shoko registered locations missing')
        normalized = []
        for location in locations:
            if not isinstance(location, dict):
                raise ValueError('Shoko registered location malformed')
            root = self.managed_roots.get(location.get('ManagedFolderID'))
            if root is None:
                continue
            if type(location.get('FileID')) is not int or location['FileID'] != row['ID']:
                raise ValueError('Shoko location belongs to a different file')
            raw = location.get('RelativePath')
            if not isinstance(raw, str) or '\\' in raw or '\x00' in raw or raw.startswith('//'):
                raise ValueError('Shoko registered path invalid')
            relative = PurePosixPath(raw.lstrip('/'))
            if not relative.parts or '..' in relative.parts:
                raise ValueError('Shoko registered path unsafe')
            target = root.joinpath(*relative.parts)
            if not target.resolve(strict=False).is_relative_to(root):
                raise ValueError('Shoko registered source escapes managed root')
            try:
                resolved = target.resolve(strict=True)
            except FileNotFoundError:
                continue
            if not resolved.is_relative_to(root):
                raise ValueError('Shoko registered source escapes managed root')
            if location.get('IsAccessible') is True and resolved.is_file() and resolved.is_relative_to(self.destination_root):
                normalized.append({'RelativePath': resolved.relative_to(self.destination_root).as_posix(), 'IsAccessible': True})
        return {**row, 'Locations': normalized}

    def compatibility(self) -> bool:
        status = self.transport.request_json("GET", "/api/v3/Init/Status")
        version = self.transport.request_json("GET", "/api/v3/Init/Version")
        try:
            current = str(version["Server"]["Version"])
            started = status["State"] == "Started"
            unblocked = status["DatabaseBlocked"]["Blocked"] is False
        except (KeyError, TypeError):
            return False
        return started and unblocked and (not self.allowed_versions or current in self.allowed_versions)

    def by_path_ending(self, filename: str, *, expected_size: int) -> ShokoFileIdentity:
        if (
            not isinstance(filename, str) or not filename or "\x00" in filename
            or PurePosixPath(filename).name != filename or expected_size <= 0
        ):
            raise ValueError("Shoko path identity is invalid")
        path = f"/api/v3/File/PathEndsWith/{quote(filename, safe='')}?includeXRefs=true&limit=10"
        payload = self.transport.request_json("GET", path)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError("Shoko path did not resolve to exactly one file")
        row = payload[0]
        if int(row.get("Size", 0)) != expected_size:
            raise ValueError("Shoko file size does not match Sonarr history")
        try:
            file_id = int(row["ID"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Shoko file identity is malformed") from None
        episodes_path = f"/api/v3/File/{file_id}/Episode?includeXRefs=true&includeDataFrom=AniDB"
        episodes = self.transport.request_json("GET", episodes_path)
        try:
            anidb_file = self.transport.request_json("GET", f"/api/v3/File/{file_id}/AniDB")
        except (OSError, RuntimeError, ValueError):
            anidb_file = None
        return self.normalize_file_identity(self._registered_locations(row), episodes, anidb_file)

    def series_ids_for_tmdb(self, tmdb_id: int) -> frozenset[int]:
        """Return the exact Shoko series owned by one current TMDB show.

        This supported Shoko endpoint provides the semantic boundary needed to
        prove that a file Shoko recognized still belongs to the show Sonarr
        requested. Empty, duplicate or malformed evidence fails closed.
        """
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise ValueError("TMDB show identity is invalid")
        path = f"/api/v3/Tmdb/Show/{tmdb_id}/Shoko/Series?page=1&pageSize=0"
        payload = self.transport.request_json("GET", path)
        if not isinstance(payload, list) or not payload:
            raise ValueError("Shoko TMDB series membership is missing")
        series_ids: list[int] = []
        for row in payload:
            try:
                identifiers = row["IDs"]
                series_id = int(identifiers["ID"])
                anidb_id = int(identifiers["AniDB"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Shoko TMDB series membership is malformed") from None
            if series_id <= 0 or anidb_id <= 0:
                raise ValueError("Shoko TMDB series membership is invalid")
            series_ids.append(series_id)
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("Shoko TMDB series membership is ambiguous")
        return frozenset(series_ids)

    def preferred_tmdb_coordinate(
        self, shoko_episode_id: int, tmdb_show_id: int,
    ) -> tuple[int, int]:
        """Return one episode's coordinate in Shoko's preferred TMDB order.

        All evidence comes from supported Shoko endpoints. A missing TMDB link,
        a link to a different show, or multiple preferred coordinates is
        ambiguous and therefore fails closed.
        """
        if (
            not isinstance(shoko_episode_id, int) or shoko_episode_id <= 0
            or not isinstance(tmdb_show_id, int) or tmdb_show_id <= 0
        ):
            raise ValueError("Shoko or TMDB episode identity is invalid")
        linked_path = f"/api/v3/Episode/{shoko_episode_id}/TMDB/Episode"
        linked = self.transport.request_json("GET", linked_path)
        if not isinstance(linked, list) or not linked:
            raise ValueError("Shoko episode has no exact TMDB episode link")
        matching_episode_ids: list[int] = []
        for row in linked:
            try:
                tmdb_episode_id = int(row["ID"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Shoko TMDB episode link is malformed") from None
            if tmdb_episode_id <= 0:
                raise ValueError("Shoko TMDB episode link is invalid")
            show = self.transport.request_json(
                "GET", f"/api/v3/Tmdb/Episode/{tmdb_episode_id}/Show"
            )
            try:
                linked_show_id = int(show["ID"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Shoko TMDB episode show is malformed") from None
            if linked_show_id == tmdb_show_id:
                matching_episode_ids.append(tmdb_episode_id)
        if len(matching_episode_ids) != 1:
            raise ValueError("Shoko TMDB episode identity is missing or ambiguous")
        ordering = self.transport.request_json(
            "GET", f"/api/v3/Tmdb/Episode/{matching_episode_ids[0]}/Ordering"
        )
        if not isinstance(ordering, list) or not ordering:
            raise ValueError("Shoko TMDB preferred ordering is missing")
        preferred = [row for row in ordering if isinstance(row, dict) and row.get("IsPreferred") is True]
        if len(preferred) != 1:
            raise ValueError("Shoko TMDB preferred ordering is ambiguous")
        try:
            season = int(preferred[0]["SeasonNumber"])
            episode = int(preferred[0]["EpisodeNumber"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Shoko TMDB preferred coordinate is malformed") from None
        if season < 0 or episode <= 0:
            raise ValueError("Shoko TMDB preferred coordinate is invalid")
        return season, episode

    def has_exact_tmdb_episode_identity(
        self, shoko_episode_id: int, tmdb_show_id: int, tvdb_episode_id: int,
    ) -> bool:
        """Prove one Shoko episode is the exact episode Sonarr identified.

        Sonarr's acquisition coordinates can legitimately differ from Shoko's
        preferred TMDB presentation order. Shoko's supported TMDB episode
        response carries the exact TVDB episode ID Sonarr uses, so that stable
        provider identity is the retirement boundary rather than season labels.
        """
        if (
            not isinstance(shoko_episode_id, int) or shoko_episode_id <= 0
            or not isinstance(tmdb_show_id, int) or tmdb_show_id <= 0
            or not isinstance(tvdb_episode_id, int) or tvdb_episode_id <= 0
        ):
            raise ValueError("Shoko, TMDB or TVDB episode identity is invalid")
        path = f"/api/v3/Episode/{shoko_episode_id}/TMDB/Episode"
        linked = self.transport.request_json("GET", path)
        if not isinstance(linked, list) or not linked:
            raise ValueError("Shoko episode has no exact TMDB episode link")
        matches: list[int] = []
        matching_show_rows: list[tuple[int, int]] = []
        seen: set[int] = set()
        for row in linked:
            try:
                tmdb_episode_id = int(row["ID"])
                linked_show_id = int(row["ShowID"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Shoko TMDB episode identity is malformed") from None
            raw_tvdb_episode_id = row.get("TvdbEpisodeID")
            if raw_tvdb_episode_id in {None, "", 0, "0"}:
                linked_tvdb_episode_id = 0
            else:
                try:
                    linked_tvdb_episode_id = int(raw_tvdb_episode_id)
                except (TypeError, ValueError):
                    raise ValueError("Shoko TMDB episode identity is malformed") from None
            if (
                tmdb_episode_id <= 0 or linked_show_id <= 0
                or linked_tvdb_episode_id < 0 or tmdb_episode_id in seen
            ):
                raise ValueError("Shoko TMDB episode identity is invalid or ambiguous")
            seen.add(tmdb_episode_id)
            if linked_show_id == tmdb_show_id:
                matching_show_rows.append((tmdb_episode_id, linked_tvdb_episode_id))
                if linked_tvdb_episode_id == tvdb_episode_id:
                    matches.append(tmdb_episode_id)
        if len(matches) != 1:
            if len(matching_show_rows) == 1 and matching_show_rows[0][1] == 0:
                raise MissingTvdbEpisodeIdentity(
                    "Shoko TMDB episode has no TVDB episode identity yet"
                )
            raise ValueError("Shoko TMDB/TVDB episode identity is missing or ambiguous")
        return True

    @staticmethod
    def normalize_file_identity(
        row: object, episodes: object, anidb_file: object = None,
    ) -> ShokoFileIdentity:
        if not isinstance(row, dict) or not isinstance(episodes, list):
            raise ValueError("Shoko file identity is malformed")
        try:
            file_id = int(row["ID"])
            hashes = row['Hashes']
            if isinstance(hashes, list):
                if any(not isinstance(value, dict) for value in hashes):
                    raise ValueError('Shoko hash list malformed')
                matches = [value['Value'] for value in hashes if value.get('Type') == 'SHA1']
                if len(matches) != 1:
                    raise ValueError('Shoko SHA1 identity is missing or ambiguous')
                sha1 = str(matches[0]).lower()
            else:
                sha1 = str(hashes['SHA1']).lower()
        except (KeyError, TypeError, ValueError):
            raise ValueError("Shoko file identity is malformed") from None
        if file_id <= 0 or len(sha1) != 40 or any(c not in "0123456789abcdef" for c in sha1):
            raise ValueError("Shoko file identity is invalid")
        paths: set[str] = set()
        for item in row.get("Locations", ()):
            if not isinstance(item, dict) or item.get("IsAccessible") is not True:
                continue
            raw = item.get("RelativePath")
            if not isinstance(raw, str):
                continue
            if raw.startswith('/'):
                if (
                    raw.startswith('//') or type(item.get('ManagedFolderID')) is not int
                    or item['ManagedFolderID'] <= 0 or item.get('FileID') != file_id
                ):
                    raise ValueError('Shoko native managed path identity is invalid')
                raw = raw[1:]
            relative = PurePosixPath(raw)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or not raw or "\x00" in raw or '\\' in raw:
                raise ValueError("Shoko file location is unsafe")
            paths.add(raw)
        if not paths:
            raise ValueError("Shoko file has no accessible location")
        regular: list[ShokoEpisodeIdentity] = []
        specials: list[ShokoEpisodeIdentity] = []
        for episode in episodes:
            if not isinstance(episode, dict):
                raise ValueError("Shoko episode identity is malformed")
            try:
                ids = episode["IDs"]
                anidb = episode["AniDB"]
                identity = ShokoEpisodeIdentity(
                    int(anidb["AnimeID"]), int(anidb["EpisodeNumber"]), str(anidb["Type"]),
                    int(ids["ParentSeries"]), int(ids["ID"]),
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError("Shoko episode identity is malformed") from None
            if (
                identity.anidb_series_id <= 0 or identity.anidb_episode_number <= 0
                or identity.shoko_series_id <= 0 or identity.shoko_episode_id <= 0
            ):
                raise ValueError("Shoko episode identity is invalid")
            (regular if identity.kind == "Episode" else specials).append(identity)
        if len(set(regular)) != len(regular) or len(set(specials)) != len(specials) or len(regular) != 1:
            raise ValueError("Shoko regular-provider identity is not unique")
        def languages(key: str) -> tuple[str, ...]:
            if not isinstance(anidb_file, dict):
                return ()
            values = anidb_file.get(key)
            if not isinstance(values, list):
                return ()
            normalized: list[str] = []
            for value in values:
                if not isinstance(value, str):
                    return ()
                language = " ".join(value.split()).lower()
                if not language or len(language) > 40:
                    return ()
                if language not in normalized:
                    normalized.append(language)
            return tuple(normalized)

        return ShokoFileIdentity(
            sha1, file_id, tuple(sorted(paths)), tuple(regular), tuple(specials),
            languages("AudioLanguages"), languages("SubLanguages"),
        )
