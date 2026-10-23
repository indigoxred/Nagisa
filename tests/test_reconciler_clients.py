from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from nagisa.reconciler.plex import PlexObserver
from nagisa.reconciler.qbit import QbitObserver
from nagisa.reconciler.shoko import MissingTvdbEpisodeIdentity, ShokoObserver
from nagisa.reconciler.sonarr import SonarrClient


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], object] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[tuple[str, str, object | None]] = []

    def request_json(self, method: str, path: str, body=None):
        self.requests.append((method, path, body))
        value = self.responses.get((method, path))
        if isinstance(value, Exception):
            raise value
        return value


class ReconcilerClientTests(unittest.TestCase):

    def test_manual_import_without_download_id_is_typed_unlinked(self) -> None:
        rows = [{
            "id": 8431 + offset, "seriesId": 112, "episodeId": 500 + offset,
            "eventType": "downloadFolderImported",
            "data": {"fileId": 100 + offset % 13,
                     "importedPath": f"/Anime/Show/{offset:02}.mkv",
                     "droppedPath": f"/Import/Show/{offset:02}.mkv", "size": 1000},
        } for offset in range(30)]
        later = {**rows[-1], "id": 8461, "episodeId": 900,
                 "downloadId": "a" * 40}
        path = "/api/v3/history?page=1&pageSize=250&sortKey=id&sortDirection=descending"
        transport = FakeTransport({("GET", path): {
            "totalRecords": 31, "records": [later, *reversed(rows)],
        }})
        events = SonarrClient(transport, allowed_versions=["test"],
                              import_root="/Anime").import_history_after(8430)
        self.assertEqual([event.history_id for event in events], list(range(8431, 8462)))
        self.assertEqual(len({event.sonarr_file_id for event in events[:30]}), 13)
        self.assertTrue(all(event.download_id is None for event in events[:30]))
        self.assertEqual(events[-1].download_id, "a" * 40)

        for supplied in (None, "", "bad", "g" * 40):
            invalid = {**rows[0], "downloadId": supplied}
            transport = FakeTransport({("GET", path): {
                "totalRecords": 1, "records": [invalid],
            }})
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                SonarrClient(transport, allowed_versions=["test"],
                             import_root="/Anime").import_history_after(8430)

    def test_later_malformed_page_cannot_return_earlier_unlinked_events(self) -> None:
        first = "/api/v3/history?page=1&pageSize=250&sortKey=id&sortDirection=descending"
        second = "/api/v3/history?page=2&pageSize=250&sortKey=id&sortDirection=descending"
        rows = [{"id": 9000-i, "eventType": "grabbed"} for i in range(250)]
        rows[0] = {"id": 9000, "eventType": "downloadFolderImported",
                   "seriesId": 112, "episodeId": 501,
                   "data": {"fileId": 101, "importedPath": "/Anime/b.mkv",
                            "droppedPath": "/Import/b.mkv", "size": 1000}}
        invalid = {"id": 8750, "eventType": "downloadFolderImported",
                   "seriesId": 112, "episodeId": 500, "downloadId": "bad",
                   "data": {"fileId": 100, "importedPath": "/Anime/a.mkv",
                            "droppedPath": "/Import/a.mkv", "size": 1000}}
        transport = FakeTransport({
            ("GET", first): {"totalRecords": 251, "records": rows},
            ("GET", second): {"totalRecords": 251, "records": [invalid]},
        })
        with self.assertRaises(ValueError):
            SonarrClient(transport, allowed_versions=["test"],
                         import_root="/Anime").import_history_after(8430)
        self.assertEqual([path for _, path, _ in transport.requests], [first, second])












    def test_native_hash_list_keeps_exact_sha1_requirement(self) -> None:
        episode = {"IDs": {"ID": 3, "ParentSeries": 2},
                   "AniDB": {"AnimeID": 1, "EpisodeNumber": 1, "Type": "Episode"}}
        row = {"ID": 4, "Locations": [{"RelativePath": "Show/a.mkv", "IsAccessible": True}]}
        valid = [{"Type": "ED2K", "Value": "b" * 32}, {"Type": "SHA1", "Value": "A" * 40}]
        identity = ShokoObserver.normalize_file_identity({**row, "Hashes": valid}, [episode])
        self.assertEqual(identity.sha1, "a" * 40)
        for hashes in ([], valid[:1], valid + [valid[1]], [None],
                       [{"Type": "SHA1", "Value": "bad"}]):
            with self.subTest(hashes=hashes), self.assertRaises(ValueError):
                ShokoObserver.normalize_file_identity({**row, "Hashes": hashes}, [episode])

    def test_native_managed_folder_path_normalizes_without_accepting_unsafe_paths(self) -> None:
        episode = {"IDs": {"ID": 3, "ParentSeries": 2},
                   "AniDB": {"AnimeID": 1, "EpisodeNumber": 1, "Type": "Episode"}}
        row = {"ID": 4, "Hashes": [{"Type": "SHA1", "Value": "a" * 40}]}
        location = {"FileID": 4, "ManagedFolderID": 2, "RelativePath": "/Show/a.mkv", "IsAccessible": True}
        identity = ShokoObserver.normalize_file_identity({**row, "Locations": [location]}, [episode])
        self.assertEqual(identity.relative_paths, ("Show/a.mkv",))
        for changes in ({"RelativePath": "//server/a.mkv"}, {"RelativePath": "/../a.mkv"},
                        {"RelativePath": "/"}, {"RelativePath": "/Show/../a.mkv"},
                        {"ManagedFolderID": 0}, {"FileID": 5}, {"ManagedFolderID": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ShokoObserver.normalize_file_identity({**row, "Locations": [{**location, **changes}]}, [episode])

    def test_live_shoko_path_and_episode_contract_normalizes_exact_identity(self) -> None:
        path = "/api/v3/File/PathEndsWith/a.mkv?includeXRefs=true&limit=10"
        episodes_path = "/api/v3/File/7/Episode?includeXRefs=true&includeDataFrom=AniDB"
        anidb_path = "/api/v3/File/7/AniDB"
        transport = FakeTransport({
            ("GET", path): [{
                "ID": 7,
                "Size": 100,
                "Hashes": {"SHA1": "A" * 40},
                "Locations": [{"RelativePath": "Group/Show/a.mkv", "IsAccessible": True}],
                "SeriesIDs": [{"SeriesID": {"ID": 1001, "AniDB": 100}, "EpisodeIDs": [{"ID": 4001}]}],
            }],
            ("GET", episodes_path): [{
                "IDs": {"ID": 4001, "ParentSeries": 1001, "AniDB": 5001},
                "AniDB": {"AnimeID": 100, "EpisodeNumber": 1, "Type": "Episode"},
            }],
            ("GET", anidb_path): {
                "AudioLanguages": ["Japanese"],
                "SubLanguages": ["English", "French"],
            },
        })
        identity = ShokoObserver(transport).by_path_ending("a.mkv", expected_size=100)
        self.assertEqual(identity.sha1, "a" * 40)
        self.assertEqual(identity.relative_paths, ("Group/Show/a.mkv",))
        self.assertEqual(
            (identity.regular[0].shoko_series_id, identity.regular[0].shoko_episode_id,
             identity.regular[0].anidb_series_id, identity.regular[0].anidb_episode_number),
            (1001, 4001, 100, 1),
        )
        self.assertEqual(identity.anidb_audio_languages, ("japanese",))
        self.assertEqual(identity.anidb_subtitle_languages, ("english", "french"))
        self.assertEqual(transport.requests, [
            ("GET", path, None), ("GET", episodes_path, None), ("GET", anidb_path, None),
        ])

    def test_duplicate_regular_shoko_mappings_fail_closed(self) -> None:
        observer = ShokoObserver(FakeTransport())
        with self.assertRaises(ValueError):
            observer.normalize_file_identity(
                {"ID": 4, "Size": 100, "Hashes": {"SHA1": "a" * 40}, "Locations": [{"RelativePath": "Show/a.mkv", "IsAccessible": True}]},
                [
                    {"IDs": {"ID": 1, "ParentSeries": 10, "AniDB": 101}, "AniDB": {"AnimeID": 100, "EpisodeNumber": 1, "Type": "Episode"}},
                    {"IDs": {"ID": 2, "ParentSeries": 20, "AniDB": 201}, "AniDB": {"AnimeID": 200, "EpisodeNumber": 1, "Type": "Episode"}},
                ],
            )

    def test_shoko_returns_exact_series_ids_for_tmdb_show(self) -> None:
        path = "/api/v3/Tmdb/Show/84669/Shoko/Series?page=1&pageSize=0"
        transport = FakeTransport({
            ("GET", path): [
                {"IDs": {"ID": 1001, "AniDB": 100}},
                {"IDs": {"ID": 1002, "AniDB": 200}},
            ],
        })

        series_ids = ShokoObserver(transport).series_ids_for_tmdb(84669)

        self.assertEqual(series_ids, frozenset({1001, 1002}))
        self.assertEqual(transport.requests, [("GET", path, None)])

    def test_shoko_tmdb_series_membership_fails_closed_on_empty_or_malformed_rows(self) -> None:
        path = "/api/v3/Tmdb/Show/84669/Shoko/Series?page=1&pageSize=0"
        for payload in ([], [{"IDs": {"ID": 0, "AniDB": 100}}], [{"IDs": {"ID": 1001}}]):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ShokoObserver(FakeTransport({("GET", path): payload})).series_ids_for_tmdb(84669)

    def test_shoko_returns_exact_coordinate_from_current_preferred_ordering(self) -> None:
        linked = "/api/v3/Episode/30668/TMDB/Episode"
        show = "/api/v3/Tmdb/Episode/1894523/Show"
        ordering = "/api/v3/Tmdb/Episode/1894523/Ordering"
        transport = FakeTransport({
            ("GET", linked): [{"ID": 1894523, "SeasonNumber": 2, "EpisodeNumber": 1}],
            ("GET", show): {"ID": 84669, "Title": "The Quintessential Quintuplets"},
            ("GET", ordering): [
                {"OrderingID": "84669", "IsDefault": True, "IsPreferred": False,
                 "SeasonNumber": 2, "EpisodeNumber": 1},
                {"OrderingID": "alternate", "IsDefault": False, "IsPreferred": True,
                 "SeasonNumber": 7, "EpisodeNumber": 3},
            ],
        })

        coordinate = ShokoObserver(transport).preferred_tmdb_coordinate(30668, 84669)

        self.assertEqual(coordinate, (7, 3))
        self.assertEqual(transport.requests, [
            ("GET", linked, None), ("GET", show, None), ("GET", ordering, None),
        ])

    def test_shoko_episode_without_exact_tmdb_coordinate_fails_closed(self) -> None:
        linked = "/api/v3/Episode/30810/TMDB/Episode"
        with self.assertRaises(ValueError):
            ShokoObserver(FakeTransport({("GET", linked): []})).preferred_tmdb_coordinate(
                30810, 84669
            )

    def test_shoko_ambiguous_preferred_coordinate_fails_closed(self) -> None:
        linked = "/api/v3/Episode/30668/TMDB/Episode"
        show = "/api/v3/Tmdb/Episode/1894523/Show"
        ordering = "/api/v3/Tmdb/Episode/1894523/Ordering"
        transport = FakeTransport({
            ("GET", linked): [{"ID": 1894523}],
            ("GET", show): {"ID": 84669},
            ("GET", ordering): [
                {"OrderingID": "first", "IsPreferred": True,
                 "SeasonNumber": 2, "EpisodeNumber": 1},
                {"OrderingID": "second", "IsPreferred": True,
                 "SeasonNumber": 9, "EpisodeNumber": 1},
            ],
        })
        with self.assertRaises(ValueError):
            ShokoObserver(transport).preferred_tmdb_coordinate(30668, 84669)

    def test_shoko_requires_exact_sonarr_tvdb_episode_identity(self) -> None:
        linked = "/api/v3/Episode/30603/TMDB/Episode"
        transport = FakeTransport({
            ("GET", linked): [{
                "ID": 2060563, "ShowID": 93816, "TvdbEpisodeID": 7776520,
                "SeasonNumber": 1, "EpisodeNumber": 15,
            }],
        })

        exact = ShokoObserver(transport).has_exact_tmdb_episode_identity(
            30603, 93816, 7776520
        )

        self.assertTrue(exact)
        self.assertEqual(transport.requests, [("GET", linked, None)])

    def test_shoko_cross_order_coordinate_is_not_an_identity_mismatch(self) -> None:
        linked = "/api/v3/Episode/30603/TMDB/Episode"
        transport = FakeTransport({
            ("GET", linked): [{
                "ID": 2060563, "ShowID": 93816, "TvdbEpisodeID": 7776520,
                "SeasonNumber": 1, "EpisodeNumber": 15,
            }],
        })

        self.assertTrue(ShokoObserver(transport).has_exact_tmdb_episode_identity(
            30603, 93816, 7776520
        ))

    def test_shoko_distinguishes_missing_tvdb_identity_from_a_mismatch(self) -> None:
        linked = "/api/v3/Episode/31092/TMDB/Episode"
        payload = [{
            "ID": 7483745, "ShowID": 280049, "TvdbEpisodeID": None,
            "SeasonNumber": 2, "EpisodeNumber": 9,
        }]

        with self.assertRaises(MissingTvdbEpisodeIdentity):
            ShokoObserver(FakeTransport({("GET", linked): payload})).has_exact_tmdb_episode_identity(
                31092, 280049, 11895817
            )

    def test_shoko_movie_or_wrong_tvdb_episode_fails_closed(self) -> None:
        linked = "/api/v3/Episode/30810/TMDB/Episode"
        for payload in (
            [],
            [{"ID": 2060563, "ShowID": 84669, "TvdbEpisodeID": 9999999}],
            [{"ID": 2060563, "ShowID": 99999, "TvdbEpisodeID": 6917724}],
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ShokoObserver(FakeTransport({("GET", linked): payload})).has_exact_tmdb_episode_identity(
                    30810, 84669, 6917724
                )

    def test_plex_requires_exact_destination_path(self) -> None:
        observer = PlexObserver(FakeTransport())
        parts = observer.normalize_parts({"MediaContainer": {"Metadata": [
            {"guid": "com.plexapp.agents.shokorelay://1001/1/1?lang=en", "Media": [{"Part": [{"file": "/tv2/Group/Show/a.mkv", "accessible": True}]}]}
        ]}})
        self.assertTrue(observer.has_exact_playable_part(parts, "Group/Show/a.mkv", 1001))
        self.assertFalse(observer.has_exact_playable_part(parts, "Group/Show/b.mkv", 1001))
        self.assertFalse(observer.has_exact_playable_part(parts, "Group/Show/a.mkv", 9999))

    def test_plex_destination_scan_is_paginated_and_exact(self) -> None:
        first = "/library/sections/53/all?type=4&includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=500"
        second = "/library/sections/53/all?type=4&includeGuids=1&X-Plex-Container-Start=500&X-Plex-Container-Size=500"
        transport = FakeTransport({
            ("GET", first): {"MediaContainer": {"totalSize": 501, "size": 500, "Metadata": []}},
            ("GET", second): {"MediaContainer": {"totalSize": 501, "size": 1, "Metadata": [
                {"guid": "com.plexapp.agents.shokorelay://1001/1/1?lang=en", "Media": [{"Part": [{"file": "/tv2/Group/Show/a.mkv"}]}]},
            ]}},
        })
        observer = PlexObserver(transport)
        self.assertTrue(observer.has_exact_destination("Group/Show/a.mkv", 1001, (53,)))
        self.assertEqual([row[1] for row in transport.requests], [first, second])




if __name__ == "__main__":
    unittest.main()
