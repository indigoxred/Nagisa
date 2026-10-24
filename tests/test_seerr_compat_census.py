"""Bounded initial exact-ID read, without links or media changes."""

import unittest

from nagisa.seerr_compat_census import ShokoInitialCensus


class Pages:
    def __init__(self):
        self.ids = [69, 70, 72, 80, 81]
        self.changed = False
        self.head_reads = 0

    def request_json(self, method, path):
        assert method == 'GET'
        page = int(path.split('page=')[1].split('&')[0])
        size = int(path.split('pageSize=')[1].split('&')[0])
        ids = self.ids.copy()
        if page == 1:
            self.head_reads += 1
            if self.changed and self.head_reads > 1:
                ids[0] = 68
        return {'Total': len(ids),
                'List': [{'ID': item} for item in
                         ids[(page - 1) * size:page * size]]}


class ShokoInitialCensusTests(unittest.TestCase):
    def test_exact_ascending_ids_and_stable_head(self):
        self.assertEqual(ShokoInitialCensus(Pages(), page_size=2).read_ids(),
                         (69, 70, 72, 80, 81))

    def test_changed_page_holds_entire_initial_export(self):
        pages = Pages()
        pages.changed = True
        with self.assertRaises(ValueError):
            ShokoInitialCensus(pages, page_size=2).read_ids()


if __name__ == '__main__':
    unittest.main()
