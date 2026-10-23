"""Read-only Plex presentation observer using exact ShokoRelay GUIDs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SHOKORELAY_GUID = re.compile(
    r"^com\.plexapp\.agents\.shokorelay://(?P<series_id>[1-9][0-9]*)(?:\?|$)"
)


class PlexObserver:
    def __init__(
        self,
        base_url: str,
        token: str,
        section_ids: Iterable[int],
        *,
        timeout: float = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._section_ids = tuple(int(value) for value in section_ids)
        if not self._section_ids or any(value <= 0 for value in self._section_ids):
            raise ValueError("at least one positive Plex section ID is required")
        self._timeout = timeout

    def __repr__(self) -> str:
        return (
            f"PlexObserver(base_url={self._base_url!r}, "
            f"section_ids={self._section_ids!r})"
        )

    def _section_guids(self, section_id: int) -> set[int] | None:
        path = (
            f"/library/sections/{section_id}/all?"
            + urlencode({"type": 2, "includeGuids": 1})
        )
        request = Request(
            f"{self._base_url}{path}",
            headers={
                "Accept": "application/xml",
                "X-Plex-Token": self._token,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                root = ET.fromstring(response.read())
        except HTTPError as error:
            error.close()
            return None
        except (URLError, TimeoutError, OSError, ET.ParseError):
            return None
        series_ids: set[int] = set()
        for element in root.iter():
            values = []
            primary = element.attrib.get("guid")
            if primary:
                values.append(primary)
            if element.tag == "Guid" and element.attrib.get("id"):
                values.append(element.attrib["id"])
            for value in values:
                match = SHOKORELAY_GUID.match(value)
                if match:
                    series_ids.add(int(match.group("series_id")))
        return series_ids

    def has_presented(
        self, show_id: int, series_ids: set[int]
    ) -> bool | None:
        del show_id  # Identity comes exclusively from exact Shoko series GUIDs.
        if not series_ids or any(value <= 0 for value in series_ids):
            return None
        observed: set[int] = set()
        for section_id in self._section_ids:
            section = self._section_guids(section_id)
            if section is None:
                return None
            observed.update(section)
            if observed & series_ids:
                return True
        return False

