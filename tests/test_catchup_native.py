import copy
import datetime as dt
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from nagisa.catchup_native import NativeCatchupAdapter, CatchupNativeError

NOW=dt.datetime(2026,10,3,12,tzinfo=dt.timezone.utc)

class Fake:
    def __init__(self):
        self.posts=[]; self.calls=[]
        self.episode={'id':1,'seriesId':2,'monitored':True,'hasFile':False,'episodeFileId':0,'seasonNumber':1,'episodeNumber':1,'absoluteEpisodeNumber':1,'airDateUtc':'2026-10-01T00:00:00Z'}
        self.series={'id':2,'seriesType':'anime','monitored':True,'qualityProfileId':3,'tvdbId':4,'tags':[],'useSceneNumbering':False}
        self.release={'guid':'native-guid','title':'[Group] Fixture - 01 [1080p]','size':500000000,'indexerId':5,'indexer':'Fixture','protocol':'torrent','publishDate':'2026-10-03T11:00:00Z','downloadUrl':'http://secret.invalid/api?apikey=DO-NOT-LEAK','magnetUrl':None,'infoHash':'a'*40,'seeders':10,'leechers':1,'indexerFlags':0,'tvdbId':4,'tvRageId':0,'imdbId':None,'mappedSeriesId':2,'mappedEpisodeInfo':[{'id':1}],'mappedAbsoluteEpisodeNumbers':[1],'absoluteEpisodeNumbers':[1],'episodeNumbers':[],'isAbsoluteNumbering':True,'fullSeason':False,'special':False,'sceneMapping':None,'approved':True,'rejected':False,'temporarilyRejected':False,'rejections':[],'releaseWeight':0}
        self.rows={'system/status':{'version':'4.0.19.2979'},'series/2':self.series,'episode/1':self.episode,'episodefile?seriesId=2':[],'command':[],'delayprofile':[{'id':1,'order':1,'tags':[],'enableTorrent':True,'torrentDelay':30,'usenetDelay':0,'preferredProtocol':'torrent','bypassIfHighestQuality':False,'bypassIfAboveCustomFormatScore':False}], 'qualityprofile':[{'id':3,'upgradeAllowed':False}], 'config/mediamanagement':{'downloadPropersAndRepacks':'doNotUpgrade'},'indexer':[{'id':5,'enableInteractiveSearch':True,'enableAutomaticSearch':True,'protocol':'torrent'}],'release?episodeId=1':[self.release],'history/episode?episodeId=1':[]}
        self.series['title']='Fixture'
        self.release['seriesTitle']='Fixture'
        self.queue=[]; self.missing=[dict(self.episode,series=self.series)]
    def request_json(self,method,path,body=None):
        self.calls.append((method,path)); route=path.removeprefix('/api/v3/')
        if method=='POST':
            self.posts.append(copy.deepcopy(body)); return [dict(self.release)]
        if route.startswith('queue?'):return {'page':1,'pageSize':100,'totalRecords':len(self.queue),'records':copy.deepcopy(self.queue)}
        if route.startswith('wanted/missing?'):return {'page':1,'pageSize':100,'totalRecords':len(self.missing),'records':copy.deepcopy(self.missing)}
        if route.startswith('history?'):
            rows=self.rows.get('history/episode?episodeId=1',[])
            return {'page':1,'pageSize':100,'totalRecords':len(rows),'records':copy.deepcopy(rows)}
        if route.startswith('history/episode'):raise KeyError('unsupported-native-history-route')
        return copy.deepcopy(self.rows[route])

class NativeCatchupTests(unittest.TestCase):
    def setUp(self):
        self.fake=Fake();self.adapter=NativeCatchupAdapter(SimpleNamespace(transport=self.fake,allowed_versions={'4.0.19.2979'}),clock=lambda:NOW)
    def test_native_exact_push_preserves_fields_and_no_overrides(self):
        self.assertEqual(self.adapter.scan_missing(),[1]);result=self.adapter.assess(1)
        self.assertEqual(result['status'],'eligible');candidate=result['candidate']
        self.assertNotIn('DO-NOT-LEAK',str(candidate));self.assertNotIn('DO-NOT-LEAK',str(candidate.summary()))
        response=self.adapter.dispatch(candidate);self.assertTrue(response['approved'])
        sent=self.fake.posts[0];self.assertEqual(sent['publishDate'],self.fake.release['publishDate']);self.assertEqual(sent['downloadUrl'],self.fake.release['downloadUrl'])
        self.assertFalse({'shouldOverride','episodeIds','quality','languages','downloadClientId'}&sent.keys())
    def test_young_top_waits_instead_of_selecting_old_lower(self):
        self.fake.release['publishDate']='2026-10-03T11:59:00Z';older=copy.deepcopy(self.fake.release);older.update(publishDate='2026-10-03T10:00:00Z',releaseWeight=1)
        self.fake.rows['release?episodeId=1'].append(older)
        self.assertEqual(self.adapter.assess(1)['status'],'waiting');self.assertFalse(self.fake.posts)
    def test_do_not_prefer_with_disabled_upgrades_preserves_native_policy(self):
        self.fake.rows['config/mediamanagement']['downloadPropersAndRepacks']='doNotPrefer'
        result=self.adapter.assess(1)
        self.assertEqual(result['status'],'eligible')
        self.assertTrue(self.adapter.dispatch(result['candidate'])['approved'])
        self.assertEqual(len(self.fake.posts),1)
        self.assertEqual(self.fake.rows['config/mediamanagement']['downloadPropersAndRepacks'],'doNotPrefer')
        self.assertNotIn('shouldOverride',self.fake.posts[0])
        self.fake.rows['qualityprofile'][0]['upgradeAllowed']=True
        self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_owned_pending_active_search_and_proper_policy_block(self):
        for field,value in [('hasFile',True),('episodeFileId',9)]:
            with self.subTest(field=field):
                old=self.fake.episode[field];self.fake.episode[field]=value
                self.assertEqual(self.adapter.assess(1)['status'],'blocked');self.fake.episode[field]=old
        self.fake.queue=[{'episodeId':1,'seriesId':2,'status':'delay'}];self.assertEqual(self.adapter.assess(1)['status'],'blocked');self.fake.queue=[]
        self.fake.rows['command']=[{'name':'EpisodeSearch','status':'queued','body':{'episodeIds':[1]}}];self.assertEqual(self.adapter.assess(1)['status'],'blocked');self.fake.rows['command']=[]
        self.fake.rows['config/mediamanagement']['downloadPropersAndRepacks']='preferAndUpgrade';self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_mapping_pack_scene_and_native_rejection_block(self):
        for field,value in [('fullSeason',True),('sceneMapping',{'title':'alias'}),('mappedEpisodeInfo',[{'id':1},{'id':8}]),('mappedAbsoluteEpisodeNumbers',[8]),('approved',False),('tvdbId',9),('absoluteEpisodeNumbers',[1,2])]:
            with self.subTest(field=field):
                old=self.fake.release[field];self.fake.release[field]=value
                self.assertEqual(self.adapter.assess(1)['status'],'blocked');self.fake.release[field]=old
    def test_dispatch_rechecks_owner_settings_and_candidate(self):
        for mutate in [lambda:self.fake.episode.update(hasFile=True),lambda:self.fake.rows['qualityprofile'][0].update(upgradeAllowed=True),lambda:self.fake.release.update(size=600000000)]:
            self.setUp();candidate=self.adapter.assess(1)['candidate'];mutate()
            with self.assertRaises(CatchupNativeError):self.adapter.dispatch(candidate)
            self.assertFalse(self.fake.posts)
    def test_malformed_unsafe_rows_never_dispatched(self):
        self.fake.release['releaseWeight']=False;self.assertEqual(self.adapter.assess(1)['status'],'blocked')
        self.fake.rows['system/status']['version']='future';self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_incomplete_missing_pagination_raises_safely(self):
        original=self.fake.request_json
        def request(method,path,body=None):
            result=original(method,path,body)
            if 'wanted/missing' in path:result['totalRecords']=101
            return result
        self.fake.request_json=request
        with self.assertRaises(CatchupNativeError):self.adapter.scan_missing()
    def test_receipt_requires_native_grab_not_push_approval(self):
        candidate=self.adapter.assess(1)['candidate'];intent=dict(candidate.summary(),dispatch_time=NOW.isoformat())
        self.assertEqual(self.adapter.receipt(1,intent)['status'],'ambiguous')
        self.fake.rows['history/episode?episodeId=1']=[{'id':10,'episodeId':1,'eventType':'grabbed','date':NOW.isoformat(),'sourceTitle':self.fake.release['title']}]
        self.assertEqual(self.adapter.receipt(1,intent)['status'],'queued')
        self.fake.episode['hasFile']=True;self.fake.episode['episodeFileId']=9
        self.fake.rows['episodefile?seriesId=2']=[{'id':9}]
        self.assertEqual(self.adapter.receipt(1,intent)['status'],'acquired')
    def test_grab_receipt_uses_native_paged_history_endpoint(self):
        candidate=self.adapter.assess(1)['candidate'];intent=dict(candidate.summary(),dispatch_time=NOW.isoformat())
        self.fake.rows['history/episode?episodeId=1']=[{'id':10,'episodeId':1,'eventType':'grabbed','date':NOW.isoformat(),'sourceTitle':self.fake.release['title']}]
        self.assertEqual(self.adapter.receipt(1,intent)['status'],'queued')
        self.assertTrue(any(method=='GET' and path.startswith('/api/v3/history?episodeId=1&') for method,path in self.fake.calls))
        self.assertFalse(any('/history/episode' in path for _,path in self.fake.calls))
    def test_zero_external_id_requires_exact_native_canonical_title(self):
        self.fake.release['tvdbId']=0
        self.assertEqual(self.adapter.assess(1)['status'],'eligible')
        self.fake.release['seriesTitle']='Ambiguous alias'
        self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_own_search_timestamp_does_not_invalidate_stable_candidate(self):
        candidate=self.adapter.assess(1)['candidate'];self.fake.episode['lastSearchTime']=NOW.isoformat()
        self.assertEqual(self.adapter.dispatch(candidate)['status'],'approved')
    def test_complete_pagination_and_anime_only_frontier(self):
        original=self.fake.request_json
        def request(method,path,body=None):
            if 'wanted/missing' in path:
                rows=[dict(self.fake.missing[0],id=n) for n in range(1,102)]
                rows[-1]['series']=dict(self.fake.series,seriesType='standard')
                page=2 if 'page=2&' in path else 1
                return {'page':page,'pageSize':100,'totalRecords':101,'records':rows[(page-1)*100:page*100]}
            return original(method,path,body)
        self.fake.request_json=request
        self.assertEqual(self.adapter.scan_missing(),list(range(1,101)))
    def test_dispatch_timeout_is_ambiguous_and_pending_response_not_grab(self):
        candidate=self.adapter.assess(1)['candidate']
        self.fake.release.update(approved=False,temporarilyRejected=True,rejected=False,rejections=['Waiting'])
        # Preserve the GET candidate while change occurs only inside native POST.
        original=self.fake.request_json
        def request(method,path,body=None):
            if method=='POST':return [dict(self.fake.release)]
            value=original(method,path,body)
            if 'release?episodeId' in path:
                value[0].update(approved=True,temporarilyRejected=False,rejected=False,rejections=[])
            return value
        self.fake.request_json=request
        self.assertEqual(self.adapter.dispatch(candidate)['status'],'pending')
        def timeout(method,path,body=None):
            if method=='POST':raise TimeoutError('secret endpoint token')
            return request(method,path,body)
        self.fake.request_json=timeout
        with self.assertRaisesRegex(CatchupNativeError,'native-push-outcome-ambiguous'):self.adapter.dispatch(candidate)
    def test_boolean_native_identity_is_not_an_episode_id(self):
        self.fake.episode['id']=True
        self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_stock_default_scene_flag_does_not_change_absolute_anime_route(self):
        self.fake.series['useSceneNumbering']=True
        result=self.adapter.assess(1)
        self.assertEqual(result['status'],'eligible')
        self.assertEqual(self.adapter.dispatch(result['candidate'])['status'],'approved')
        self.fake.release['sceneMapping']={'title':'Ambiguous alias'}
        self.assertEqual(self.adapter.assess(1)['status'],'blocked')
    def test_proper_policy_uses_actual_media_management_resource(self):
        result=self.adapter.assess(1)
        self.assertEqual(result['status'],'eligible')
        self.assertIn(('GET','/api/v3/config/mediamanagement'),self.fake.calls)
        self.assertNotIn(('GET','/api/v3/config/indexer'),self.fake.calls)
        self.fake.rows['config/mediamanagement']={}
        self.assertEqual(self.adapter.assess(1)['reason'],'no-proper-upgrade-policy-required')
    def test_native_empty_optional_magnet_preserves_valid_download_source(self):
        self.fake.release['downloadUrl']='magnet:?xt=urn:btih:'+('a'*40)
        self.fake.release['magnetUrl']=''
        result=self.adapter.assess(1);self.assertEqual(result['status'],'eligible')
        self.adapter.dispatch(result['candidate'])
        self.assertEqual(self.fake.posts[0]['downloadUrl'],self.fake.release['downloadUrl'])
        self.assertEqual(self.fake.posts[0]['magnetUrl'],'')
    def test_native_service_restart_keeps_private_intent_and_never_redispatches(self):
        from nagisa.catchup import build_catchup_service
        with tempfile.TemporaryDirectory() as directory:
            clock=lambda:NOW.timestamp()
            service=build_catchup_service(Path(directory).resolve(),self.adapter,binding=self.adapter.binding_summary(),include_backlog=True,clock=clock)
            service.run_once();self.assertEqual(len(self.fake.posts),1)
            intent=service.db.execute('SELECT intent FROM catchup_episodes WHERE episode_id=1').fetchone()[0]
            self.assertNotIn('DO-NOT-LEAK',intent);self.assertNotIn('secret.invalid',intent);service.close()
            service=build_catchup_service(Path(directory).resolve(),self.adapter,binding=self.adapter.binding_summary(),include_backlog=True,clock=lambda:NOW.timestamp()+3600)
            try:service.run_once();self.assertEqual(len(self.fake.posts),1)
            finally:service.close()

if __name__=='__main__':unittest.main()
