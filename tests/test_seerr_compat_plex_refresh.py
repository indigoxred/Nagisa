"""A scan request is not a proof that Plex indexed the owned links."""

import unittest

from nagisa.seerr_compat_plex_refresh import VerifiedShowRefresh


class FakePlex:
    def __init__(self):
        self.files = []
        self.show_count = 1
        self.calls = []

    def request_json(self, method, path):
        self.calls.append((method, path))
        if '/sections/107/all?type=3' in path:
            if 'season.trash=' in path:
                raise AssertionError('use the complete stock season listing')
            return {'MediaContainer': {'size': 2, 'totalSize': 2,
                'Metadata': [
                    {'type': 'season', 'ratingKey': '79',
                     'parentRatingKey': '77', 'deletedAt': 123},
                    {'type': 'season', 'ratingKey': '80',
                     'parentRatingKey': '88'}]}}
        if '/sections/107/all?' in path:
            shows = [{'ratingKey': str(77 + i), 'type': 'show',
                      'Guid': [{'id': 'tvdb://111'}]}
                     for i in range(self.show_count)]
            return {'MediaContainer': {'size': len(shows),
                                        'totalSize': len(shows),
                                        'Metadata': shows}}
        if '/metadata/77/allLeaves?' in path:
            episodes = [{'type': 'episode', 'ratingKey': str(i + 1),
                         'Media': [{'Part': [{'file': path}]}]}
                        for i, path in enumerate(self.files)]
            return {'MediaContainer': {'size': len(episodes),
                                        'totalSize': len(episodes),
                                        'Metadata': episodes}}
        raise AssertionError(path)


class FakeStore:
    def show_links(self, show_id):
        assert show_id == 111
        return frozenset({'tvdb-111/Season 01/S01E01.mkv'})


class VerifiedShowRefreshTests(unittest.TestCase):
    def setUp(self):
        self.plex = FakePlex()
        self.posts = []
        self.refresh = VerifiedShowRefresh(
            self.plex, FakeStore(), 107, '/Compat',
            lambda: None,
            lambda path: self.posts.append(path))

    def test_accepted_scan_keeps_intent_until_exact_part_is_indexed(self):
        self.assertFalse(self.refresh(111))
        self.assertEqual(self.posts, [
            '/library/sections/107/refresh?path=/Compat/tvdb-111'])
        self.plex.files = ['/Compat/tvdb-111/Season 01/S01E01.mkv']
        self.assertTrue(self.refresh(111))
        self.assertEqual(len(self.posts), 1)

    def test_tmdb_refresh_checks_tmdb_tag_and_tmdb_folder(self):
        class TmdbPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = [
                        {'id': 'tmdb://111'}]
                return response

        self.plex = TmdbPlex()
        self.refresh = VerifiedShowRefresh(
            self.plex,
            type('Store', (), {'show_links': lambda _, show_id:
                 frozenset({'tmdb-111/Season 01/S01E01.mkv'})})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path),
            identity_provider='tmdb')
        self.assertFalse(self.refresh(111))
        self.assertEqual(self.posts, [
            '/library/sections/107/refresh?path=/Compat/tmdb-111'])
        self.plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv']
        self.assertTrue(self.refresh(111))

    def test_tmdb_tagless_exact_card_gets_item_metadata_refresh(self):
        class TaglessPlex(FakePlex):
            tagged = False

            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = (
                        [{'id': 'tmdb://111'}] if self.tagged else [])
                return response

        plex = TaglessPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv']
        refreshed_items = []
        refresh = VerifiedShowRefresh(
            plex,
            type('Store', (), {'show_links': lambda _, show_id:
                 frozenset({'tmdb-111/Season 01/S01E01.mkv'})})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path),
            identity_provider='tmdb',
            refresh_item=lambda key: refreshed_items.append(key))
        self.assertFalse(refresh(111))
        self.assertEqual(refreshed_items, ['77'])
        self.assertEqual(self.posts, [])
        plex.tagged = True
        self.assertTrue(refresh(111))

    def test_tmdb_wrong_identity_card_is_never_item_refreshed(self):
        class WrongIdentityPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = [
                        {'id': 'tmdb://999'}]
                return response

        plex = WrongIdentityPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv']
        refreshed_items = []
        refresh = VerifiedShowRefresh(
            plex,
            type('Store', (), {'show_links': lambda _, show_id:
                 frozenset({'tmdb-111/Season 01/S01E01.mkv'})})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path),
            identity_provider='tmdb',
            refresh_item=lambda key: refreshed_items.append(key))
        self.assertFalse(refresh(111))
        self.assertEqual(refreshed_items, [])

    def test_tmdb_dual_identity_card_cannot_ack_exact_part(self):
        class DualIdentityPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = [
                        {'id': 'tmdb://111'}, {'id': 'tmdb://999'}]
                return response

        plex = DualIdentityPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv']
        refresh = VerifiedShowRefresh(
            plex,
            type('Store', (), {'show_links': lambda _, show_id:
                 frozenset({'tmdb-111/Season 01/S01E01.mkv'})})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path),
            identity_provider='tmdb', refresh_item=lambda key: None)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            refresh(111)
        self.assertEqual(self.posts, [])

    def test_tmdb_dual_card_quarantines_only_complete_owned_part_union(self):
        class DualPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?type=2' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = [
                        {'id': 'tmdb://111'}, {'id': 'tmdb://999'}]
                return response

        class OwnedStore:
            quarantines = []

            def show_links(self, show):
                return frozenset({f'tmdb-{show}/Season 01/S01E01.mkv'})

            def quarantine_card(self, shows, links):
                self.quarantines.append((shows, links))
                return (11, 99)

        plex = DualPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv',
                      '/Compat/tmdb-999/Season 01/S01E01.mkv']
        store = OwnedStore()
        refresh = VerifiedShowRefresh(
            plex, store, 107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb')
        with self.assertLogs('nagisa.seerr_compat_plex_refresh',
                             level='WARNING') as captured:
            self.assertFalse(refresh(111))
        self.assertIn('tmdb_ids=(111, 999)', captured.output[0])
        self.assertIn('file_ids=(11, 99)', captured.output[0])
        self.assertEqual(store.quarantines, [
            ((111, 999), frozenset({
                'tmdb-111/Season 01/S01E01.mkv',
                'tmdb-999/Season 01/S01E01.mkv'}))])
        self.assertEqual(self.posts, [
            '/library/sections/107/refresh?path=/Compat/tmdb-111',
            '/library/sections/107/refresh?path=/Compat/tmdb-999'])

    def test_tmdb_dual_card_with_foreign_part_cannot_quarantine(self):
        class DualPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?type=2' in path:
                    response['MediaContainer']['Metadata'][0]['Guid'] = [
                        {'id': 'tmdb://111'}, {'id': 'tmdb://999'}]
                return response

        class OwnedStore:
            def show_links(self, show):
                return frozenset({f'tmdb-{show}/Season 01/S01E01.mkv'})

            def quarantine_card(self, shows, links):
                raise AssertionError('foreign Part must never quarantine')

        plex = DualPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv',
                      '/foreign/episode.mkv']
        refresh = VerifiedShowRefresh(
            plex, OwnedStore(), 107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            refresh(111)
        self.assertEqual(self.posts, [])

    def test_stale_or_extra_episode_does_not_complete(self):
        self.plex.files = ['/Compat/tvdb-111/Season 01/S01E02.mkv']
        self.assertFalse(self.refresh(111))
        self.assertEqual(len(self.posts), 1)

    def test_ambiguous_show_identity_refuses_refresh(self):
        self.plex.show_count = 2
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.refresh(111)
        self.assertEqual(self.posts, [])

    def test_incomplete_show_listing_refuses_refresh(self):
        class TruncatedPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['totalSize'] = 2
                    if 'X-Plex-Container-Start=1' in path:
                        response['MediaContainer']['size'] = 0
                        response['MediaContainer']['Metadata'] = []
                return response

        self.refresh.plex = TruncatedPlex()
        with self.assertRaisesRegex(ValueError, 'listing'):
            self.refresh(111)
        self.assertEqual(self.posts, [])

    def test_no_owned_link_and_no_plex_show_is_confirmed_removed(self):
        self.refresh.store.show_links = lambda _: frozenset()
        self.plex.show_count = 0
        self.assertTrue(self.refresh(111))
        self.assertEqual(self.posts, [])

    def test_no_owned_link_with_empty_plex_show_still_needs_scan(self):
        self.refresh.store.show_links = lambda _: frozenset()
        self.plex.files = []
        self.assertFalse(self.refresh(111))
        self.assertEqual(self.posts, [
            '/library/sections/107/refresh?path=/Compat/tvdb-111'])

    def test_tmdb_whole_title_with_exact_private_trash_calls_scoped_clear(self):
        class TrashedPlex(FakePlex):
            trashed = True

            def request_json(self, method, path):
                if '/sections/107/all?type=3' in path:
                    if 'season.trash=' in path:
                        raise AssertionError('use the complete stock season listing')
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'season', 'ratingKey': '79',
                            'parentRatingKey': '77', 'deletedAt': 123}]}}
                if 'episode.trash=1' in path:
                    if not self.trashed:
                        return {'MediaContainer': {'size': 0, 'totalSize': 0,
                                                    'Metadata': []}}
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?' in path and self.trashed:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        cleared = []
        plex = TrashedPlex()
        refresh = VerifiedShowRefresh(
            plex,
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        self.assertFalse(refresh(111))
        self.assertEqual(cleared, [107])
        self.assertEqual(self.posts, [])
        plex.trashed = False
        plex.show_count = 0
        self.assertTrue(refresh(111))
        self.assertEqual(cleared, [107])

    def test_quarantined_dual_card_with_exact_union_can_clear_private_trash(self):
        class TrashedDualPlex(FakePlex):
            def request_json(self, method, path):
                if '/sections/107/all?type=3' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'season', 'ratingKey': '79',
                            'parentRatingKey': '77', 'deletedAt': 123}]}}
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 2, 'totalSize': 2,
                        'Metadata': [
                            {'type': 'episode', 'ratingKey': '81',
                             'grandparentRatingKey': '77', 'deletedAt': 123,
                             'Media': [{'Part': [{'file':
                                 '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]},
                            {'type': 'episode', 'ratingKey': '82',
                             'grandparentRatingKey': '77', 'deletedAt': 123,
                             'Media': [{'Part': [{'file':
                                 '/Compat/tmdb-999/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?type=2' in path:
                    response['MediaContainer']['Metadata'][0].update({
                        'Guid': [{'id': 'tmdb://111'}, {'id': 'tmdb://999'}],
                        'deletedAt': 123})
                return response

        class QuarantinedStore:
            def show_links(self, show):
                return frozenset()

            def quarantined_show_ids(self, shows):
                return shows == (111, 999)

            def owned_file_ids_for_shows(self, shows):
                return ()

        plex = TrashedDualPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E01.mkv',
                      '/Compat/tmdb-999/Season 01/S01E01.mkv']
        cleared = []
        refresh = VerifiedShowRefresh(
            plex, QuarantinedStore(), 107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        self.assertFalse(refresh(111))
        self.assertEqual(cleared, [107])

    def test_tmdb_private_clear_holds_if_foreign_trash_appears_before_put(self):
        class ChangingTrashPlex(FakePlex):
            show_reads = 0

            def request_json(self, method, path):
                if '/sections/107/all?type=3' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'season', 'ratingKey': '79',
                            'parentRatingKey': '77', 'deletedAt': 123}]}}
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?type=2' in path:
                    self.show_reads += 1
                    rows = response['MediaContainer']['Metadata']
                    rows[0].update({'Guid': [{'id': 'tmdb://111'}],
                                    'deletedAt': 123})
                    if self.show_reads > 1:
                        rows.append({'type': 'show', 'ratingKey': '88',
                                     'Guid': [{'id': 'tmdb://222'}],
                                     'deletedAt': 123})
                    response['MediaContainer']['size'] = len(rows)
                    response['MediaContainer']['totalSize'] = len(rows)
                return response

        cleared = []
        plex = ChangingTrashPlex()
        refresh = VerifiedShowRefresh(
            plex,
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'changed before clear'):
            refresh(111)
        self.assertGreaterEqual(plex.show_reads, 2)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_if_owned_claim_appears_before_put(self):
        class TrashedPlex(FakePlex):
            def request_json(self, method, path):
                if '/sections/107/all?type=3' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'season', 'ratingKey': '79',
                            'parentRatingKey': '77', 'deletedAt': 123}]}}
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?type=2' in path:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        class NewClaimStore:
            checks = 0

            def show_links(self, show):
                return frozenset()

            def owned_file_ids_for_shows(self, shows):
                self.checks += 1
                return () if self.checks == 1 else (123,)

        store = NewClaimStore()
        cleared = []
        refresh = VerifiedShowRefresh(
            TrashedPlex(), store, 107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'claim changed before clear'):
            refresh(111)
        self.assertEqual(store.checks, 2)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_foreign_trashed_season(self):
        class ForeignSeasonPlex(FakePlex):
            def request_json(self, method, path):
                if '/sections/107/all?type=3' in path:
                    if 'season.trash=' in path:
                        raise AssertionError('use the complete stock season listing')
                    return {'MediaContainer': {'size': 2, 'totalSize': 2,
                        'Metadata': [
                            {'type': 'season', 'ratingKey': '79',
                             'parentRatingKey': '77', 'deletedAt': 123},
                            {'type': 'season', 'ratingKey': '99',
                             'parentRatingKey': '88', 'deletedAt': 123}]}}
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        cleared = []
        refresh = VerifiedShowRefresh(
            ForeignSeasonPlex(),
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'unrelated season'):
            refresh(111)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_when_another_show_is_in_trash(self):
        class OtherShowPlex(FakePlex):
            def request_json(self, method, path):
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'] = [
                        {'ratingKey': '77', 'type': 'show', 'deletedAt': 123,
                         'Guid': [{'id': 'tmdb://111'}]},
                        {'ratingKey': '88', 'type': 'show', 'deletedAt': 123,
                         'Guid': [{'id': 'tmdb://222'}]}]
                    response['MediaContainer']['size'] = 2
                    response['MediaContainer']['totalSize'] = 2
                return response

        cleared = []
        refresh = VerifiedShowRefresh(
            OtherShowPlex(),
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'another show'):
            refresh(111)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_foreign_trashed_episode(self):
        class ForeignEpisodePlex(FakePlex):
            def request_json(self, method, path):
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '90',
                            'grandparentRatingKey': '88', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-222/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        cleared = []
        refresh = VerifiedShowRefresh(
            ForeignEpisodePlex(),
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'unrelated episode'):
            refresh(111)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_live_part_on_trashed_card(self):
        class LivePartPlex(FakePlex):
            def request_json(self, method, path):
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        plex = LivePartPlex()
        plex.files = ['/Compat/tmdb-111/Season 01/S01E02.mkv']
        cleared = []
        refresh = VerifiedShowRefresh(
            plex,
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            refresh(111)
        self.assertEqual(cleared, [])

    def test_tmdb_private_clear_holds_pending_owned_claim(self):
        class TrashedPlex(FakePlex):
            def request_json(self, method, path):
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                response = super().request_json(method, path)
                if '/sections/107/all?' in path:
                    response['MediaContainer']['Metadata'][0].update(
                        {'Guid': [{'id': 'tmdb://111'}], 'deletedAt': 123})
                return response

        cleared = []
        refresh = VerifiedShowRefresh(
            TrashedPlex(),
            type('PendingStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: (15135,)})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'owned claim'):
            refresh(111)
        self.assertEqual(cleared, [])

    def test_tmdb_unmatched_trashed_part_cannot_ack_removed(self):
        class WrongIdentityPlex(FakePlex):
            def __init__(self):
                super().__init__()
                self.show_count = 0

            def request_json(self, method, path):
                if 'episode.trash=1' in path:
                    return {'MediaContainer': {'size': 1, 'totalSize': 1,
                        'Metadata': [{'type': 'episode', 'ratingKey': '78',
                            'grandparentRatingKey': '77', 'deletedAt': 123,
                            'Media': [{'Part': [{'file':
                                '/Compat/tmdb-111/Season 01/S01E01.mkv'}]}]}]}}
                return super().request_json(method, path)

        cleared = []
        refresh = VerifiedShowRefresh(
            WrongIdentityPlex(),
            type('EmptyStore', (), {
                'show_links': lambda _, show: frozenset(),
                'owned_file_ids_for_shows': lambda _, shows: ()})(),
            107, '/Compat', lambda: None,
            lambda path: self.posts.append(path), identity_provider='tmdb',
            empty_trash=lambda section: cleared.append(section))
        with self.assertRaisesRegex(ValueError, 'unmatched'):
            refresh(111)
        self.assertEqual(cleared, [])


if __name__ == '__main__':
    unittest.main()
