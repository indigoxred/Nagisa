"""Small, fail-closed client for the supported Shoko TMDB ordering API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ShokoClientError(RuntimeError):
    """Base error that deliberately never contains authentication material."""


class MalformedResponse(ShokoClientError):
    """Shoko returned a response that did not match the required JSON shape."""


class UnsupportedOrUnhealthy(ShokoClientError):
    """A write was refused because compatibility or health was not proven."""


class PostWriteVerificationError(ShokoClientError):
    """Shoko accepted a write but the required state was not observed afterward."""


@dataclass(frozen=True, slots=True)
class Compatibility:
    supported: bool
    server_version: str | None
    reason: str


class ShokoClient:
    """Use reads broadly, but permit writes only for an attested Shoko API shape."""

    _ORDERING_PATH = "/Tmdb/Show/{showID}/Ordering"
    _SET_PREFERRED_PATH = "/Tmdb/Show/{showID}/Ordering/SetPreferred"
    _SET_PREFERRED_SCHEMA = "#/components/schemas/APIv3.Tmdb.SetPreferredOrderingBody"

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        allowed_versions: Iterable[str],
        timeout: float = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._allowed_versions = frozenset(allowed_versions)
        self._timeout = timeout

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["apikey"] = self._api_token
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as error:
            code = error.code
            error.close()
            raise ShokoClientError(
                f"Shoko request failed: {method} {path} returned HTTP {code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ShokoClientError(
                f"Shoko request failed: {method} {path} was unavailable"
            ) from None
        # Shoko's SetPreferred endpoint returns an empty 2xx response on the
        # current server build.  Accept that only for a POST; set_preferred()
        # still performs an exact GET verification immediately afterward.
        if method == "POST" and not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MalformedResponse(
                f"Shoko returned malformed JSON for {method} {path}"
            ) from None

    def request_json(self, method: str, path: str) -> Any:
        """Expose authenticated stock GETs to private read-only consumers."""
        if method != 'GET' or not path.startswith('/api/v3/'):
            raise ValueError('only stock Shoko API reads are allowed')
        return self._request_json(path)

    @staticmethod
    def _list_envelope(value: Any, path: str) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or not isinstance(value.get("List"), list):
            raise MalformedResponse(f"Shoko returned an invalid list envelope for GET {path}")
        if not all(isinstance(row, dict) for row in value["List"]):
            raise MalformedResponse(f"Shoko returned invalid list rows for GET {path}")
        return value["List"]

    @classmethod
    def _schema_supported(cls, document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        paths = document.get("paths")
        schemas = document.get("components", {}).get("schemas")
        if not isinstance(paths, dict) or not isinstance(schemas, dict):
            return False
        ordering = paths.get(cls._ORDERING_PATH)
        setter = paths.get(cls._SET_PREFERRED_PATH)
        if not isinstance(ordering, dict) or not isinstance(ordering.get("get"), dict):
            return False
        if not isinstance(setter, dict) or not isinstance(setter.get("post"), dict):
            return False
        post = setter["post"]
        try:
            schema_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        except (KeyError, TypeError):
            return False
        if schema_ref != cls._SET_PREFERRED_SCHEMA:
            return False
        schema = schemas.get("APIv3.Tmdb.SetPreferredOrderingBody")
        if not isinstance(schema, dict):
            return False
        required = schema.get("required")
        properties = schema.get("properties")
        return (
            isinstance(required, list)
            and "AlternateOrderingID" in required
            and isinstance(properties, dict)
            and isinstance(properties.get("AlternateOrderingID"), dict)
            and properties["AlternateOrderingID"].get("type") == "string"
        )

    def compatibility(self) -> Compatibility:
        try:
            status = self._request_json("/api/v3/Init/Status")
            version_document = self._request_json("/api/v3/Init/Version")
            openapi = self._request_json(
                "/swagger/v3/swagger.json", authenticated=False
            )
        except ShokoClientError:
            return Compatibility(False, None, "UNHEALTHY_OR_UNREADABLE")

        try:
            server_version = version_document["Server"]["Version"]
            state = status["State"]
            blocked = status["DatabaseBlocked"]["Blocked"]
        except (KeyError, TypeError):
            return Compatibility(False, None, "UNHEALTHY_OR_UNREADABLE")
        if not isinstance(server_version, str):
            return Compatibility(False, None, "UNHEALTHY_OR_UNREADABLE")
        if state != "Started" or blocked is not False:
            return Compatibility(False, server_version, "UNHEALTHY")
        if server_version not in self._allowed_versions:
            return Compatibility(False, server_version, "UNSUPPORTED_SERVER_VERSION")
        if not self._schema_supported(openapi):
            return Compatibility(False, server_version, "UNSUPPORTED_OPENAPI_SCHEMA")
        return Compatibility(True, server_version, "SUPPORTED")

    def list_shows(self) -> list[dict[str, Any]]:
        path = "/api/v3/Tmdb/Show?" + urlencode({"page": 1, "pageSize": 0})
        return self._list_envelope(self._request_json(path), path)

    def get_orderings(self, show_id: int) -> list[dict[str, Any]]:
        path = f"/api/v3/Tmdb/Show/{show_id}/Ordering"
        value = self._request_json(path)
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise MalformedResponse(f"Shoko returned invalid orderings for GET {path}")
        return value

    def get_current_preference(self, show_id: int) -> str:
        preferred = [
            row for row in self.get_orderings(show_id)
            if row.get("IsPreferred") is True
        ]
        if len(preferred) != 1:
            raise MalformedResponse(
                f"Shoko returned an invalid preferred ordering for show {show_id}"
            )
        if preferred[0].get("IsDefault") is True:
            return "default"
        ordering_id = str(preferred[0].get("OrderingID", ""))
        if not ordering_id:
            raise MalformedResponse(
                f"Shoko returned an empty preferred ordering for show {show_id}"
            )
        return ordering_id

    def get_evidence(self, show_id: int, ordering_id: str) -> dict[str, Any]:
        episode_path = (
            f"/api/v3/Tmdb/Show/{show_id}/Episode?"
            + urlencode(
                {"page": 1, "pageSize": 0, "alternateOrderingID": ordering_id}
            )
        )
        cross_reference_path = f"/api/v3/Tmdb/Show/{show_id}/Episode/CrossReferences"
        series_path = (
            f"/api/v3/Tmdb/Show/{show_id}/Shoko/Series?"
            + urlencode({"page": 1, "pageSize": 0})
        )
        file_path = (
            f"/api/v3/Tmdb/Show/{show_id}/Shoko/File?"
            + urlencode({"page": 1, "pageSize": 0})
        )
        return {
            "episodes": self._request_json(episode_path),
            "cross_references": self._request_json(cross_reference_path),
            "series": self._request_json(series_path),
            "files": self._request_json(file_path),
        }

    @staticmethod
    def _complete_envelope(value: Any, path: str) -> tuple[list[dict[str, Any]], bool]:
        rows = ShokoClient._list_envelope(value, path)
        total = value.get("Total")
        return rows, isinstance(total, int) and total == len(rows)

    def get_evaluation_inputs(self, show_id: int) -> dict[str, Any]:
        """Normalize only exact IDs/counts required by the pure evaluator.

        Titles, filenames, paths and inferred media identity are intentionally
        excluded from the returned structure.
        """

        raw_orderings = self.get_orderings(show_id)
        preferred_rows = [row for row in raw_orderings if row.get("IsPreferred") is True]
        current_preference: str | None = None
        common_complete = len(preferred_rows) == 1
        if len(preferred_rows) == 1:
            current_preference = (
                "default"
                if preferred_rows[0].get("IsDefault") is True
                else str(preferred_rows[0].get("OrderingID", "")) or None
            )

        cross_reference_path = f"/api/v3/Tmdb/Show/{show_id}/Episode/CrossReferences"
        cross_references, cross_references_complete = self._complete_envelope(
            self._request_json(cross_reference_path), cross_reference_path
        )
        common_complete = common_complete and cross_references_complete

        series_path = (
            f"/api/v3/Tmdb/Show/{show_id}/Shoko/Series?"
            + urlencode({"page": 1, "pageSize": 0})
        )
        raw_series = self._request_json(series_path)
        if not isinstance(raw_series, list) or not all(
            isinstance(row, dict) for row in raw_series
        ):
            raise MalformedResponse(f"Shoko returned invalid series rows for GET {series_path}")

        cross_references_by_anime: dict[int, list[dict[str, Any]]] = {}
        for row in cross_references:
            try:
                anime_id = int(row["AnidbAnimeID"])
                int(row["AnidbEpisodeID"])
                int(row["TmdbEpisodeID"])
            except (KeyError, TypeError, ValueError):
                common_complete = False
                continue
            cross_references_by_anime.setdefault(anime_id, []).append(row)

        required: list[dict[str, Any]] = []
        tmdb_by_shoko: dict[int, int] = {}
        seen_shoko: set[int] = set()
        seen_anidb_episode: set[int] = set()
        required_cross_reference_count = 0
        processed_anime_ids: set[int] = set()
        for series in raw_series:
            try:
                series_id = int(series["IDs"]["ID"])
                anidb_anime_id = int(series["IDs"]["AniDB"])
            except (KeyError, TypeError, ValueError):
                common_complete = False
                continue
            processed_anime_ids.add(anidb_anime_id)
            series_episode_path = (
                f"/api/v3/Series/{series_id}/Episode?"
                + urlencode({"page": 1, "pageSize": 0})
            )
            anidb_episode_path = (
                f"/api/v3/Series/AniDB/{anidb_anime_id}/Episode?"
                + urlencode({"page": 1, "pageSize": 0})
            )
            series_episodes, series_complete = self._complete_envelope(
                self._request_json(series_episode_path), series_episode_path
            )
            anidb_episodes, anidb_complete = self._complete_envelope(
                self._request_json(anidb_episode_path), anidb_episode_path
            )
            common_complete = common_complete and series_complete and anidb_complete

            shoko_by_anidb: dict[int, dict[str, Any]] = {}
            for episode in series_episodes:
                try:
                    anidb_episode_id = int(episode["IDs"]["AniDB"])
                except (KeyError, TypeError, ValueError):
                    common_complete = False
                    continue
                if anidb_episode_id in shoko_by_anidb:
                    common_complete = False
                shoko_by_anidb[anidb_episode_id] = episode

            anidb_by_id: dict[int, dict[str, Any]] = {}
            for episode in anidb_episodes:
                try:
                    anidb_episode_id = int(episode["ID"])
                except (KeyError, TypeError, ValueError):
                    common_complete = False
                    continue
                if anidb_episode_id in anidb_by_id:
                    common_complete = False
                anidb_by_id[anidb_episode_id] = episode

            for reference in cross_references_by_anime.get(anidb_anime_id, []):
                anidb_episode_id = int(reference["AnidbEpisodeID"])
                tmdb_episode_id = int(reference["TmdbEpisodeID"])
                shoko_episode = shoko_by_anidb.get(anidb_episode_id)
                anidb_episode = anidb_by_id.get(anidb_episode_id)
                # Ignore only a two-sided stale row: both complete current
                # envelopes agree that the referenced episode no longer
                # exists. A one-sided disagreement remains fail-closed.
                if shoko_episode is None and anidb_episode is None:
                    continue
                required_cross_reference_count += 1
                if shoko_episode is None or anidb_episode is None:
                    common_complete = False
                    continue
                try:
                    shoko_episode_id = int(shoko_episode["IDs"]["ID"])
                    local_number = int(anidb_episode["EpisodeNumber"])
                    raw_type = anidb_episode["Type"]
                    file_count = int(shoko_episode.get("Size", 0))
                except (KeyError, TypeError, ValueError):
                    common_complete = False
                    continue
                kind = {"Episode": "regular", "Special": "special"}.get(raw_type)
                if kind is None or local_number <= 0 or file_count < 0:
                    common_complete = False
                    continue
                if (
                    shoko_episode_id in seen_shoko
                    or anidb_episode_id in seen_anidb_episode
                    or shoko_episode_id in tmdb_by_shoko
                ):
                    common_complete = False
                    continue
                seen_shoko.add(shoko_episode_id)
                seen_anidb_episode.add(anidb_episode_id)
                tmdb_by_shoko[shoko_episode_id] = tmdb_episode_id
                required.append(
                    {
                        "shoko_episode_id": shoko_episode_id,
                        "series_id": series_id,
                        "series_episode_number": local_number,
                        "kind": kind,
                        "file_backed": file_count > 0,
                        "linked": True,
                    }
                )

        if any(
            rows for anime_id, rows in cross_references_by_anime.items()
            if anime_id not in processed_anime_ids
        ):
            common_complete = False
        if len(required) != required_cross_reference_count or not required:
            common_complete = False

        normalized_orderings: list[dict[str, Any]] = []
        for ordering in raw_orderings:
            is_default = ordering.get("IsDefault") is True
            ordering_id = "default" if is_default else str(ordering.get("OrderingID", ""))
            if not ordering_id:
                common_complete = False
                continue
            episode_path = (
                f"/api/v3/Tmdb/Show/{show_id}/Episode?"
                + urlencode(
                    {
                        "page": 1,
                        "pageSize": 0,
                        "alternateOrderingID": ordering_id,
                    }
                )
            )
            provider_rows, provider_complete = self._complete_envelope(
                self._request_json(episode_path), episode_path
            )
            provider_by_id: dict[int, dict[str, Any]] = {}
            for provider_row in provider_rows:
                try:
                    provider_id = int(provider_row["ID"])
                except (KeyError, TypeError, ValueError):
                    provider_complete = False
                    continue
                if provider_id in provider_by_id:
                    provider_complete = False
                provider_by_id[provider_id] = provider_row

            normalized_rows: list[dict[str, Any]] = []
            for source in required:
                shoko_episode_id = int(source["shoko_episode_id"])
                provider_id = tmdb_by_shoko[shoko_episode_id]
                provider_row = provider_by_id.get(provider_id)
                if provider_row is None:
                    provider_complete = False
                    continue
                try:
                    season = int(provider_row["SeasonNumber"])
                    episode = int(provider_row["EpisodeNumber"])
                    hidden = provider_row["IsHidden"] is True
                except (KeyError, TypeError, ValueError):
                    provider_complete = False
                    continue
                normalized_rows.append(
                    {
                        "shoko_episode_id": shoko_episode_id,
                        "provider_episode_id": provider_id,
                        "season": season,
                        "episode": episode,
                        "hidden": hidden,
                    }
                )
            normalized_orderings.append(
                {
                    "id": ordering_id,
                    "is_default": is_default,
                    "evidence_complete": common_complete and provider_complete,
                    "episodes": normalized_rows,
                }
            )

        required.sort(key=lambda row: int(row["shoko_episode_id"]))
        return {
            "current_preference": current_preference,
            "required_episodes": required,
            "orderings": normalized_orderings,
        }

    def set_preferred(self, show_id: int, ordering_id: str) -> None:
        compatibility = self.compatibility()
        if not compatibility.supported:
            raise UnsupportedOrUnhealthy(
                "Shoko ordering mutation refused: " + compatibility.reason
            )
        path = f"/api/v3/Tmdb/Show/{show_id}/Ordering/SetPreferred"
        self._request_json(
            path,
            method="POST",
            body={"AlternateOrderingID": ordering_id},
        )
        orderings = self.get_orderings(show_id)
        preferred = [row for row in orderings if row.get("IsPreferred") is True]
        preferred_matches = (
            len(preferred) == 1
            and (
                preferred[0].get("IsDefault") is True
                if ordering_id == "default"
                else str(preferred[0].get("OrderingID")) == str(ordering_id)
            )
        )
        if not preferred_matches:
            raise PostWriteVerificationError(
                f"Shoko ordering write could not be verified for show {show_id}"
            )
