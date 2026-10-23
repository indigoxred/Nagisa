"""Persistent isolated arrival orchestration. Not wired into production.

Ingest actual stock import events; callers supply the tested stock-record reader,
exact visibility probe and independently qualified owner preflight. No permissive
default preflight, media changes, generated links or episode mapping live here.
"""
from dataclasses import asdict
import hashlib
import json
import logging
import math
import re
import threading
import time

from nagisa.reconciler.arrival import ArrivalAttempt, NotDispatched, PreservationError, UnqualifiedOwner
from nagisa.reconciler.arrival_identity import VerifiedArrival
from nagisa.reconciler.models import ImportHistoryEvent
from nagisa.reconciler.movie_arrival import MovieIdentityBlocked


def _safe_build_reason(error):
    # Only stable, code-owned messages may enter the public journal. Never
    # persist arbitrary exception text, which may contain paths or credentials.
    if isinstance(error,PreservationError) and str(error)=='inventory entry cap exceeded':
        return 'presentation-cap'
    return 'build-unqualified'


class ArrivalLoop:
    def __init__(self,connection,guard,identity_reader,visibility,owner_preflight,dispatch,*,initial_watermark,settle_seconds=30,clock=time.time,native_guard=None,event_type=ImportHistoryEvent):
        if type(initial_watermark) is not int or initial_watermark<0:
            raise ValueError('explicit non-negative import watermark required')
        if type(settle_seconds) not in (int,float) or not math.isfinite(settle_seconds) or settle_seconds<0 or not callable(clock):
            raise ValueError('finite non-negative settling duration and epoch clock required')
        self.attempt=ArrivalAttempt(guard,connection,native_guard=native_guard)
        self.event_type=event_type
        self.db=connection
        self.identity_reader=identity_reader
        self.visibility=visibility
        self.owner_preflight=owner_preflight
        self.dispatch=dispatch
        self.settle_seconds=settle_seconds
        # Epoch timestamps survive restart. A reconstructed observer starts a
        # fresh interval; downtime never supplies visibility continuity.
        self.clock=clock
        self._settling_observed=set()
        self.lock=threading.Lock()
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1), watermark INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_inbox(history_id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_failure_reasons(history_id INTEGER PRIMARY KEY,reason TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO arrival_cursor VALUES(1,?)',(initial_watermark,))
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_work_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1), startup_floor INTEGER NOT NULL,last_history INTEGER NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO arrival_work_cursor VALUES(1,?,0)',(initial_watermark,))
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_binding(history_id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS arrival_settling(history_id INTEGER PRIMARY KEY,visible_since REAL NOT NULL,last_seen REAL NOT NULL)')
        self.db.commit()

    def watermark(self):
        return self.db.execute('SELECT watermark FROM arrival_cursor WHERE singleton=1').fetchone()[0]

    def poll_once(self,sonarr):
        # Existing reader returns a complete window or raises. Never advance
        # durable ingestion state after a partial/failed API history read.
        events=sonarr.import_history_after(self.watermark())
        self.ingest(events)
        return self.run_once()

    def ingest(self,events):
        # Save payloads before advancing cursor so unknown identity is never lost.
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                watermark=self.watermark()
                initial=self.db.execute('SELECT startup_floor FROM arrival_work_cursor WHERE singleton=1').fetchone()[0]
                for event in events:
                    if not isinstance(event,self.event_type) or type(event.history_id) is not int or event.history_id<=0:
                        raise ValueError('actual typed import event required')
                    payload=json.dumps(asdict(event),sort_keys=True,separators=(',',':'))
                    prior=self.db.execute('SELECT payload FROM arrival_inbox WHERE history_id=?',(event.history_id,)).fetchone()
                    if prior and prior[0]!=payload:
                        raise ValueError('replayed import identity changed')
                    if not prior and event.history_id>initial:
                        self.db.execute('INSERT INTO arrival_inbox VALUES(?,?,?)',(event.history_id,payload,'pending'))
                    watermark=max(watermark,event.history_id)
                self.db.execute('UPDATE arrival_cursor SET watermark=? WHERE singleton=1',(watermark,))
                self.db.commit()
            except Exception:
                self.db.rollback();raise

    def _status(self,history_id,status):
        self.db.execute('UPDATE arrival_inbox SET status=? WHERE history_id=?',(status,history_id))
        if status!='blocked-build':
            self.db.execute('DELETE FROM arrival_failure_reasons WHERE history_id=?',(history_id,))
        self.db.commit()

    def hold_upgrade_backlog(self):
        """On first candidate start, prevent unissued old imports from rebuilding.

        Keep attempts with an issued reservation under their existing observer.
        The historical inbox remains readable. New imports retain normal retry
        behavior after this one-time transition.
        """
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('CREATE TABLE IF NOT EXISTS arrival_upgrade_hold(singleton INTEGER PRIMARY KEY CHECK(singleton=1),held_count INTEGER NOT NULL)')
                if self.db.execute('SELECT 1 FROM arrival_upgrade_hold WHERE singleton=1').fetchone():
                    self.db.commit()
                    return 0
                unsafe="status NOT IN ('delivered','ambiguous','blocked-source','blocked-preservation','blocked-identity','held-backlog') AND (status='blocked-build' OR NOT EXISTS (SELECT 1 FROM arrival_attempts a WHERE a.history_id=arrival_inbox.history_id AND a.phase NOT IN ('not-dispatched')))"
                held=self.db.execute('SELECT COUNT(*) FROM arrival_inbox WHERE '+unsafe).fetchone()[0]
                self.db.execute("UPDATE arrival_inbox SET status='held-backlog' WHERE "+unsafe)
                self.db.execute('INSERT INTO arrival_upgrade_hold VALUES(1,?)',(held,))
                self.db.commit()
                return held
            except Exception:
                self.db.rollback()
                raise

    def _reset_settling(self,history_id):
        self.db.execute('DELETE FROM arrival_settling WHERE history_id=?',(history_id,))
        self.db.commit()
        self._settling_observed.discard(history_id)

    def _settled(self,history_id,visible):
        if not visible:
            self._reset_settling(history_id)
            return False
        now=self.clock()
        if type(now) not in (int,float) or not math.isfinite(now):
            self._reset_settling(history_id)
            raise PreservationError('visibility clock unavailable')
        row=self.db.execute('SELECT visible_since,last_seen FROM arrival_settling WHERE history_id=?',(history_id,)).fetchone()
        since=now
        if row and history_id in self._settling_observed and now>=row[1] and now-row[1]<=self.settle_seconds:
            since=row[0]
        self.db.execute('INSERT INTO arrival_settling VALUES(?,?,?) ON CONFLICT(history_id) DO UPDATE SET visible_since=excluded.visible_since,last_seen=excluded.last_seen',(history_id,since,now))
        self.db.commit()
        self._settling_observed.add(history_id)
        return now-since>=self.settle_seconds

    def _visible(self,history_id,verified):
        try:
            return self.visibility(verified) is True
        except Exception as exc:
            self._reset_settling(history_id)
            raise ValueError('visibility probe unavailable') from exc
        except BaseException:
            self._reset_settling(history_id)
            raise

    def _process(self,event):
        try:
            verified=self.identity_reader(event)
        except MovieIdentityBlocked:
            self._reset_settling(event.history_id)
            return 'blocked-identity'
        except (ValueError,OSError,RuntimeError):
            self._reset_settling(event.history_id)
            return 'waiting-identity'
        if verified is None:
            self._reset_settling(event.history_id)
            return 'waiting-identity'
        if not isinstance(verified,VerifiedArrival) or verified.history_id!=event.history_id:
            return 'blocked-identity'
        binding={'identity':[verified.shoko_file_id,verified.shoko_series_id,verified.shoko_episode_id],
                 'source':str(verified.source.resolve()),'stat':list(verified.source_stat),
                 'sha1':verified.source_sha1}
        prior=self.db.execute('SELECT phase,series_id FROM arrival_attempts WHERE history_id=?',(event.history_id,)).fetchone()
        if prior and prior[1]!=verified.shoko_series_id:return 'blocked-identity'
        saved=self.db.execute('SELECT payload FROM arrival_binding WHERE history_id=?',(event.history_id,)).fetchone()
        if saved:
            original=json.loads(saved[0])
            if original['identity']!=binding['identity']:return 'blocked-identity'
            if original['source']!=binding['source'] or (original.get('sha1') and original['sha1']!=binding['sha1']):return 'blocked-source'
            if original['stat']!=binding['stat']:
                # A pre-dispatch mover copy may change only the filesystem
                # identity. Check its bytes against the original Shoko digest;
                # any issued or uncertain Relay request retains its old binding.
                old_stat=original['stat'];new_stat=binding['stat']
                if (prior and prior[0]!='not-dispatched') or len(old_stat)!=4 or old_stat[2:]!=new_stat[2:] or not isinstance(binding['sha1'],str) or not re.fullmatch(r'[0-9a-f]{40}',binding['sha1']):
                    return 'blocked-source'
                try:
                    before=verified.source.stat()
                    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=verified.source_stat:return 'blocked-source'
                    digest=hashlib.sha1()
                    with verified.source.open('rb') as source:
                        for chunk in iter(lambda:source.read(4*1024*1024),b''):
                            digest.update(chunk)
                    after=verified.source.stat()
                    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=verified.source_stat or digest.hexdigest()!=binding['sha1']:
                        return 'blocked-source'
                except OSError:return 'blocked-source'
                self.db.execute('UPDATE arrival_binding SET payload=? WHERE history_id=?',(json.dumps(binding,sort_keys=True),event.history_id))
                self.db.commit()
        try:
            info=verified.source.stat()
            if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=verified.source_stat:
                return 'blocked-source'
        except OSError:return 'blocked-source'
        if prior and prior[0] in {'ambiguous','dispatching'}:
            return 'ambiguous'
        try:
            visible=self._visible(event.history_id,verified)
        except (ValueError,OSError,RuntimeError):
            self._reset_settling(event.history_id)
            return 'waiting-visibility'
        if prior and prior[0] in {'awaiting-verification','complete'}:
            if not saved:return 'blocked-identity'
            self.attempt.observe(event.history_id)
            try:
                complete=self.attempt.finish('completed',visible=self._settled(event.history_id,visible))
            except PreservationError as error:
                logging.getLogger(__name__).error('arrival preservation blocked history=%s: %s',event.history_id,error)
                return 'blocked-preservation'
            return 'delivered' if complete else 'settling-visibility' if visible else 'awaiting-visibility'
        if visible:
            self.db.execute('INSERT OR IGNORE INTO arrival_binding VALUES(?,?)',(event.history_id,json.dumps(binding,sort_keys=True)))
            self.db.commit()
            return 'delivered' if self._settled(event.history_id,True) else 'settling-visibility'
        self._reset_settling(event.history_id)
        # Keep dispatch evidence across restart; a fresh reader cannot redefine
        # the source or episode that the issued request was intended to deliver.
        self.db.execute('INSERT OR IGNORE INTO arrival_binding VALUES(?,?)',(event.history_id,json.dumps(binding,sort_keys=True)))
        self.db.commit()
        def guarded_dispatch():
            # Preflight and durable reservation can take time. Recheck the
            # incoming physical binding immediately before the stock client.
            try:
                current=verified.source.stat()
                if not verified.source.is_file() or verified.source.resolve(strict=True)!=verified.source or (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=verified.source_stat:
                    raise NotDispatched('incoming source changed before dispatch')
            except OSError as error:
                raise NotDispatched('incoming source unavailable before dispatch') from error
            return self.dispatch(verified.shoko_series_id)
        try:
            self.attempt.begin(event.history_id,verified.shoko_series_id,guarded_dispatch,preflight=lambda inventory:self.owner_preflight(verified,inventory))
        except UnqualifiedOwner:return 'unqualified-owner'
        except NotDispatched:return 'waiting-relay'
        except Exception as error:
            state=self.db.execute('SELECT phase FROM arrival_attempts WHERE history_id=?',(event.history_id,)).fetchone()
            if state and state[0]=='ambiguous':return 'ambiguous'
            self.db.execute('INSERT INTO arrival_failure_reasons VALUES(?,?) ON CONFLICT(history_id) DO UPDATE SET reason=excluded.reason',
                            (event.history_id,_safe_build_reason(error)))
            self.db.commit()
            return 'blocked-build'
        try:
            info=verified.source.stat()
            if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=verified.source_stat:
                return 'blocked-source'
            visible=self._visible(event.history_id,verified)
            complete=self.attempt.finish('completed',visible=self._settled(event.history_id,visible))
        except PreservationError as error:
            logging.getLogger(__name__).error('arrival preservation blocked history=%s: %s',event.history_id,error)
            return 'blocked-preservation'
        except (ValueError,OSError,RuntimeError):
            self._reset_settling(event.history_id)
            return 'awaiting-visibility'
        return 'delivered' if complete else 'settling-visibility' if visible else 'awaiting-visibility'

    def run_once(self):
        with self.lock:
            # Terminal failures stay visible for review. An uncertain rebuild is
            # not retried even if another actor later makes the episode visible.
            last=self.db.execute('SELECT last_history FROM arrival_work_cursor WHERE singleton=1').fetchone()[0]
            query="SELECT history_id,payload FROM arrival_inbox WHERE status NOT IN ('delivered','ambiguous','blocked-source','blocked-preservation','blocked-identity','held-backlog') AND history_id>? ORDER BY history_id LIMIT 100"
            rows=self.db.execute(query,(last,)).fetchall()
            if not rows:rows=self.db.execute(query,(0,)).fetchall()
            for history_id,payload in rows:
                event=self.event_type(**json.loads(payload))
                self._status(history_id,self._process(event))
                self.db.execute('UPDATE arrival_work_cursor SET last_history=? WHERE singleton=1',(history_id,))
                self.db.commit()
            return dict(self.db.execute('SELECT status,COUNT(*) FROM arrival_inbox GROUP BY status'))
