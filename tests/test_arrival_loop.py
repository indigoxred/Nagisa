import sqlite3
import os
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.reconciler.arrival_identity import ArrivalIdentityGate, VerifiedArrival
from nagisa.reconciler.arrival import InventoryGuard, PreservationError
from nagisa.reconciler.arrival_loop import ArrivalLoop
from nagisa.reconciler.sonarr import SonarrClient


class LoopTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.presentation=self.root/'presentation';self.presentation.mkdir()
        self.source=self.root/'sources';self.source.mkdir()
        self.video=self.source/'episode.mkv';self.video.write_bytes(b'video')
        self.dbpath=self.root/'state.sqlite'
        self.db=sqlite3.connect(self.dbpath,isolation_level=None);self.addCleanup(self.db.close)
        self.event=ImportHistoryEvent(17,84,170,1758,'download','/Anime/episode.mkv','/downloads/episode.mkv',5)
        st=self.video.stat()
        self.verified=VerifiedArrival(17,99,1044,2200,self.video.resolve(),(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns))
        self.identity=self.verified;self.visible=False;self.safe=True;self.calls=[]
        self.guard=InventoryGuard((self.presentation,),(self.source,))

    def loop(self,db=None):
        return ArrivalLoop(db or self.db,self.guard,lambda event:self.identity,lambda verified:self.visible,lambda verified,inventory:self.safe,self.dispatch,initial_watermark=16,settle_seconds=0)

    def dispatch(self,owner):
        self.calls.append(owner)
        return {'Status':'ok','Data':{'Errors':[]}}

    def test_already_visible_is_completed_without_rebuild(self):
        self.visible=True
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[])

    def test_unknown_identity_survives_ingestion_watermark(self):
        self.identity=None
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.watermark(),17)
        self.assertEqual(loop.run_once(),{'waiting-identity':1})
        self.identity=self.verified;self.visible=True
        self.assertEqual(loop.run_once(),{'delivered':1})

    def test_visibility_retry_does_not_rebuild_twice(self):
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.visible=True
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_closed_reopened_loop_observes_without_redispatch(self):
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        self.db.close()
        db=sqlite3.connect(self.dbpath,isolation_level=None);self.addCleanup(db.close)
        self.visible=True
        self.assertEqual(self.loop(db).run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_changed_source_blocks_before_dispatch(self):
        loop=self.loop();loop.ingest((self.event,))
        self.video.write_bytes(b'changed source')
        self.assertEqual(loop.run_once(),{'blocked-source':1})
        self.assertEqual(self.calls,[])

    def _replace_source_preserving_mtime(self,content):
        original=self.video.stat()
        replacement=self.source/'replacement.mkv'
        replacement.write_bytes(content)
        os.utime(replacement,ns=(original.st_atime_ns,original.st_mtime_ns))
        os.replace(replacement,self.video)
        current=self.video.stat()
        self.assertNotEqual(original.st_ino,current.st_ino)
        self.identity=replace(self.verified,source_stat=(current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns))

    def test_mover_same_bytes_before_dispatch_rechecks_and_proceeds(self):
        self.verified=replace(self.verified,source_sha1=hashlib.sha1(b'video').hexdigest())
        self.identity=self.verified;self.safe=False
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'unqualified-owner':1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arrival_attempts').fetchone()[0],0)
        self._replace_source_preserving_mtime(b'video')
        self.safe=True
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.assertEqual(self.calls,[1044])

    def test_mover_changed_bytes_before_dispatch_stays_blocked(self):
        self.verified=replace(self.verified,source_sha1=hashlib.sha1(b'video').hexdigest())
        self.identity=self.verified;self.safe=False
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        self._replace_source_preserving_mtime(b'other')
        self.safe=True
        self.assertEqual(loop.run_once(),{'blocked-source':1})
        self.assertEqual(self.calls,[])

    def test_mover_without_native_sha1_stays_blocked(self):
        self.safe=False
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        self._replace_source_preserving_mtime(b'video')
        self.safe=True
        self.assertEqual(loop.run_once(),{'blocked-source':1})
        self.assertEqual(self.calls,[])

    def test_mover_same_bytes_after_dispatch_stays_blocked(self):
        self.verified=replace(self.verified,source_sha1=hashlib.sha1(b'video').hexdigest())
        self.identity=self.verified
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self._replace_source_preserving_mtime(b'video')
        self.assertEqual(loop.run_once(),{'blocked-source':1})
        self.assertEqual(self.calls,[1044])

    def test_preflight_and_durable_journal_use_same_snapshot(self):
        import json,copy
        asset=self.presentation/'original.ass';asset.write_bytes(b'original subtitle')
        snapshots=[]
        def preflight(verified,inventory):
            self.assertEqual(verified,self.verified)
            snapshots.append(copy.deepcopy(inventory))
            return True
        loop=self.loop();loop.owner_preflight=preflight;loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        saved=json.loads(self.db.execute('SELECT inventory_json FROM arrival_attempts WHERE history_id=17').fetchone()[0])
        self.assertEqual(snapshots,[saved])

    def test_rejected_snapshot_preflight_never_reserves_or_dispatches(self):
        loop=self.loop();loop.owner_preflight=lambda verified,inventory:False;loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'unqualified-owner':1})
        self.assertEqual(self.calls,[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM arrival_attempts').fetchone()[0],0)

    def test_unqualified_owner_blocks_before_dispatch(self):
        self.safe=False
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'unqualified-owner':1})
        self.assertEqual(self.calls,[])

    def test_ambiguous_dispatch_cannot_become_delivered_when_visible(self):
        loop=self.loop();loop.ingest((self.event,))
        def timeout(owner):raise TimeoutError()
        loop.dispatch=timeout
        self.assertEqual(loop.run_once(),{'ambiguous':1})
        self.visible=True
        self.assertEqual(loop.run_once(),{'ambiguous':1})

    def test_upgrade_holds_old_blocked_build_without_holding_future_imports(self):
        loop=self.loop();loop.ingest((self.event,))
        capture=self.guard.capture
        self.guard.capture=lambda: (_ for _ in ()).throw(ValueError('private inventory failure'))
        self.assertEqual(loop.run_once(),{'blocked-build':1})
        self.assertEqual(self.calls,[])
        waiting=replace(self.event,history_id=18)
        loop.ingest((waiting,));loop._status(18,'waiting-identity')
        self.assertEqual(loop.hold_upgrade_backlog(),2)
        self.assertEqual(loop.hold_upgrade_backlog(),0)
        self.guard.capture=capture
        self.assertEqual(loop.run_once(),{'held-backlog':2})
        self.assertEqual(self.calls,[])
        next_event=replace(self.event,history_id=19)
        loop.identity_reader=lambda event:replace(self.verified,history_id=event.history_id)
        loop.ingest((next_event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1,'held-backlog':2})
        self.assertEqual(self.calls,[1044])

    def test_new_blocked_build_after_upgrade_can_retry(self):
        loop=self.loop()
        self.assertEqual(loop.hold_upgrade_backlog(),0)
        loop.ingest((self.event,))
        capture=self.guard.capture
        self.guard.capture=lambda: (_ for _ in ()).throw(ValueError('private inventory failure'))
        self.assertEqual(loop.run_once(),{'blocked-build':1})
        self.guard.capture=capture
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.assertEqual(self.calls,[1044])

    def test_blocked_build_records_only_a_safe_failure_reason(self):
        loop=self.loop();loop.ingest((self.event,))
        capture=self.guard.capture
        self.guard.capture=lambda: (_ for _ in ()).throw(PreservationError('inventory entry cap exceeded'))
        self.assertEqual(loop.run_once(),{'blocked-build':1})
        self.assertEqual(self.db.execute('SELECT reason FROM arrival_failure_reasons WHERE history_id=17').fetchone(),('presentation-cap',))
        self.assertEqual(self.calls,[])
        self.guard.capture=lambda: (_ for _ in ()).throw(ValueError('/private/path token=secret'))
        self.assertEqual(loop.run_once(),{'blocked-build':1})
        self.assertEqual(self.db.execute('SELECT reason FROM arrival_failure_reasons WHERE history_id=17').fetchone(),('build-unqualified',))
        self.assertNotIn('private',str(self.db.execute('SELECT reason FROM arrival_failure_reasons WHERE history_id=17').fetchone()))
        self.guard.capture=capture
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.assertIsNone(self.db.execute('SELECT reason FROM arrival_failure_reasons WHERE history_id=17').fetchone())

    def test_upgrade_keeps_issued_attempt_under_observation(self):
        loop=self.loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.assertEqual(loop.hold_upgrade_backlog(),0)
        self.visible=True
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_replayed_event_cannot_replace_original_payload(self):
        loop=self.loop();loop.ingest((self.event,))
        changed=ImportHistoryEvent(17,85,170,1758,'download','/Anime/episode.mkv','/downloads/episode.mkv',5)
        with self.assertRaises(ValueError):loop.ingest((changed,))

    def test_preservation_failure_is_recorded_as_blocked(self):
        subtitle=self.presentation/'episode.en.ass';subtitle.write_text('original')
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        subtitle.unlink();self.visible=True
        self.assertEqual(loop.run_once(),{'blocked-preservation':1})

    def test_out_of_order_batch_does_not_drop_a_real_import(self):
        loop=self.loop()
        later=ImportHistoryEvent(18,84,171,1759,'download','/Anime/next.mkv','/downloads/next.mkv',5)
        loop.ingest((later,self.event))
        self.assertEqual(loop.watermark(),18)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arrival_inbox').fetchone()[0],2)

    def test_source_changed_during_rebuild_is_not_delivered(self):
        loop=self.loop();loop.ingest((self.event,))
        def changed(owner):
            self.video.write_bytes(b'other')
            os.utime(self.video,ns=(self.video.stat().st_atime_ns,self.verified.source_stat[3]+1_000_000_000))
            self.visible=True
            return {'Status':'ok','Data':{'Errors':[]}}
        loop.dispatch=changed
        self.assertEqual(loop.run_once(),{'blocked-source':1})

    def test_separate_descending_pages_retain_unseen_import(self):
        loop=self.loop()
        loop.ingest((replace(self.event,history_id=18),))
        loop.ingest((self.event,))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arrival_inbox').fetchone()[0],2)

    def test_pending_first_hundred_do_not_starve_later_import(self):
        loop=self.loop()
        loop.identity_reader=lambda event: replace(self.verified,history_id=117) if event.history_id==117 else None
        self.visible=True
        loop.ingest(tuple(replace(self.event,history_id=i) for i in range(17,118)))
        self.assertEqual(loop.run_once(),{'pending':1,'waiting-identity':100})
        self.assertEqual(loop.run_once(),{'delivered':1,'waiting-identity':100})

    def test_fresh_reader_cannot_reset_dispatched_source_evidence(self):
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        self.video.write_bytes(b'other')
        os.utime(self.video,ns=(self.video.stat().st_atime_ns,self.verified.source_stat[3]+1_000_000_000))
        st=self.video.stat()
        self.identity=replace(self.verified,source_stat=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns))
        self.visible=True
        self.assertEqual(loop.run_once(),{'blocked-source':1})
        self.assertEqual(self.calls,[1044])

    def test_episode_identity_cannot_change_after_dispatch(self):
        loop=self.loop();loop.ingest((self.event,));loop.run_once()
        self.identity=replace(self.verified,shoko_episode_id=2201)
        self.visible=True
        self.assertEqual(loop.run_once(),{'blocked-identity':1})

    def test_existing_stock_history_reader_feeds_arrival_loop(self):
        calls=[]
        class Transport:
            def request_json(_self,method,path):
                calls.append((method,path))
                return {'totalRecords':1,'records':[{'id':17,'eventType':'downloadFolderImported','seriesId':84,'episodeId':170,'downloadId':'a'*40,'data':{'fileId':1758,'importedPath':'/Anime/episode.mkv','droppedPath':'/downloads/episode.mkv','size':5}}]}
        client=SonarrClient(Transport(),allowed_versions=(),import_root='/Anime')
        loop=self.loop();self.visible=True
        self.assertEqual(loop.poll_once(client),{'delivered':1})
        self.assertEqual(loop.watermark(),17)
        self.assertEqual(self.calls,[])
        self.assertEqual(calls[0][0],'GET')

    def test_unlinked_stock_history_uses_exact_identity_and_holds_if_source_gone(self):
        class Transport:
            def request_json(_self,method,path):
                return {'totalRecords':1,'records':[{'id':17,'eventType':'downloadFolderImported',
                    'seriesId':84,'episodeId':170,
                    'data':{'fileId':1758,'importedPath':'/Anime/episode.mkv',
                            'droppedPath':'/downloads/episode.mkv','size':5}}]}
        client=SonarrClient(Transport(),allowed_versions=(),import_root='/Anime')
        loop=self.loop();self.identity=None;self.visible=True
        self.assertEqual(loop.poll_once(client),{'waiting-identity':1})
        self.assertEqual(loop.watermark(),17)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.db.execute(
            'SELECT status FROM arrival_inbox WHERE history_id=17').fetchone()[0],
            'waiting-identity')
        self.identity=self.verified
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[])

    def test_thirty_unlinked_arrivals_are_durably_held_and_later_linked_row_ingests(self):
        events=tuple(ImportHistoryEvent(
            8431+i,84,300+i,900+i%13,None,
            f'/Anime/{i%13:02}.mkv',f'/downloads/{i%13:02}.mkv',5,
        ) for i in range(30))
        linked=ImportHistoryEvent(8461,84,400,999,'a'*40,
                                  '/Anime/linked.mkv','/downloads/linked.mkv',5)
        loop=ArrivalLoop(self.db,self.guard,lambda event:None,
                         lambda verified:True,lambda verified,inventory:True,
                         self.dispatch,initial_watermark=8430,settle_seconds=0)
        loop.ingest((*events,linked))
        self.assertEqual(loop.watermark(),8461)
        self.assertEqual(loop.run_once(),{'waiting-identity':31})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM arrival_inbox').fetchone()[0],31)
        self.assertEqual(self.calls,[])

    def test_two_sonarr_positions_one_shoko_broadcast_can_arrive_without_torrent_id(self):
        # Panty-like numbering: two Sonarr/TVDB positions refer to one physical
        # broadcast with one Shoko/AniDB episode. This is an arrival-only proof.
        events=(
            ImportHistoryEvent(17,84,170,1758,None,'/Anime/episode.mkv','/downloads/episode.mkv',5),
            ImportHistoryEvent(18,84,171,1758,None,'/Anime/episode.mkv','/downloads/episode.mkv',5),
        )
        gate=ArrivalIdentityGate('/Anime',self.source,{4:self.source})
        shoko={'ID':99,'Size':5,'Locations':[{'ManagedFolderID':4,'RelativePath':'/episode.mkv'}]}
        episodes=[{'IDs':{'ID':2200,'ParentSeries':1044},'AniDB':{'Type':'Episode','EpisodeNumber':1}}]
        def identity(event):
            sonarr_file={'id':1758,'seriesId':84,'path':event.imported_path,'size':5}
            sonarr_episode={'id':event.episode_id,'seriesId':84,'episodeFileId':1758}
            return gate.verify(event,sonarr_file,shoko,episodes,{1044},sonarr_episode)
        loop=ArrivalLoop(self.db,self.guard,identity,lambda verified:True,
                         lambda verified,inventory:True,self.dispatch,
                         initial_watermark=16,settle_seconds=0)
        loop.ingest(events)
        self.assertEqual(loop.run_once(),{'delivered':2})
        self.assertEqual(self.calls,[])
        self.assertEqual(loop.watermark(),18)

    def test_failed_history_read_does_not_skip_events(self):
        class Reader:
            def import_history_after(_self,watermark):raise ValueError('incomplete page')
        loop=self.loop()
        with self.assertRaises(ValueError):loop.poll_once(Reader())
        self.assertEqual(loop.watermark(),16)

    def settling_loop(self,db=None,seconds=30):
        return ArrivalLoop(db or self.db,self.guard,lambda event:self.identity,lambda verified:self.visible,lambda verified,inventory:self.safe,self.dispatch,initial_watermark=16,settle_seconds=seconds,clock=lambda:self.now)

    def test_transient_native_guid_visibility_does_not_complete_delivery(self):
        self.now=1000
        loop=self.settling_loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.now=1065.274;self.visible=True
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.assertEqual(self.db.execute('SELECT phase FROM arrival_attempts').fetchone(),('awaiting-verification',))
        self.now=1068;self.visible=False
        self.assertEqual(loop.run_once(),{'awaiting-visibility':1})
        self.now=1070;self.visible=True
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1099
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1100
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_sustained_visibility_completes_after_configured_interval(self):
        self.now=1000
        loop=self.settling_loop(seconds=10);loop.ingest((self.event,));loop.run_once()
        self.visible=True
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1009
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1010
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_restart_preserves_settling_without_completing_early(self):
        self.now=1000
        loop=self.settling_loop();loop.ingest((self.event,));loop.run_once()
        self.visible=True
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.db.close()
        db=sqlite3.connect(self.dbpath,isolation_level=None);self.addCleanup(db.close)
        self.now=1015;resumed=self.settling_loop(db)
        self.assertEqual(resumed.run_once(),{'settling-visibility':1})
        self.now=1030
        self.assertEqual(resumed.run_once(),{'settling-visibility':1})
        self.now=1045
        self.assertEqual(resumed.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_replayed_preexisting_visible_event_cannot_bypass_settling(self):
        self.now=1000;self.visible=True
        loop=self.settling_loop();loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1015;loop.ingest((self.event,))
        resumed=self.settling_loop()
        self.assertEqual(resumed.run_once(),{'settling-visibility':1})
        self.now=1030
        self.assertEqual(resumed.run_once(),{'settling-visibility':1})
        self.now=1045
        self.assertEqual(resumed.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[])

    def test_backward_clock_resets_visibility_interval(self):
        self.now=1000;self.visible=True
        loop=self.settling_loop();loop.ingest((self.event,));loop.run_once()
        self.now=1020
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1010
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1030
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1040
        self.assertEqual(loop.run_once(),{'delivered':1})

    def test_stale_observation_gap_does_not_count_as_stable_visibility(self):
        self.now=1000;self.visible=True
        loop=self.settling_loop();loop.ingest((self.event,));loop.run_once()
        self.now=1100
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1120
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1130
        self.assertEqual(loop.run_once(),{'delivered':1})

    def test_unexpected_visibility_probe_exception_clears_settling(self):
        self.now=1000
        loop=self.settling_loop();loop.ingest((self.event,));loop.run_once()
        self.visible=True
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        def unreadable(verified):raise KeyError('missing native response field')
        self.now=1020;loop.visibility=unreadable
        try:status=loop.run_once()
        except KeyError:self.fail('visibility probe exception must produce a waiting state')
        self.assertEqual(status,{'waiting-visibility':1})
        self.now=1030;loop.visibility=lambda verified:self.visible
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1060
        self.assertEqual(loop.run_once(),{'delivered':1})
        self.assertEqual(self.calls,[1044])

    def test_default_interval_requires_thirty_seconds_of_visibility(self):
        self.now=1000;self.visible=True
        loop=ArrivalLoop(self.db,self.guard,lambda event:self.identity,lambda verified:self.visible,lambda verified,inventory:self.safe,self.dispatch,initial_watermark=16,clock=lambda:self.now)
        loop.ingest((self.event,))
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1029
        self.assertEqual(loop.run_once(),{'settling-visibility':1})
        self.now=1030
        self.assertEqual(loop.run_once(),{'delivered':1})
