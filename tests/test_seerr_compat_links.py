"""Private compatibility-view checks; no Plex/Seerr or media writes."""

import tempfile
from copy import deepcopy
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from nagisa.seerr_compat_links import (
    LinkProofReader, NotRegularTVFile, OwnedPublishedCoordinate,
    SeerrEpisodeResolver)


class FakeShoko:
    def __init__(self, root, *, relative='/Show/episode.mkv', episode_id=99):
        self.root = root
        self.relative = relative
        self.episode_id = episode_id
        self.episode_type = 'Episode'
        self.deleted = False
        self.extra_locations = []
        self.tvdb_episode_id = 7398592
        self.tmdb_show_id = 94404
        self.tmdb_episode_id = 1948394
        self.tmdb_season = 2
        self.tmdb_episode = 3
        self.tmdb_hidden = False
        self.tmdb_duplicate = False
        self.tmdb_total_override = None
        self.extra_episode_rows = []

    def request_json(self, method, path):
        assert method == 'GET'
        if path == '/api/v3/File/42':
            if self.deleted:
                error = OSError('not found')
                error.code = 404
                raise error
            return {'ID': 42, 'Size': 7, 'IsIgnored': False,
                    'Locations': [{'FileID': 42, 'ManagedFolderID': 2,
                                   'RelativePath': self.relative,
                                   'IsAccessible': True},
                                  *self.extra_locations]}
        if path == '/api/v3/File/42/Episode?includeDataFrom=AniDB':
            return [{'IDs': {'ID': self.episode_id, 'ParentSeries': 1051,
                             'AniDB': 225738, 'TMDB': {
                                 'Show': [self.tmdb_show_id],
                                 'Episode': [self.tmdb_episode_id],
                                 'Movie': []}},
                     'AniDB': {'ID': 225738, 'Type': self.episode_type,
                               'EpisodeNumber': 5}},
                    *deepcopy(self.extra_episode_rows)]
        if path == '/api/v3/ManagedFolder/2':
            return {'ID': 2, 'Path': str(self.root) + '/',
                    'DropFolderType': 'Destination'}
        if path == '/api/v3/ManagedFolder/4':
            return {'ID': 4, 'Path': str(self.root.parent / 'Sonarr') + '/',
                    'DropFolderType': 'Destination'}
        if path == '/api/v3/ManagedFolder/1':
            return {'ID': 1, 'Path': str(self.root.parent / 'Import') + '/',
                    'DropFolderType': 'Source'}
        if path == '/api/v3/Tmdb/Show/94404/Episode?page=1&pageSize=0':
            row = {'ID': 1948394, 'SeasonNumber': self.tmdb_season,
                   'EpisodeNumber': self.tmdb_episode,
                   'IsHidden': self.tmdb_hidden,
                   'TvdbEpisodeID': self.tvdb_episode_id}
            listed = [row, dict(row)] if self.tmdb_duplicate else [row]
            return {'List': listed, 'Total': (self.tmdb_total_override
                    if self.tmdb_total_override is not None else len(listed))}
        if path == '/api/v3/Series/1051/Episode?page=1&pageSize=0&includeFiles=true':
            return {'Total': 1, 'List': [{'IDs': {
                'ID': 99, 'ParentSeries': 1051, 'AniDB': 225738},
                'Files': [{'ID': 42}, {'ID': 43}]}]}
        raise AssertionError(path)


class FakePlex:
    def __init__(self):
        self.tmdb_id = 94404
        self.tvdb_id = 370761

    def request_json(self, method, path):
        assert method == 'GET'
        key = path.split('/')[3].split('?')[0]
        section = 102 if key == '1002' else 103
        return {'MediaContainer': {'Metadata': [{
            'ratingKey': key, 'librarySectionID': section,
            'type': 'show', 'guid': 'tv.plex.agents.custom.shoko://show/1051',
            'Guid': [{'id': f'tmdb://{self.tmdb_id}'},
                     *([{'id': f'tvdb://{self.tvdb_id}'}]
                       if self.tvdb_id is not None else [])]}]}}


class FakeSeerr:
    def __init__(self):
        self.season_number = 1
        self.episode_number = 5
        self.episode_id = 7398592
        self.duplicate = False
        self.special_duplicate_coordinates = False
        self.anime_provider = 'tvdb'
        self.calls = []

    def request_json(self, method, path):
        assert method == 'GET'
        self.calls.append(path)
        if path == '/api/v1/settings/metadatas':
            return {'anime': self.anime_provider}
        if path == '/api/v1/tv/94404':
            seasons = [{'seasonNumber': self.season_number,
                        'episodeCount': 1}]
            if self.duplicate:
                seasons.append({'seasonNumber': 17, 'episodeCount': 1})
            if self.special_duplicate_coordinates:
                seasons.append({'seasonNumber': 0, 'episodeCount': 2})
            return {'id': 94404, 'externalIds': {'tvdbId': 370761},
                    'seasons': seasons}
        if path == f'/api/v1/tv/94404/season/{self.season_number}':
            return {'episodes': [{'id': self.episode_id,
                                  'seasonNumber': self.season_number,
                                  'episodeNumber': self.episode_number}]}
        if self.duplicate and path == '/api/v1/tv/94404/season/17':
            return {'episodes': [{'id': 7398592, 'seasonNumber': 17,
                                  'episodeNumber': 10}]}
        if (self.special_duplicate_coordinates and
                path == '/api/v1/tv/94404/season/0'):
            return {'episodes': [
                {'id': 880001, 'seasonNumber': 0, 'episodeNumber': 7},
                {'id': 880002, 'seasonNumber': 0, 'episodeNumber': 7}]}
        raise AssertionError(path)


def card(section, *, file_id=42, episode_id=99, season=1, episode=5,
         part_path=None, part_index=None):
    suffix = f'p{part_index}' if part_index is not None else ''
    return {'guid': f'tv.plex.agents.custom.shoko://episode/e{episode_id}{suffix}',
            'index': episode, 'parentIndex': season,
            'grandparentRatingKey': '1002' if section == 102 else '1003',
            'Media': [{'Part': [{
                'file': (str(part_path) if part_path else
                         f'/anime-native/!Relay/1051/Season 1/S01E05 [{file_id}].mkv'),
                'size': 7}]}]}


class LinkProofReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Anime'
        (self.root / 'Show').mkdir(parents=True)
        (self.root / 'Show' / 'episode.mkv').write_bytes(b'episode')
        self.shoko = FakeShoko(self.root)
        self.plex = FakePlex()
        self.seerr = FakeSeerr()
        self.cards = {102: [card(102)], 103: [card(103)]}
        samefile = patch(
            'nagisa.seerr_compat_links.os.path.samefile',
            side_effect=lambda part, source: (
                str(part) == '/anime-native/!Relay/1051/Season 1/S01E05 [42].mkv'
                and str(source) == str(self.root / 'Show' / 'episode.mkv')))
        samefile.start()
        self.addCleanup(samefile.stop)

    def reader(self, *, ignore_source=False, published_resolver=None,
               sections=(102, 103), sonarr_destination=False,
               coordinate_mode='tvdb'):
        allowed = {2: (self.root, '/source-pure/Anime')}
        if sonarr_destination:
            allowed[4] = (self.root.parent / 'Sonarr', '/sonarr-anime-native')
        return LinkProofReader(self.shoko,
                               lambda owner: {s: self.cards[s] for s in sections},
                               self.plex, self.seerr, sections=sections,
                               allowed_folders=allowed,
                               ignored_source_folders=(
                                   {1: self.root.parent / 'Import'}
                                   if ignore_source else None),
                               part_probe=lambda path: True,
                               published_resolver=published_resolver,
                               coordinate_mode=coordinate_mode)

    def test_native_tmdb_slot_works_with_tvdb_seerr_request_setting(self):
        self.shoko.tvdb_episode_id = None
        proof = self.reader(coordinate_mode='tmdb').read(42)
        self.assertEqual((proof.tmdb_show_id, proof.tmdb_episode_id,
                          proof.season, proof.episode),
                         (94404, 1948394, 2, 3))

    def test_tmdb_identity_accepts_one_exact_tmdb_id_without_tvdb_tag(self):
        self.plex.tvdb_id = None
        proof = self.reader(coordinate_mode='tmdb').read(42)
        self.assertEqual((proof.tmdb_show_id, proof.tvdb_show_id),
                         (94404, None))
        with self.assertRaisesRegex(ValueError, 'Plex external show ID'):
            self.reader(coordinate_mode='tvdb').read(42)

    def test_shoko_relay_variation_part_keeps_exact_file_ownership(self):
        variation = '/anime-native/!Relay/1051/Season 1/S01E05 [42][variation].mkv'
        self.cards = {section: [card(section, part_path=variation)]
                      for section in (102, 103)}
        source = str(self.root / 'Show' / 'episode.mkv')
        with patch('nagisa.seerr_compat_links.os.path.samefile',
                   side_effect=lambda part, candidate:
                   str(part) == variation and str(candidate) == source):
            proof = self.reader(coordinate_mode='tmdb').read(42)
        self.assertEqual((proof.shoko_file_id, proof.plex_target),
                         (42, '/source-pure/Anime/Show/episode.mkv'))

    def test_removed_shoko_file_with_variation_part_is_not_confirmed_gone(self):
        variation = '/anime-native/!Relay/1051/Season 1/S01E05 [42][variation].mkv'
        self.cards = {section: [card(section, part_path=variation)]
                      for section in (102, 103)}
        self.shoko.deleted = True
        self.assertFalse(self.reader(coordinate_mode='tmdb').confirm_removed(
            42, 1051))

    def test_native_tmdb_slot_holds_hidden_or_duplicate_provider_claim(self):
        self.shoko.tmdb_hidden = True
        with self.assertRaises(ValueError):
            self.reader(coordinate_mode='tmdb').read(42)
        self.shoko.tmdb_hidden = False
        self.shoko.tmdb_duplicate = True
        with self.assertRaises(ValueError):
            self.reader(coordinate_mode='tmdb').read(42)

    def test_native_mapping_change_can_be_confirmed_without_public_plex(self):
        reader = self.reader(coordinate_mode='tmdb')
        owned = {'file_id': 42, 'tmdb_show_id': 94404,
                 'tmdb_episode_id': 1948394, 'slot': '94404/2/3',
                 'series_id': 1051, 'anidb_episode_id': 225738}
        self.assertFalse(reader.confirm_native_mapping_changed(42, owned))
        self.shoko.tmdb_episode = 4
        self.assertTrue(reader.confirm_native_mapping_changed(42, owned))
        self.shoko.tmdb_episode = 3
        self.shoko.tmdb_hidden = True
        self.assertTrue(reader.confirm_native_mapping_changed(42, owned))

    def test_tvdb_order_confirms_changed_shoko_provider_without_public_plex(self):
        reader = self.reader(coordinate_mode='tvdb')
        owned = {'file_id': 42, 'tmdb_show_id': 94404,
                 'tmdb_episode_id': 1948394, 'slot': '370761/1/5',
                 'series_id': 1051, 'anidb_episode_id': 225738}
        self.assertFalse(reader.confirm_native_mapping_changed(42, owned))
        self.shoko.tmdb_show_id = 77694
        self.shoko.tmdb_episode_id = 4789321
        self.assertTrue(reader.confirm_native_mapping_changed(42, owned))

    def test_native_tmdb_reader_holds_incomplete_provider_page(self):
        self.shoko.tmdb_total_override = 2
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.reader(coordinate_mode='tmdb').read(42)

    def test_exact_file_creates_canonical_proof_without_title_guess(self):
        proof = self.reader().read(42)
        self.assertEqual((proof.tvdb_show_id, proof.tmdb_show_id,
                          proof.season, proof.episode, proof.shoko_file_id),
                         (370761, 94404, 1, 5, 42))
        self.assertEqual(proof.plex_target,
                         '/source-pure/Anime/Show/episode.mkv')

    def test_bounded_owner_snapshot_reuses_and_invalidates_source(self):
        calls = []

        def owner(series_id):
            calls.append(series_id)
            return deepcopy(self.cards)

        reader = LinkProofReader(
            self.shoko, owner, self.plex, self.seerr,
            sections=(102, 103),
            allowed_folders={2: (self.root, '/source-pure/Anime')},
            part_probe=lambda _: True, coordinate_mode='tvdb',
            owner_cache_size=128)
        reader.read(42)
        reader.read(42)
        self.assertEqual(calls, [1051])
        self.cards = {section: [card(section, file_id=43)]
                      for section in (102, 103)}
        reader.invalidate_owner_cache()
        with self.assertRaises(ValueError):
            reader.read(42)
        self.assertEqual(calls, [1051, 1051])

    def test_bounded_shoko_provider_snapshot_reuses_and_invalidates_source(self):
        calls = []
        original = self.shoko.request_json

        def request(method, path):
            if path == '/api/v3/Tmdb/Show/94404/Episode?page=1&pageSize=0':
                calls.append(path)
            return original(method, path)

        self.shoko.request_json = request
        reader = LinkProofReader(
            self.shoko, lambda _: deepcopy(self.cards), self.plex,
            self.seerr, sections=(102, 103),
            allowed_folders={2: (self.root, '/source-pure/Anime')},
            part_probe=lambda _: True, coordinate_mode='tvdb',
            provider_cache_size=128)
        reader.read(42)
        reader.read(42)
        self.assertEqual(len(calls), 1)
        self.shoko.tvdb_episode_id = 8888888
        reader.invalidate_owner_cache()
        with self.assertRaisesRegex(ValueError, 'absent'):
            reader.read(42)
        self.assertEqual(len(calls), 2)

    def test_shoko_provider_epoch_rejects_same_count_identity_change(self):
        reader = LinkProofReader(
            self.shoko, lambda _: deepcopy(self.cards), self.plex,
            self.seerr, sections=(102, 103),
            allowed_folders={2: (self.root, '/source-pure/Anime')},
            part_probe=lambda _: True, coordinate_mode='tvdb',
            provider_cache_size=128)
        reader.read(42)
        self.shoko.tvdb_episode_id = 8888888
        with self.assertRaisesRegex(ValueError, 'Shoko provider changed'):
            reader.revalidate_provider_snapshots()

    def test_cached_public_show_and_folder_identity_revalidates(self):
        shoko_calls = []
        plex_calls = []
        shoko_get = self.shoko.request_json
        plex_get = self.plex.request_json

        def counted_shoko(method, path):
            shoko_calls.append(path)
            return shoko_get(method, path)

        def counted_plex(method, path):
            plex_calls.append(path)
            return plex_get(method, path)

        self.shoko.request_json = counted_shoko
        self.plex.request_json = counted_plex
        reader = LinkProofReader(
            self.shoko, lambda _: deepcopy(self.cards), self.plex,
            self.seerr, sections=(102, 103),
            allowed_folders={2: (self.root, '/source-pure/Anime')},
            part_probe=lambda _: True, coordinate_mode='tvdb',
            owner_cache_size=128, provider_cache_size=128)
        reader.read(42)
        reader.read(42)
        self.assertEqual(shoko_calls.count('/api/v3/ManagedFolder/2'), 1)
        self.assertEqual(sum(path.startswith('/library/metadata/100')
                             for path in plex_calls), 2)
        self.plex.tmdb_id = 123456
        with self.assertRaisesRegex(ValueError, 'public Plex show changed'):
            reader.revalidate_provider_snapshots()

    def test_cached_managed_folder_revalidation_detects_changed_root(self):
        reader = LinkProofReader(
            self.shoko, lambda _: deepcopy(self.cards), self.plex,
            self.seerr, sections=(102, 103),
            allowed_folders={2: (self.root, '/source-pure/Anime')},
            part_probe=lambda _: True, coordinate_mode='tvdb',
            owner_cache_size=128, provider_cache_size=128)
        reader.read(42)
        self.shoko.root = self.root.parent / 'Different'
        with self.assertRaisesRegex(ValueError, 'managed-folder root changed'):
            reader.revalidate_provider_snapshots()

    def test_regular_file_with_one_attached_special_keeps_exact_regular_link(self):
        for special_episode_ids in ([], [1948395]):
            with self.subTest(special_episode_ids=special_episode_ids):
                self.shoko.extra_episode_rows = [{
                    'IDs': {'ID': 100, 'ParentSeries': 1051,
                            'AniDB': 225739, 'TMDB': {
                                'Show': [94404] if special_episode_ids else [],
                                'Episode': special_episode_ids, 'Movie': []}},
                    'AniDB': {'ID': 225739, 'Type': 'Special',
                              'EpisodeNumber': 1}}]
                proof = self.reader(coordinate_mode='tmdb').read(42)
                self.assertEqual((proof.shoko_file_id, proof.shoko_episode_id,
                                  proof.tmdb_episode_id, proof.season,
                                  proof.episode), (42, 99, 1948394, 2, 3))
                self.assertEqual(len(self.shoko.extra_episode_rows), 1)

    def test_tvdb_stage_projects_regular_despite_distinct_linked_special(self):
        self.shoko.extra_episode_rows = [{
            'IDs': {'ID': 100, 'ParentSeries': 1051,
                    'AniDB': 225739, 'TMDB': {
                        'Show': [94404], 'Episode': [1948395], 'Movie': []}},
            'AniDB': {'ID': 225739, 'Type': 'Special',
                      'EpisodeNumber': 1}}]

        proof = self.reader(coordinate_mode='tvdb').read(42)
        self.assertEqual((proof.shoko_episode_id, proof.tmdb_episode_id,
                          proof.tvdb_episode_id, proof.season, proof.episode),
                         (99, 1948394, 7398592, 1, 5))
        self.assertEqual((self.shoko.tmdb_season, self.shoko.tmdb_episode),
                         (2, 3))

        self.shoko.tvdb_episode_id = 8000000
        with self.assertRaisesRegex(ValueError, 'exact TVDB episode absent'):
            self.reader(coordinate_mode='tvdb').read(42)

    def test_attached_special_must_be_distinct_same_series_nonmovie(self):
        special = {'IDs': {'ID': 100, 'ParentSeries': 1051,
                           'AniDB': 225739, 'TMDB': {
                               'Show': [], 'Episode': [], 'Movie': []}},
                   'AniDB': {'ID': 225739, 'Type': 'Special',
                             'EpisodeNumber': 1}}
        for mutation in (
                lambda row: row['IDs'].update(ParentSeries=1052),
                lambda row: row['IDs'].update(ID=99),
                lambda row: row['AniDB'].update(Type='Episode'),
                lambda row: row['IDs']['TMDB'].update(Movie=[1418665])):
            with self.subTest(mutation=mutation):
                row = deepcopy(special)
                mutation(row)
                self.shoko.extra_episode_rows = [row]
                with self.assertRaisesRegex(ValueError, 'ambiguous'):
                    self.reader(coordinate_mode='tmdb').read(42)

    def test_attached_special_cannot_turn_a_public_special_card_into_tv(self):
        self.shoko.extra_episode_rows = [{
            'IDs': {'ID': 100, 'ParentSeries': 1051,
                    'AniDB': 225739, 'TMDB': {
                        'Show': [], 'Episode': [], 'Movie': []}},
            'AniDB': {'ID': 225739, 'Type': 'Special', 'EpisodeNumber': 1}}]
        self.cards = {section: [card(section, episode_id=100)]
                      for section in (102, 103)}
        with self.assertRaisesRegex(ValueError, 'Plex episode'):
            self.reader(coordinate_mode='tmdb').read(42)

    def test_relay_part_guid_requires_exact_native_episode_coordinate(self):
        self.cards = {section: [card(section, part_index=2)]
                      for section in (102, 103)}
        self.shoko.tmdb_season = 1
        self.shoko.tmdb_episode = 5
        proof = self.reader(coordinate_mode='tmdb').read(42)
        self.assertEqual((proof.shoko_episode_id, proof.tmdb_episode_id,
                          proof.season, proof.episode), (99, 1948394, 1, 5))

        self.shoko.tmdb_episode = 6
        with self.assertRaisesRegex(ValueError, 'part|coordinate'):
            self.reader(coordinate_mode='tmdb').read(42)
        self.shoko.tmdb_episode = 5
        with self.assertRaisesRegex(ValueError, 'Plex episode'):
            self.reader(coordinate_mode='tvdb').read(42)
        self.cards[103] = [card(103)]
        with self.assertRaisesRegex(ValueError, 'part|Plex episode|mirrored Plex identity'):
            self.reader(coordinate_mode='tmdb').read(42)

    def test_relay_part_card_with_an_alternate_file_selects_exact_part(self):
        self.shoko.tmdb_season = 1
        self.shoko.tmdb_episode = 5
        alternate = '/anime-native/!Relay/1051/Season 1/S01E05 [43].mkv'
        for section in (102, 103):
            episode_card = card(section, part_index=2)
            episode_card['Media'].append({'Part': [{
                'file': alternate, 'size': 11}]})
            self.cards[section] = [episode_card]

        proof = self.reader(coordinate_mode='tmdb').read(42)
        self.assertEqual((proof.shoko_file_id, proof.shoko_episode_id,
                          proof.plex_target),
                         (42, 99, '/source-pure/Anime/Show/episode.mkv'))

        self.cards[103][0]['Media'][1]['Part'][0]['file'] = (
            '/anime-native/!Relay/1051/Season 1/duplicate [42].mkv')
        with self.assertRaisesRegex(ValueError, 'Plex episode'):
            self.reader(coordinate_mode='tmdb').read(42)

    def test_real_anime_section_alone_proves_playable_file(self):
        proof = self.reader(sections=(102,)).read(42)
        self.assertEqual((proof.shoko_file_id, proof.tvdb_show_id),
                         (42, 370761))

    def test_two_managed_destinations_select_only_plex_physical_owner(self):
        sonarr = self.root.parent / 'Sonarr' / 'Show' / 'episode.mkv'
        sonarr.parent.mkdir(parents=True)
        sonarr.write_bytes(b'episode')
        self.shoko.extra_locations = [{
            'FileID': 42, 'ManagedFolderID': 4,
            'RelativePath': '/Show/episode.mkv', 'IsAccessible': True}]
        part = '/anime-native/!Relay/1051/Season 1/S01E05 [42].mkv'
        with patch('nagisa.seerr_compat_links.os.path.samefile',
                   side_effect=lambda candidate, source:
                   str(candidate) == part and str(source) == str(sonarr)):
            proof = self.reader(sonarr_destination=True).read(42)
        self.assertEqual(proof.plex_target,
                         '/sonarr-anime-native/Show/episode.mkv')

    def test_two_managed_destinations_disagreeing_between_sections_hold(self):
        sonarr = self.root.parent / 'Sonarr' / 'Show' / 'episode.mkv'
        sonarr.parent.mkdir(parents=True)
        sonarr.write_bytes(b'episode')
        self.shoko.extra_locations = [{
            'FileID': 42, 'ManagedFolderID': 4,
            'RelativePath': '/Show/episode.mkv', 'IsAccessible': True}]
        left = '/anime-native/!Relay/1051/Season 1/S01E05 [42].mkv'
        right = '/anime-native/other/episode [42].mkv'
        self.cards[103] = [card(103, part_path=right)]
        def points_to_different_source(candidate, source):
            return ((str(candidate) == left and str(source) == str(sonarr))
                    or (str(candidate) == right
                        and str(source) == str(self.root / 'Show' / 'episode.mkv')))
        with patch('nagisa.seerr_compat_links.os.path.samefile',
                   side_effect=points_to_different_source):
            with self.assertRaisesRegex(ValueError, 'physical|destination'):
                self.reader(sonarr_destination=True).read(42)

    def test_two_destinations_matching_same_plex_part_are_ambiguous(self):
        sonarr = self.root.parent / 'Sonarr' / 'Show' / 'episode.mkv'
        sonarr.parent.mkdir(parents=True)
        sonarr.write_bytes(b'episode')
        self.shoko.extra_locations = [{
            'FileID': 42, 'ManagedFolderID': 4,
            'RelativePath': '/Show/episode.mkv', 'IsAccessible': True}]
        with patch('nagisa.seerr_compat_links.os.path.samefile',
                   return_value=True):
            with self.assertRaisesRegex(ValueError, 'multiple.*destinations'):
                self.reader(sonarr_destination=True).read(42)

    def test_removed_file_can_find_same_episode_alternate_only(self):
        self.assertEqual(self.reader().alternate_file_ids(
            1051, 225738, exclude_file_id=42), (43,))
        self.assertEqual(self.reader().alternate_file_ids(
            1051, 999999, exclude_file_id=42), ())

    def test_missing_mirror_or_conflicting_episode_is_held(self):
        self.cards[103] = []
        with self.assertRaises(ValueError):
            self.reader().read(42)
        self.cards[103] = [card(103, episode=6)]
        with self.assertRaises(ValueError):
            self.reader().read(42)

    def test_location_escape_is_held_before_link_creation(self):
        self.shoko.relative = '/../outside.mkv'
        with self.assertRaises(ValueError):
            self.reader().read(42)

    def test_conflicting_provider_show_id_is_held(self):
        self.plex.tmdb_id = 99999
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.reader().read(42)

    def test_unreadable_plex_part_is_held(self):
        self.cards[102] = [card(102, file_id=43)]
        with self.assertRaises(ValueError):
            self.reader().read(42)

    def test_same_id_and_size_at_different_physical_file_is_held(self):
        alternate = Path('/anime-native/other/alternate [42].mkv')
        self.cards[103] = [card(103, part_path=alternate)]
        with self.assertRaisesRegex(ValueError, 'physical file'):
            self.reader().read(42)

    def test_exact_tvdb_id_can_have_different_seerr_season(self):
        self.seerr.season_number = 17
        self.seerr.episode_number = 10
        proof = self.reader().read(42)
        self.assertEqual((proof.season, proof.episode), (17, 10))

    def test_missing_native_crosswalk_uses_only_exact_published_proof(self):
        self.shoko.tvdb_episode_id = None
        with self.assertRaisesRegex(ValueError, 'TVDB episode identity'):
            self.reader().read(42)
        calls = []
        def published(*, file_id, tmdb_episode_id, plex_target,
                      tmdb_show_id, tvdb_show_id, shoko_series_id,
                      anidb_episode_id):
            calls.append((file_id, tmdb_episode_id, plex_target,
                          tmdb_show_id, tvdb_show_id, shoko_series_id,
                          anidb_episode_id))
            return 1, 5
        proof = self.reader(published_resolver=published).read(42)
        self.assertEqual(calls, [(42, 1948394, '/source-pure/Anime/Show/episode.mkv',
                                  94404, 370761, 1051, 225738)])
        self.assertEqual((proof.tvdb_episode_id, proof.season, proof.episode),
                         (None, 1, 5))

    def test_no_id_fallback_requires_same_ready_owned_link(self):
        row = {'file_id': 42, 'series_id': 1051,
               'anidb_episode_id': 225738, 'tmdb_episode_id': 1948394,
               'tmdb_show_id': 94404, 'tvdb_episode_id': None,
               'slot': '94404/1/5', 'state': 'ready',
               'target': '/source-pure/Anime/Show/episode.mkv',
               'link': 'tmdb-94404/Season 01/S01E05.mkv'}
        store = SimpleNamespace(identity_provider='tmdb', root=self.root.parent,
                                lookup_file=lambda file_id: row)
        resolver = OwnedPublishedCoordinate(store)
        arguments = {'file_id': 42, 'tmdb_episode_id': 1948394,
                     'plex_target': row['target'], 'tmdb_show_id': 94404,
                     'tvdb_show_id': 370761, 'shoko_series_id': 1051,
                     'anidb_episode_id': 225738}
        with patch.object(Path, 'is_symlink', return_value=True), patch(
                'nagisa.seerr_compat_links.os.readlink', return_value=row['target']):
            self.assertEqual(resolver(**arguments), (1, 5))
            self.shoko.tvdb_episode_id = None
            proof = self.reader(published_resolver=resolver).read(42)
            self.assertEqual((proof.tvdb_episode_id, proof.season, proof.episode),
                             (None, 1, 5))
            for key, bad in [('file_id', 43), ('tmdb_episode_id', 1948395),
                             ('plex_target', '/source-pure/other.mkv'),
                             ('shoko_series_id', 1052),
                             ('anidb_episode_id', 225739)]:
                changed = dict(arguments, **{key: bad})
                self.assertIsNone(resolver(**changed))
            row['tvdb_episode_id'] = 111
            self.assertIsNone(resolver(**arguments))
            row['tvdb_episode_id'] = None
            row['state'] = 'pending'
            self.assertIsNone(resolver(**arguments))

    def test_malformed_crosswalk_cannot_be_treated_as_missing(self):
        self.shoko.tvdb_episode_id = 'bad-id'
        with self.assertRaisesRegex(ValueError, 'malformed TVDB cross-reference'):
            self.reader(published_resolver=lambda **_: (1, 5)).read(42)

    def test_published_coordinate_conflicting_with_seerr_is_held(self):
        self.shoko.tvdb_episode_id = None
        with self.assertRaisesRegex(ValueError, 'published.*Seerr'):
            self.reader(published_resolver=lambda **_:
                        (1, 6)).read(42)

    def test_exact_native_id_can_move_from_old_published_slot(self):
        calls = []
        def stale_published(**kwargs):
            calls.append(kwargs)
            return 1, 9
        proof = self.reader(published_resolver=stale_published).read(42)
        self.assertEqual((proof.season, proof.episode), (1, 5))
        self.assertEqual(calls, [])

    def test_supplied_stale_id_does_not_downgrade_to_old_slot_fallback(self):
        self.shoko.tvdb_episode_id = 8261901
        self.seerr.episode_id = 8515643
        calls = []
        def old_published(**kwargs):
            calls.append(kwargs)
            return 1, 5
        with self.assertRaisesRegex(ValueError, 'exact TVDB episode absent'):
            self.reader(published_resolver=old_published).read(42)
        self.assertEqual(calls, [])

    def test_unique_native_crosswalk_survives_unavailable_published_coordinate(self):
        proof = self.reader(published_resolver=lambda **_: None).read(42)
        self.assertEqual((proof.season, proof.episode), (1, 5))
        self.shoko.tvdb_episode_id = None
        with self.assertRaisesRegex(ValueError, 'TVDB episode identity'):
            self.reader(published_resolver=lambda **_: None).read(42)

    def test_duplicate_tvdb_episode_in_seerr_is_held(self):
        self.seerr.duplicate = True
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.reader().read(42)

    def test_duplicate_special_numbers_do_not_hide_unique_regular_episode(self):
        self.seerr.special_duplicate_coordinates = True
        proof = self.reader().read(42)
        self.assertEqual((proof.season, proof.episode), (1, 5))

    def test_regular_link_does_not_use_ambiguous_special_coordinate(self):
        self.seerr.special_duplicate_coordinates = True
        with self.assertRaisesRegex(ValueError, 'regular'):
            SeerrEpisodeResolver(self.seerr).resolve(94404, 370761, 880001)

    def test_provider_snapshot_reuses_one_complete_show_fetch(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
        self.assertEqual(self.seerr.calls.count('/api/v1/settings/metadatas'), 1)
        self.assertEqual(self.seerr.calls.count('/api/v1/tv/94404'), 1)
        self.assertEqual(self.seerr.calls.count('/api/v1/tv/94404/season/1'), 1)

    def test_published_coordinate_reuses_complete_show_snapshot(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        for _ in range(20):
            resolver.require_coordinate(94404, 370761, 1, 5)
        self.assertEqual(len(self.seerr.calls), 3)

    def test_published_regular_coordinate_cannot_be_special(self):
        self.seerr.season_number = 0
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        with self.assertRaisesRegex(ValueError, 'regular'):
            resolver.require_coordinate(94404, 370761, 0, 5)

    def test_same_count_provider_identity_change_invalidates_snapshot(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
        self.seerr.episode_id = 8888888
        with self.assertRaisesRegex(ValueError, 'catalogue changed'):
            resolver.revalidate_show(94404, 370761)
        with self.assertRaisesRegex(ValueError, 'absent'):
            resolver.resolve(94404, 370761, 7398592)

    def test_provider_switch_invalidates_old_snapshot(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
        self.seerr.anime_provider = 'tmdb'
        with self.assertRaisesRegex(ValueError, 'provider changed'):
            resolver.revalidate_show(94404, 370761)
        with self.assertRaisesRegex(ValueError, 'provider changed'):
            resolver.resolve(94404, 370761, 7398592)

    def test_two_show_snapshots_have_bounded_independent_requests(self):
        class TwoShows:
            def __init__(self, first):
                self.first = first
                self.calls = []

            def request_json(self, method, path):
                self.calls.append(path)
                if path in ('/api/v1/settings/metadatas',
                            '/api/v1/tv/94404',
                            '/api/v1/tv/94404/season/1'):
                    return self.first.request_json(method, path)
                if path == '/api/v1/tv/200':
                    return {'id': 200, 'externalIds': {'tvdbId': 300},
                            'seasons': [{'seasonNumber': 1, 'episodeCount': 1}]}
                if path == '/api/v1/tv/200/season/1':
                    return {'episodes': [{'id': 400, 'seasonNumber': 1,
                                          'episodeNumber': 1}]}
                raise AssertionError(path)

        two = TwoShows(self.seerr)
        resolver = SeerrEpisodeResolver(two, cache_seconds=3600)
        for _ in range(20):
            self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
            self.assertEqual(resolver.resolve(200, 300, 400), (1, 1))
        self.assertEqual(len(two.calls), 6)

    def test_interrupted_refresh_does_not_reuse_old_snapshot(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))
        original = self.seerr.request_json

        def interrupted(method, path):
            if path == '/api/v1/tv/94404/season/1':
                raise OSError('provider interrupted')
            return original(method, path)

        self.seerr.request_json = interrupted
        with self.assertRaisesRegex(OSError, 'interrupted'):
            resolver.revalidate_show(94404, 370761)
        with self.assertRaisesRegex(OSError, 'interrupted'):
            resolver.resolve(94404, 370761, 7398592)
        self.seerr.request_json = original
        self.assertEqual(resolver.resolve(94404, 370761, 7398592), (1, 5))

    def test_revalidation_without_prior_snapshot_cannot_pass(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        with self.assertRaisesRegex(ValueError, 'snapshot absent'):
            resolver.revalidate_show(94404, 370761)

    def test_saved_fingerprint_detects_change_after_cache_eviction(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        resolver.resolve(94404, 370761, 7398592)
        fingerprint = resolver.snapshot_fingerprint(94404, 370761)
        resolver._cache.clear()  # A long export can evict an early show.
        self.seerr.episode_id = 8888888
        with self.assertRaisesRegex(ValueError, 'catalogue changed'):
            resolver.revalidate_show(
                94404, 370761, expected_fingerprint=fingerprint)

    def test_saved_fingerprint_detects_same_id_coordinate_move(self):
        resolver = SeerrEpisodeResolver(self.seerr, cache_seconds=3600)
        resolver.resolve(94404, 370761, 7398592)
        fingerprint = resolver.snapshot_fingerprint(94404, 370761)
        self.seerr.episode_number = 6
        with self.assertRaisesRegex(ValueError, 'catalogue changed'):
            resolver.revalidate_show(
                94404, 370761, expected_fingerprint=fingerprint)

    def test_removal_needs_shoko_404_and_both_plex_sections_absent(self):
        self.assertFalse(self.reader().confirm_removed(42, 1051))
        self.shoko.deleted = True
        self.assertFalse(self.reader().confirm_removed(42, 1051))
        self.cards[102] = []
        self.cards[103] = []
        self.assertTrue(self.reader().confirm_removed(42, 1051))

    def test_positive_special_identity_is_excluded_from_tv_view(self):
        self.shoko.episode_type = 'Special'
        with self.assertRaises(NotRegularTVFile) as excluded:
            self.reader().read(42)
        self.assertEqual(excluded.exception.kind, 'Special')

    def test_movie_identity_is_distinct_from_special_for_retry(self):
        self.shoko.episode_type = 'Movie'
        with self.assertRaises(NotRegularTVFile) as excluded:
            self.reader().read(42)
        self.assertEqual(excluded.exception.kind, 'Movie')

    def test_multi_episode_movie_file_reaches_movie_handoff(self):
        original = self.shoko.request_json

        def multi(method, path):
            rows = original(method, path)
            if path != '/api/v3/File/42/Episode?includeDataFrom=AniDB':
                return rows
            first = rows[0]
            first['IDs']['TMDB']['Movie'] = [1418665]
            second = deepcopy(first)
            second['IDs']['ID'] = 100
            second['IDs']['AniDB'] = 225739
            second['AniDB']['ID'] = 225739
            return [first, second]

        self.shoko.request_json = multi
        with self.assertRaises(NotRegularTVFile) as excluded:
            self.reader().read(42)
        self.assertEqual(excluded.exception.kind, 'Movie')

    def test_one_managed_destination_with_old_import_copy_is_usable(self):
        old = self.root.parent / 'Import' / 'Show'
        old.mkdir(parents=True)
        (old / 'episode.mkv').write_bytes(b'episode')
        self.shoko.extra_locations = [{
            'FileID': 42, 'ManagedFolderID': 1,
            'RelativePath': '/Show/episode.mkv', 'IsAccessible': True}]
        proof = self.reader(ignore_source=True).read(42)
        self.assertEqual(proof.plex_target,
                         '/source-pure/Anime/Show/episode.mkv')
        with self.assertRaises(ValueError):
            self.reader().read(42)

    def test_public_special_card_can_map_by_exact_id_to_seerr_coordinate(self):
        self.cards[102] = [card(102, season=0)]
        self.cards[103] = [card(103, season=0)]
        proof = self.reader().read(42)
        self.assertEqual((proof.season, proof.episode), (1, 5))


if __name__ == '__main__':
    unittest.main()
