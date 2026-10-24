import unittest
from copy import deepcopy
import arrival_identity_fixture as identity_fixture
import arrival_visibility_fixture as visibility_fixture
from nagisa.reconciler.arrival_readers import StockArrivalIdentityReader, StockArrivalVisibilityReader


class IdentityReaderTests(unittest.TestCase):
    setUp=identity_fixture.IdentityTests.setUp
    def reader(self):
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET')
                if path=='/api/v3/episodefile/1758':return owner.file
                if path=='/api/v3/episode/170':return owner.episode
                if path=='/api/v3/series/84':return {'id':84,'tmdbId':1234}
                if path.startswith('/api/v3/File/PathEndsWith/'):return getattr(owner,'candidates',[owner.shoko])
                if path.startswith('/api/v3/File/18441/Episode'):return owner.episodes
                if path.startswith('/api/v3/File/18442/Episode'):return getattr(owner,'other_episodes',[])
                if path.startswith('/api/v3/Tmdb/Show/1234/'):return [{'IDs':{'ID':1044}}]
                raise AssertionError(path)
        return StockArrivalIdentityReader(Transport(),Transport(),self.gate)

    def test_reads_stock_records_without_mutation(self):
        self.assertEqual(self.reader()(self.event).shoko_episode_id,2200)

    def test_accessible_source_with_missing_generated_location_is_ready(self):
        self.shoko['Locations'].append({'ManagedFolderID':4,'RelativePath':'/!ShokoRelayVFS_Private/1044/Season 1/S01E01 [18441].mkv'})
        try:result=self.reader()(self.event)
        except ValueError as error:self.fail(f'native duplicate location prevented qualification: {error}')
        self.assertEqual((result.shoko_file_id,result.shoko_series_id,result.shoko_episode_id),(18441,1044,2200))

    def test_missing_alternates_do_not_hide_conflicting_source_records(self):
        self.shoko['Locations'].append({'ManagedFolderID':4,'RelativePath':'/Missing/episode.mkv'})
        other=deepcopy(self.shoko);other['ID']=18442
        self.candidates=[self.shoko,other]
        self.other_episodes=deepcopy(self.episodes)
        with self.assertRaises(ValueError):self.reader()(self.event)

    def test_retained_episode_loss_is_not_ready(self):
        self.episode['episodeFileId']=0
        with self.assertRaises(ValueError):self.reader()(self.event)

    def test_conflicting_record_at_same_source_cannot_be_filtered_away(self):
        other=deepcopy(self.shoko);other['ID']=18442
        self.candidates=[self.shoko,other]
        with self.assertRaises(ValueError):self.reader()(self.event)


class VisibilityReaderTests(unittest.TestCase):
    setUp=visibility_fixture.VisibilityTests.setUp
    def reader(self):
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET')
                if path.startswith('/library/sections/'):
                    section=int(path.split('/')[3])
                    return {'MediaContainer':{'size':len(owner.sections[section]),'totalSize':len(owner.sections[section]),'Metadata':owner.sections[section]}}
                if path.startswith('/library/metadata/'):
                    key=path.split('/')[3].split('?')[0]
                    return {'MediaContainer':{'Metadata':[item for rows in owner.sections.values() for item in rows if item['ratingKey']==key]}}
                raise AssertionError(path)
        # Distinct genuine library objects; hydration must not mix sections.
        self.sections[55][0]['ratingKey']='2'
        return StockArrivalVisibilityReader(Transport(),self.gate)

    def test_both_libraries_are_read_and_exact_parts_checked(self):
        self.assertTrue(self.reader()(self.verified))

    def test_missing_library_is_not_ready(self):
        reader=self.reader();self.sections[55]=[]
        self.assertFalse(reader(self.verified))

    def test_malformed_detail_becomes_bounded_failure(self):
        reader=self.reader()
        request=reader.plex.request_json
        reader.plex.request_json=lambda method,path: {'MediaContainer':None} if path.startswith('/library/metadata/') else request(method,path)
        with self.assertRaises(ValueError):reader(self.verified)
