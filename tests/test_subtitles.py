import json
import sqlite3
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.reconciler.sonarr import SonarrClient
from nagisa.reconciler.qbit import QbitObserver
try:
    from nagisa.subtitles import OriginalSubtitleDelivery
except ImportError:
    OriginalSubtitleDelivery = None

class Transport:
    def __init__(self, event):
        self.event = event
        self.files = []
        self.owner = 9
        self.calls = []
    def request_json(self, method, path, *args):
        self.calls.append(path)
        e = self.event
        if path == '/api/v3/system/status': return {'version':'test'}
        if path.startswith('/api/v3/history'):
            rows=[]
            for e in getattr(self,'events',[self.event]):
                row={'id':e.history_id,'seriesId':e.series_id,'episodeId':e.episode_id,'eventType':'downloadFolderImported','data':{'fileId':e.sonarr_file_id,'importedPath':e.imported_path,'droppedPath':e.dropped_path,'size':e.source_size}}
                if e.download_id is not None: row['downloadId']=e.download_id
                rows.append(row)
            return {'totalRecords':len(rows),'records':sorted(rows,key=lambda row:row['id'],reverse=True)}
        if path.startswith('/api/v3/episode/'):
            return {'id':e.episode_id,'seriesId':e.series_id,'episodeFileId':self.owner}
        if path.startswith('/api/v3/episodefile?'):
            return [{'id':9,'seriesId':e.series_id,'path':e.imported_path,'size':e.source_size}]
        if path.startswith('/api/v2/torrents/info'):
            return [{'hash':e.download_id,'progress':0.9,'amount_left':1,'state':'downloading','save_path':'/downloads','content_path':'/downloads/Release'}]
        if path.startswith('/api/v2/torrents/files?'): return self.files
        raise AssertionError(path)

class SubtitleTests(unittest.TestCase):
    def test_unlinked_import_is_held_without_torrent_manifest_or_subtitle_write(self):
        self.event = ImportHistoryEvent(11,2,3,9,None,'/Anime/Show/S01E01.mkv',
                                        '/incoming/Release/Episode.mkv',5)
        self.transport.event = self.event
        service=self.service()
        self.assertTrue(service.run_once())
        self.assertEqual(service.db.execute(
            'SELECT status FROM subtitle_events WHERE history_id=11').fetchone()[0],
            'held-unlinked-source')
        self.assertEqual(service.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '11')
        self.assertFalse(any('/api/v2/torrents/' in path for path in self.transport.calls))
        self.assertEqual(self.refreshes, [])
        self.assertEqual(service.status_snapshot()['counts']['held-unlinked-source'], 1)
        service.close()
        reopened=self.service()
        self.assertEqual(reopened.status_snapshot()['counts']['held-unlinked-source'],1)
        self.assertEqual(reopened.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '11')
        service=reopened
        self.transport.calls.clear()
        self.event = ImportHistoryEvent(12,2,4,9,'a'*40,'/Anime/Show/S01E01.mkv',
                                        '/incoming/Release/Episode.mkv',5)
        self.transport.event = self.event
        self.assertTrue(service.run_once())
        self.assertEqual(service.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '12')
        self.assertEqual(service.db.execute(
            'SELECT status FROM subtitle_events WHERE history_id=11').fetchone()[0],
            'held-unlinked-source')
        self.assertTrue(any('/api/v2/torrents/' in path for path in self.transport.calls))

    def test_changed_unlinked_history_replay_cannot_overwrite_journal(self):
        self.event = ImportHistoryEvent(11,2,3,9,None,'/Anime/Show/S01E01.mkv',
                                        '/incoming/Release/Episode.mkv',5)
        self.transport.event = self.event
        service=self.service()
        with service.db:
            service.db.execute('INSERT INTO subtitle_events(history_id,payload,status) VALUES(?,?,?)',
                               (11,'{"changed":true}','held-unlinked-source'))
        with self.assertRaises(ValueError):
            service.run_once()
        self.assertEqual(service.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '0')
        self.assertEqual(service.db.execute(
            'SELECT payload FROM subtitle_events WHERE history_id=11').fetchone()[0],
            '{"changed":true}')

    def test_outside_root_import_is_recorded_before_subtitle_cursor_advances(self):
        self.event = ImportHistoryEvent(11,2,3,9,None,'/Other/Show/S01E01.mkv',
                                        '/incoming/Release/Episode.mkv',5)
        self.transport.event = self.event
        service=self.service()
        self.assertTrue(service.run_once())
        self.assertEqual(service.db.execute(
            'SELECT status FROM subtitle_events WHERE history_id=11').fetchone()[0],
            'skipped-outside-root')
        self.assertEqual(service.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '11')
        self.assertFalse(any('/api/v2/torrents/' in path for path in self.transport.calls))
        with service.db:
            service.db.execute("UPDATE subtitle_meta SET value='0' WHERE key='watermark'")
        service.sonarr.import_root=PurePosixPath('/Other')
        with self.assertRaises(ValueError):
            service.run_once()

    def test_thirty_segment_imports_share_thirteen_files_and_all_hold(self):
        self.transport.events=[ImportHistoryEvent(
            8431+i,2,300+i,900+i%13,None,
            f'/Anime/Show/{i%13:02}.mkv',
            f'/incoming/Release/{i%13:02}.mkv',5,
        ) for i in range(30)]
        service=self.service()
        self.assertTrue(service.run_once())
        self.assertEqual(service.db.execute(
            "SELECT value FROM subtitle_meta WHERE key='watermark'").fetchone()[0], '8460')
        self.assertEqual(service.db.execute(
            "SELECT COUNT(*) FROM subtitle_events WHERE status='held-unlinked-source'").fetchone()[0],30)
        self.assertFalse(any('/api/v2/torrents/' in path for path in self.transport.calls))

    def test_manifest_refuses_unlinked_event_even_if_called_directly(self):
        from nagisa.subtitles import _Blocked
        service=self.service()
        self.transport.calls.clear()
        unlinked=ImportHistoryEvent(11,2,3,9,None,'/Anime/Show/S01E01.mkv',
                                    '/incoming/Release/Episode.mkv',5)
        with self.assertRaises(_Blocked) as caught:
            service._manifest(unlinked)
        self.assertEqual(caught.exception.status,'held-unlinked-source')
        self.assertEqual(self.transport.calls,[])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base/'source'; self.source.mkdir()
        self.dest = self.base/'managed'; self.dest.mkdir()
        (self.source/'Release').mkdir(); (self.dest/'Show').mkdir()
        (self.source/'Release'/'Episode.mkv').write_bytes(b'video')
        (self.dest/'Show'/'S01E01.mkv').write_bytes(b'video')
        self.event = ImportHistoryEvent(11,2,3,9,'a'*40,'/Anime/Show/S01E01.mkv','/incoming/Release/Episode.mkv',5)
        self.transport = Transport(self.event)
        self.transport.files = [{'name':'Release/Episode.mkv','size':5,'progress':1}]
        self.refreshes=[]
        self.services=[]
        self.addCleanup(lambda: [s.close() for s in self.services])
    def sidecar(self, name, data=b'original', progress=1):
        p = self.source/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(data)
        self.transport.files.append({'name':name,'size':len(data),'progress':progress})
    def service(self, **kw):
        self.assertIsNotNone(OriginalSubtitleDelivery, 'original subtitle worker is missing')
        connection=sqlite3.connect(self.base/'journal.db',check_same_thread=False)
        self.addCleanup(connection.close)
        def verified_refresh(event,destination):
            self.refreshes.append(destination)
            return True
        s=OriginalSubtitleDelivery(SonarrClient(self.transport,allowed_versions=['test'],import_root='/Anime'),QbitObserver(self.transport),verified_refresh,source_mounts={'/downloads':self.source,'/incoming':self.source},destination_root='/Anime',destination_mount=self.dest,connection=connection,initial_watermark=0,**kw)
        self.services.append(s); return s
    def test_exact_flat_and_nested_originals_preserve_suffix_and_source(self):
        self.sidecar('Release/Episode.en.forced.ass')
        self.sidecar('Release/Episode/french.srt',b'fr')
        s=self.service(); self.assertEqual(self.transport.calls,[]); s.run_once()
        self.assertEqual((self.dest/'Show/S01E01.en.forced.ass').read_bytes(),b'original')
        self.assertEqual((self.dest/'Show/S01E01.french.srt').read_bytes(),b'fr')
        self.assertEqual((self.source/'Release/Episode.en.forced.ass').read_bytes(),b'original')
    def test_incomplete_manifest_retries_after_restart_without_cursor_reset(self):
        self.sidecar('Release/Episode.srt',progress=0.5)
        s=self.service(); s.run_once(); self.assertFalse((self.dest/'Show/S01E01.srt').exists()); s.close()
        self.transport.files[-1]['progress']=1
        s=self.service(); s.run_once(); self.assertEqual((self.dest/'Show/S01E01.srt').read_bytes(),b'original')
        count=len(self.refreshes); s.run_once(); self.assertEqual(len(self.refreshes),count)
    def test_conflicting_destination_never_overwritten(self):
        self.sidecar('Release/Episode.srt'); out=self.dest/'Show/S01E01.srt'; out.write_bytes(b'keep')
        s=self.service(); s.run_once(); self.assertEqual(out.read_bytes(),b'keep'); self.assertEqual(s.status_snapshot()['counts'],{'conflict':1})
    def test_wrong_video_path_and_owner_never_write(self):
        self.sidecar('Release/Episode.srt'); self.transport.files[0]['name']='Other/Episode.mkv'
        s=self.service(); s.run_once(); self.assertFalse((self.dest/'Show/S01E01.srt').exists())
        self.transport.files[0]['name']='Release/Episode.mkv'; self.transport.owner=10; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_traversal_manifest_blocks_entire_event(self):
        self.sidecar('Release/Episode.srt'); self.transport.files.append({'name':'../escape.srt','size':1,'progress':1})
        s=self.service(); s.run_once(); self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_symlink_source_is_rejected(self):
        p=self.source/'Release/Episode.srt'; outside=self.base/'outside.srt'; outside.write_bytes(b'original')
        try: p.symlink_to(outside)
        except OSError: self.skipTest('symlinks unavailable')
        self.transport.files.append({'name':'Release/Episode.srt','size':8,'progress':1})
        s=self.service(); s.run_once(); self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_idx_sub_pair_is_complete_and_bounded(self):
        self.sidecar('Release/Episode.en.idx',b'idx'); self.sidecar('Release/Episode.en.sub',b'sub',progress=0.5)
        s=self.service(max_file_bytes=4,max_total_bytes=6); s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.en.idx').exists())
        self.transport.files[-1]['progress']=1; s.run_once()
        self.assertEqual((self.dest/'Show/S01E01.en.idx').read_bytes(),b'idx')
        self.assertEqual((self.dest/'Show/S01E01.en.sub').read_bytes(),b'sub')
    def test_generic_nested_subtitle_folder_is_not_guessed(self):
        self.sidecar('Release/Subs/english.srt'); s=self.service(); s.run_once()
        self.assertEqual(list((self.dest/'Show').glob('*.srt')),[])
        self.assertEqual(s.status_snapshot()['counts'],{'ambiguous':1})
    def test_byte_limit_blocks_without_partial_copies(self):
        self.sidecar('Release/Episode.srt'); s=self.service(max_file_bytes=4); s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_same_size_destination_replaced_after_prepare_blocks_install(self):
        self.sidecar('Release/Episode.srt'); s=self.service()
        original=self.transport.request_json; reads=[]
        def request(method,path,*args):
            if path.startswith('/api/v3/episode/'):
                reads.append(1)
                if len(reads)==2:
                    (self.dest/'Show/S01E01.mkv').write_bytes(b'other')
            return original(method,path,*args)
        self.transport.request_json=request; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
        self.assertEqual(s.status_snapshot()['counts'],{'blocked-owner':1})
    def test_same_size_destination_replaced_on_restart_retry_blocks_install(self):
        self.sidecar('Release/Episode.srt',progress=0.5); s=self.service(); s.run_once(); s.close()
        (self.dest/'Show/S01E01.mkv').write_bytes(b'other')
        self.transport.files[-1]['progress']=1; s=self.service(); s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
        self.assertEqual(s.status_snapshot()['counts'],{'blocked-owner':1})
    def test_new_competing_manifest_video_blocks_before_install(self):
        self.sidecar('Release/Episode.en.srt'); s=self.service()
        original=self.transport.request_json; reads=[]
        def request(method,path,*args):
            if path.startswith('/api/v2/torrents/files?'):
                reads.append(1)
                if len(reads)==2:
                    self.sidecar('Release/Episode.en.mkv',b'other')
            return original(method,path,*args)
        self.transport.request_json=request; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.en.srt').exists())
        self.assertEqual(s.status_snapshot()['counts'],{'ambiguous':1})
    def test_refresh_false_or_none_keeps_durable_visibility_pending(self):
        self.sidecar('Release/Episode.srt'); s=self.service()
        for result in (False,None):
            s.refresh=lambda *args: result
            s.run_once()
            self.assertEqual(s.status_snapshot()['counts'],{'waiting-refresh':1})
        s.close(); s=self.service(); s.refresh=lambda *args: True; s.run_once()
        self.assertEqual(s.status_snapshot()['counts'],{'delivered':1})
    def test_delivered_torrent_can_retire_without_reprocessing(self):
        self.sidecar('Release/Episode.srt'); s=self.service(); s.run_once()
        self.transport.files=[]
        (self.source/'Release/Episode.mkv').unlink()
        (self.source/'Release/Episode.srt').unlink()
        self.transport.calls.clear(); s.run_once()
        self.assertEqual(s.status_snapshot()['counts'],{'delivered':1})
        self.assertFalse(any('/api/v2/' in path or '/api/v3/episode' in path for path in self.transport.calls))
        self.assertEqual(len(self.refreshes),1)
    def test_bounded_pending_cycle_rotates_fairly_across_restart(self):
        from dataclasses import asdict,replace
        self.sidecar('Release/Episode.srt',progress=0.5); s=self.service()
        with s.db:
            for ident in range(1000,1205):
                payload=json.dumps(asdict(replace(self.event,history_id=ident)))
                s.db.execute('INSERT INTO subtitle_events(history_id,payload,status) VALUES(?,?,?)',(ident,payload,'pending'))
        s.run_once()
        self.assertEqual(s.status_snapshot()['counts'],{'pending':106,'waiting-download':100})
        s.close(); s=self.service(); s.run_once()
        self.assertEqual(s.status_snapshot()['counts'],{'pending':6,'waiting-download':200})
        s.run_once(); self.assertEqual(s.status_snapshot()['counts'],{'waiting-download':206})
    def test_owner_changes_after_prepare_block_before_install(self):
        self.sidecar('Release/Episode.srt'); s=self.service()
        original=self.transport.request_json; reads=[]
        def request(method,path,*args):
            if path.startswith('/api/v3/episode/'):
                reads.append(1)
                if len(reads)==2: self.transport.owner=10
            return original(method,path,*args)
        self.transport.request_json=request; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
        self.assertEqual(s.status_snapshot()['counts'],{'blocked-owner':1})
    def test_history_source_changes_after_prepare_block_before_install(self):
        from dataclasses import replace
        self.sidecar('Release/Episode.srt'); s=self.service()
        original=self.transport.request_json; reads=[]
        def request(method,path,*args):
            if path.startswith('/api/v3/history'):
                reads.append(1)
                if len(reads)==2: self.transport.event=replace(self.event,dropped_path='/incoming/Other.mkv')
            return original(method,path,*args)
        self.transport.request_json=request; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_same_size_changed_video_on_late_retry_is_blocked(self):
        self.sidecar('Release/Episode.srt',progress=0.5)
        s=self.service(); s.run_once()
        (self.source/'Release/Episode.mkv').write_bytes(b'other')
        self.transport.files[-1]['progress']=1; s.run_once()
        self.assertFalse((self.dest/'Show/S01E01.srt').exists())
    def test_identical_existing_sidecar_refreshes_once_and_keeps_bytes(self):
        self.sidecar('Release/Episode.srt'); out=self.dest/'Show/S01E01.srt'; out.write_bytes(b'original')
        s=self.service(); s.run_once(); s.run_once()
        self.assertEqual(out.read_bytes(),b'original'); self.assertEqual(len(self.refreshes),1)
    def test_total_limit_does_not_install_first_smaller_file(self):
        self.sidecar('Release/Episode.en.srt',b'four'); self.sidecar('Release/Episode.fr.srt',b'five!')
        s=self.service(max_total_bytes=8); s.run_once()
        self.assertEqual(list((self.dest/'Show').glob('*.srt')),[])
    def test_changed_manifest_subtitle_while_waiting_visibility_does_not_overwrite(self):
        self.sidecar('Release/Episode.srt'); s=self.service(); s.refresh=lambda *args: False; s.run_once()
        (self.source/'Release/Episode.srt').write_bytes(b'modified'); s.run_once()
        self.assertEqual((self.dest/'Show/S01E01.srt').read_bytes(),b'original')
        self.assertEqual(s.status_snapshot()['counts'],{'blocked-source':1})
    def test_refresh_failure_is_durable_and_does_not_recopy(self):
        self.sidecar('Release/Episode.srt'); s=self.service()
        def fail(*args): raise RuntimeError('private-secret')
        s.refresh=fail; s.run_once(); self.assertEqual(s.status_snapshot()['counts'],{'waiting-refresh':1}); s.close()
        s=self.service(); s.run_once()
        self.assertEqual((self.dest/'Show/S01E01.srt').read_bytes(),b'original')
        self.assertEqual(s.status_snapshot()['counts'],{'delivered':1})
    def test_journal_root_change_is_rejected_and_status_is_secret_safe(self):
        self.sidecar('Release/Episode.srt'); s=self.service(); s.run_once(); s.close()
        self.dest=self.base/'other'; self.dest.mkdir()
        with self.assertRaises(ValueError): self.service()
        self.assertNotIn(str(self.base), json.dumps(s.status_snapshot()))

if __name__ == '__main__': unittest.main()
