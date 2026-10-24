import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.reconciler.arrival_identity import ArrivalIdentityGate


class IdentityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        (self.root/'Show').mkdir()
        (self.root/'Show'/'episode.mkv').write_bytes(b'original video')
        self.event=ImportHistoryEvent(123,84,170,1758,'download','/Anime/Show/episode.mkv','/downloads/episode.mkv',14)
        self.file={'id':1758,'seriesId':84,'path':self.event.imported_path,'size':14}
        self.episode={'id':170,'seriesId':84,'episodeFileId':1758}
        self.shoko={'ID':18441,'Size':14,'Locations':[{'ManagedFolderID':4,'RelativePath':'/Show/episode.mkv'}]}
        self.episodes=[{'IDs':{'ID':2200,'ParentSeries':1044},'AniDB':{'Type':'Episode','EpisodeNumber':1}}]
        self.gate=ArrivalIdentityGate('/Anime',self.root,{4:self.root})

    def verify(self):
        return self.gate.verify(self.event,self.file,self.shoko,self.episodes,{1044},self.episode)

    def test_exact_retained_source_returns_stock_owner(self):
        result=self.verify()
        self.assertEqual(result.shoko_series_id,1044)
        self.assertEqual(result.source,(self.root/'Show'/'episode.mkv').resolve())

    def test_native_sha1_is_carried_for_mover_byte_verification(self):
        digest=hashlib.sha1(b'original video').hexdigest().upper()
        self.shoko['Hashes']=[{'Type':'SHA1','Value':digest}]
        self.assertEqual(self.verify().source_sha1,digest.lower())

    def test_missing_generated_alternate_does_not_hide_accessible_source(self):
        original=self.shoko['Locations'][0]
        alternate={'ManagedFolderID':4,'RelativePath':'/!ShokoRelayVFS_Private/1044/Season 1/S01E01 [18441].mkv'}
        for locations in ([alternate,original],[original,alternate]):
            with self.subTest(missing_first=locations[0] is alternate):
                self.shoko['Locations']=locations
                try:result=self.verify()
                except ValueError as error:self.fail(f'accessible retained source was hidden by missing alternate: {error}')
                self.assertEqual((result.shoko_file_id,result.shoko_series_id,result.shoko_episode_id),(18441,1044,2200))
                self.assertEqual(result.source,self.root/'Show'/'episode.mkv')

    def test_all_registered_locations_missing_refuses_delivery(self):
        self.shoko['Locations']=[{'ManagedFolderID':4,'RelativePath':'/Missing/episode.mkv'}]
        with self.assertRaises(ValueError):self.verify()

    def test_malformed_missing_alternate_refuses_delivery(self):
        for raw in ('/../Missing/episode.mkv','//Missing/episode.mkv','/Missing\\episode.mkv','/Missing\x00episode.mkv'):
            with self.subTest(raw=raw):
                self.shoko['Locations']=[{'ManagedFolderID':4,'RelativePath':'/Show/episode.mkv'},
                                         {'ManagedFolderID':4,'RelativePath':raw}]
                with self.assertRaises(ValueError):self.verify()

    def test_escaping_alternate_symlink_refuses_delivery_even_if_target_missing(self):
        with tempfile.TemporaryDirectory() as outside:
            target=Path(outside)/'episode.mkv'
            try:(self.root/'escape').symlink_to(Path(outside),target_is_directory=True)
            except OSError as error:self.skipTest(f'local symlink creation unavailable: {error}')
            self.shoko['Locations'].append({'ManagedFolderID':4,'RelativePath':'/escape/episode.mkv'})
            with self.assertRaises(ValueError):self.verify()
            target.write_bytes(b'original video')
            with self.assertRaises(ValueError):self.verify()

    def test_alternate_permission_error_is_not_treated_as_missing(self):
        self.shoko['Locations'].append({'ManagedFolderID':4,'RelativePath':'/Denied/episode.mkv'})
        resolve=Path.resolve
        def denied(path,*args,**kwargs):
            if 'Denied' in path.parts:raise PermissionError('injected inaccessible alternate')
            return resolve(path,*args,**kwargs)
        with patch.object(Path,'resolve',denied):
            with self.assertRaises(ValueError):self.verify()

    def test_same_basename_elsewhere_does_not_establish_source(self):
        (self.root/'Other').mkdir()
        (self.root/'Other'/'episode.mkv').write_bytes(b'original video')
        self.shoko['Locations'][0]['RelativePath']='/Other/episode.mkv'
        with self.assertRaises(ValueError):self.verify()

    def test_lost_sonarr_ownership_refuses_delivery(self):
        self.file['seriesId']=85
        with self.assertRaises(ValueError):self.verify()

    def test_wrong_shoko_show_refuses_delivery(self):
        self.episodes[0]['IDs']['ParentSeries']=999
        with self.assertRaises(ValueError):self.verify()

    def test_multiple_provider_episodes_require_review(self):
        self.episodes.append({'IDs':{'ID':2201,'ParentSeries':1044},'AniDB':{'Type':'Episode','EpisodeNumber':2}})
        with self.assertRaises(ValueError):self.verify()

    def test_relative_path_traversal_refuses_delivery(self):
        self.shoko['Locations'][0]['RelativePath']='/../Show/episode.mkv'
        with self.assertRaises(ValueError):self.verify()

    def test_source_size_change_refuses_delivery(self):
        (self.root/'Show'/'episode.mkv').write_bytes(b'new video')
        with self.assertRaises(ValueError):self.verify()

    def test_episode_lost_its_file_association_refuses_delivery(self):
        self.episode['episodeFileId']=0
        with self.assertRaises(ValueError):self.verify()
