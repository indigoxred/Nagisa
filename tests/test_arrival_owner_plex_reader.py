import unittest
from copy import deepcopy
from nagisa.reconciler.arrival import PreservationError
from nagisa.reconciler.arrival_readers import StockPlexOwnerReader

class OwnerPlexReaderTests(unittest.TestCase):
    def setUp(self):
        self.show={'type':'show','ratingKey':'1','guid':'tv.plex.agents.custom.shoko://show/156','librarySectionID':53}
        self.season={'type':'season','ratingKey':'2','guid':'tv.plex.agents.custom.shoko://season/156/1','parentRatingKey':'1','librarySectionID':53,'index':1}
        self.episode={'type':'episode','ratingKey':'3','guid':'tv.plex.agents.custom.shoko://episode/e20','parentRatingKey':'2','grandparentRatingKey':'1','librarySectionID':53,'parentIndex':1,'index':1,'viewCount':1,'Media':[{'Part':[{'file':'/example/anime/show-156/Season 1/stock.mkv','size':5}]}]}
        self.calls=[];self.shows=[self.show];self.episodes=[self.episode]
        self.details={'1':self.show,'2':self.season,'3':self.episode}

    def reader(self):
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET');owner.calls.append(path)
                if path.startswith('/library/sections/53/all?'):
                    return {'MediaContainer':{'size':len(owner.shows),'totalSize':len(owner.shows),'Metadata':deepcopy(owner.shows)}}
                if path.startswith('/library/metadata/1/allLeaves?'):
                    return {'MediaContainer':{'size':len(owner.episodes),'totalSize':len(owner.episodes),'Metadata':deepcopy(owner.episodes)}}
                key=path.split('/')[3].split('?')[0]
                return {'MediaContainer':{'Metadata':[deepcopy(owner.details[key])]}}
        return StockPlexOwnerReader(Transport(),(53,))

    def test_complete_native_owner_is_hydrated_without_mutation(self):
        result=self.reader()(156)
        self.assertEqual(result[53][0]['parentGuid'],self.season['guid'])
        self.assertEqual(result[53][0]['grandparentGuid'],self.show['guid'])
        self.assertTrue(all('skipRefresh=1' in call for call in self.calls if '/all?' not in call and '/allLeaves?' not in call))

    def test_absent_owner_is_explicit_empty_not_missing_section(self):
        self.shows=[]
        self.assertEqual(self.reader()(156),{53:[]})

    def test_ambiguous_owner_show_is_refused(self):
        other=deepcopy(self.show);other['ratingKey']='4';self.shows.append(other)
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_episode_from_other_show_is_refused(self):
        self.episode['grandparentRatingKey']='4'
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_existing_wrong_show_guid_is_not_trusted(self):
        self.episode['grandparentGuid']='tv.plex.agents.custom.shoko://show/157'
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_season_from_other_owner_is_refused(self):
        self.season['parentRatingKey']='4'
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_season_number_mismatch_is_refused(self):
        self.season['index']=0
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_episode_without_parts_is_refused(self):
        self.episode['Media']=[]
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_unknown_episode_guid_is_refused(self):
        self.episode['guid']='imdb://unknown'
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_stock_segmented_episode_guid_is_hydrated(self):
        self.episode['guid']='tv.plex.agents.custom.shoko://episode/e20p2'
        self.assertEqual(self.reader()(156)[53][0]['guid'],self.episode['guid'])

    def test_duplicate_episode_key_is_refused(self):
        self.episodes.append(deepcopy(self.episode))
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_truncated_page_is_refused(self):
        reader=self.reader();request=reader.plex.request_json
        def truncated(method,path):
            result=request(method,path)
            if '/all?' in path:result['MediaContainer']['totalSize']=2
            return result
        reader.plex.request_json=truncated
        with self.assertRaises(PreservationError):reader(156)

    def test_owner_read_checks_every_configured_section(self):
        reader=self.reader();reader.sections=(53,55)
        with self.assertRaises(PreservationError):reader(156)

    def test_boolean_episode_coordinate_is_not_native_numbering(self):
        self.episode['parentIndex']=True
        with self.assertRaises(PreservationError):self.reader()(156)

    def test_optional_batched_details_keep_complete_owner_proof(self):
        other=deepcopy(self.episode);other['ratingKey']='4';other['index']=2
        self.episodes.append(other);self.details['4']=other
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET');owner.calls.append(path)
                if path.startswith('/library/sections/53/all?'):
                    rows=owner.shows
                elif path.startswith('/library/metadata/1/allLeaves?'):
                    rows=owner.episodes
                else:
                    keys=path.split('/')[3].split('?')[0].split(',')
                    rows=[owner.details[key] for key in keys]
                    if len(keys)>1:rows.reverse()
                return {'MediaContainer':{'size':len(rows),'totalSize':len(rows),
                                          'Metadata':deepcopy(rows)}}
        reader=StockPlexOwnerReader(Transport(),(53,),batch_details=True)
        result=reader(156)
        self.assertEqual({row['ratingKey'] for row in result[53]},{'3','4'})
        details=[path for path in self.calls if 'skipRefresh=1' in path]
        self.assertEqual(len(details),3)
        self.assertTrue(any('/metadata/3,4?' in path for path in details))

    def test_batched_details_refuse_missing_episode(self):
        other=deepcopy(self.episode);other['ratingKey']='4';other['index']=2
        self.episodes.append(other);self.details['4']=other
        owner=self
        class Transport:
            def request_json(self,method,path):
                if path.startswith('/library/sections/53/all?'):
                    rows=owner.shows
                elif path.startswith('/library/metadata/1/allLeaves?'):
                    rows=owner.episodes
                else:
                    keys=path.split('/')[3].split('?')[0].split(',')
                    rows=[owner.details[key] for key in keys if key!='4']
                return {'MediaContainer':{'size':len(rows),'totalSize':len(rows),
                                          'Metadata':deepcopy(rows)}}
        with self.assertRaises(PreservationError):
            StockPlexOwnerReader(Transport(),(53,),batch_details=True)(156)

    def test_exact_guid_filter_avoids_full_show_census_for_present_owner(self):
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET');owner.calls.append(path)
                if path.startswith('/library/sections/53/all?'):
                    rows=owner.shows
                elif '/allLeaves?' in path:
                    rows=owner.episodes
                else:
                    key=path.split('/')[3].split('?')[0]
                    rows=[owner.details[key]]
                return {'MediaContainer':{'size':len(rows),'totalSize':len(rows),
                                          'Metadata':deepcopy(rows)}}
        result=StockPlexOwnerReader(Transport(),(53,),exact_guid_filter=True)(156)
        self.assertEqual(result[53][0]['ratingKey'],'3')
        listings=[path for path in self.calls if path.startswith('/library/sections/53/all?')]
        self.assertEqual(len(listings),1)
        self.assertIn('guid=',listings[0])

    def test_exact_guid_filter_rechecks_empty_response_with_full_census(self):
        owner=self
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET');owner.calls.append(path)
                if path.startswith('/library/sections/53/all?'):
                    rows=[] if 'guid=' in path else owner.shows
                elif '/allLeaves?' in path:
                    rows=owner.episodes
                else:
                    key=path.split('/')[3].split('?')[0]
                    rows=[owner.details[key]]
                return {'MediaContainer':{'size':len(rows),'totalSize':len(rows),
                                          'Metadata':deepcopy(rows)}}
        result=StockPlexOwnerReader(Transport(),(53,),exact_guid_filter=True)(156)
        self.assertEqual(result[53][0]['ratingKey'],'3')
        listings=[path for path in self.calls if path.startswith('/library/sections/53/all?')]
        self.assertEqual(len(listings),2)

    def test_exact_guid_filter_rejects_unrelated_result(self):
        other=deepcopy(self.show)
        other['guid']='tv.plex.agents.custom.shoko://show/157'
        self.shows=[other]
        with self.assertRaises(PreservationError):
            StockPlexOwnerReader(self.reader().plex,(53,),exact_guid_filter=True)(156)

    def test_exact_guid_filter_rechecks_unrelated_response_against_full_census(self):
        owner=self
        other=deepcopy(self.show)
        other['guid']='tv.plex.agents.custom.shoko://show/157'
        class Transport:
            def request_json(self,method,path):
                owner.assertEqual(method,'GET');owner.calls.append(path)
                if path.startswith('/library/sections/53/all?'):
                    rows=[other] if 'guid=' in path else owner.shows
                elif '/allLeaves?' in path:
                    rows=owner.episodes
                else:
                    key=path.split('/')[3].split('?')[0]
                    rows=[owner.details[key]]
                return {'MediaContainer':{'size':len(rows),'totalSize':len(rows),
                                          'Metadata':deepcopy(rows)}}
        result=StockPlexOwnerReader(Transport(),(53,),exact_guid_filter=True)(156)
        self.assertEqual(result[53][0]['ratingKey'],'3')
        listings=[path for path in self.calls if path.startswith('/library/sections/53/all?')]
        self.assertEqual(len(listings),2)
