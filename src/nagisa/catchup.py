"""Durable bounded anime recovery inside Nagisa; native adapter owns decisions."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,math,re,sqlite3,stat,threading,time
from nagisa.worklist import public_reason

_STATES=frozenset({'ready','held-backlog','waiting','blocked','queued','pending','ambiguous','awaiting-receipt','rejected','acquired'})

def _settings(poll_seconds,include_backlog,maximum_per_cycle):
    if type(poll_seconds) is not int or not 300<=poll_seconds<=3600 or type(include_backlog) is not bool or type(maximum_per_cycle) is not int or not 1<=maximum_per_cycle<=20:
        raise ValueError('bounded explicit recovery settings required')

def _reason(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-z0-9-]{1,80}',value) else 'native-observation'

class CatchupService:
    def __init__(self,db,adapter,*,poll_seconds=300,include_backlog=False,maximum_per_cycle=1,clock=time.time,actor_lock=None):
        _settings(poll_seconds,include_backlog,maximum_per_cycle)
        self.db,self.adapter=db,adapter
        self.poll_seconds,self.include_backlog,self.maximum_per_cycle=poll_seconds,include_backlog,maximum_per_cycle
        self.clock=clock;self._cycle=actor_lock or threading.Lock();self._status_lock=threading.Lock()
        self._closed=False;self._last_error=None;self._last_cycle=None;self._counts={};self._refresh_counts()

    def _refresh_counts(self):
        counts={}
        for status,count in self.db.execute('SELECT status,count(*) FROM catchup_episodes GROUP BY status'):
            label=status if status in _STATES else 'unknown'
            counts[label]=counts.get(label,0)+count
        items=[{'episode_id':episode,'state':state if state in _STATES else 'unknown','reason':public_reason(reason)}
            for episode,state,reason in self.db.execute("SELECT episode_id,status,reason FROM catchup_episodes WHERE status!='acquired' ORDER BY episode_id LIMIT 100")]
        with self._status_lock:self._counts=counts;self._work_items=items

    def _enroll(self,missing):
        if not isinstance(missing,list) or len(missing)>10000 or any(type(i) is not int or i<=0 for i in missing) or len(set(missing))!=len(missing):
            raise ValueError('complete unique bounded native missing enumeration required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            initialized=self.db.execute('SELECT initialized FROM catchup_frontier WHERE singleton=1').fetchone()[0]
            state='ready' if initialized or self.include_backlog else 'held-backlog'
            for episode in missing:
                self.db.execute('INSERT OR IGNORE INTO catchup_episodes VALUES(?,?,?,0,0,NULL)',(episode,state,'new-missing' if initialized else 'initial-backlog'))
            if self.include_backlog:
                self.db.execute("UPDATE catchup_episodes SET status='ready',reason='explicit-backlog' WHERE status='held-backlog'")
            self.db.execute('UPDATE catchup_frontier SET initialized=1 WHERE singleton=1');self.db.commit()
        except BaseException:self.db.rollback();raise

    def _update(self,episode,status,reason,now):
        if status not in _STATES:status='blocked'
        attempts=self.db.execute('SELECT attempts FROM catchup_episodes WHERE episode_id=?',(episode,)).fetchone()[0]+1
        delay=min(3600,self.poll_seconds*(2**min(attempts-1,4)))
        self.db.execute('UPDATE catchup_episodes SET status=?,reason=?,attempts=?,next_at=? WHERE episode_id=?',
            (status,_reason(reason),attempts,now+delay,episode))

    def _observe(self,episode,intent,now):
        try:
            result=self.adapter.receipt(episode,json.loads(intent))
            if not isinstance(result,dict) or result.get('status') not in ('acquired','queued','pending','ambiguous','blocked'):
                result={'status':'ambiguous','reason':'receipt-unqualified'}
        except Exception:result={'status':'ambiguous','reason':'receipt-unavailable'}
        self._update(episode,result['status'],result.get('reason'),now)

    def _assess(self,episode,now):
        try:result=self.adapter.assess(episode)
        except Exception:
            self._update(episode,'blocked','native-read-unavailable',now);return
        if not isinstance(result,dict):
            self._update(episode,'blocked','native-assessment-unqualified',now);return
        status=result.get('status')
        if status!='eligible':
            self._update(episode,status if status in ('waiting','blocked','queued','pending','acquired') else 'blocked',result.get('reason'),now);return
        candidate=result.get('candidate')
        try:
            summary=candidate.summary()
            if not isinstance(summary,dict) or type(summary.get('episode_id')) is not int or summary['episode_id']!=episode or type(summary.get('series_id')) is not int or summary['series_id']<=0 or any(not isinstance(summary.get(key),str) or not re.fullmatch(r'[0-9a-f]{64}',summary[key]) for key in ('fingerprint','title_sha256')):
                raise ValueError('invalid candidate identity')
            # Only the native receipt's named identity fields enter private state.
            # Opaque release URLs never enter status, errors or journal records.
            intent={key:summary[key] for key in ('episode_id','series_id','fingerprint','title_sha256','publish_date')}
            published=intent['publish_date']
            if not isinstance(published,str) or len(published)>128 or datetime.fromisoformat(published.replace('Z','+00:00')).tzinfo is None:
                raise ValueError('invalid publication identity')
            intent['dispatch_time']=datetime.fromtimestamp(now,timezone.utc).isoformat()
            encoded=json.dumps(intent,sort_keys=True,separators=(',',':'))
            if len(encoded)>4096:raise ValueError('excessive receipt identity')
        except Exception:
            self._update(episode,'blocked','candidate-identity-unqualified',now);return
        # Autocommit makes this intent durable before any possible native POST.
        reserved=self.db.execute("UPDATE catchup_episodes SET status='ambiguous',reason='dispatch-intent',intent=?,next_at=? WHERE episode_id=? AND intent IS NULL",(encoded,now+self.poll_seconds,episode))
        if reserved.rowcount!=1:return
        try:response=self.adapter.dispatch(candidate)
        except Exception:
            self._update(episode,'ambiguous','dispatch-outcome-unknown',now);return
        if isinstance(response,dict) and response.get('status')=='rejected':
            self._update(episode,'rejected',response.get('reason'),now)
        elif isinstance(response,dict) and response.get('status')=='pending':
            self._update(episode,'pending',response.get('reason'),now)
        else:
            # A returned decision never certifies a queue/grab/import receipt.
            self._update(episode,'awaiting-receipt','native-receipt-required',now)

    def run_once(self):
        if not self._cycle.acquire(blocking=False):return False
        try:
            if self._closed:return False
            now=self.clock()
            if type(now) not in (int,float) or not math.isfinite(now) or now<0:raise ValueError('valid recovery clock required')
            try:
                self._enroll(self.adapter.scan_missing())
                receipts=self.db.execute("SELECT episode_id,intent FROM catchup_episodes WHERE intent IS NOT NULL AND status NOT IN ('acquired','rejected') AND next_at<=? ORDER BY next_at,episode_id LIMIT 20",(now,)).fetchall()
                for episode,intent in receipts:self._observe(episode,intent,now)
                cursor=self.db.execute('SELECT cursor FROM catchup_frontier WHERE singleton=1').fetchone()[0]
                rows=self.db.execute("SELECT episode_id FROM catchup_episodes WHERE intent IS NULL AND status NOT IN ('held-backlog','acquired','rejected') AND next_at<=? ORDER BY CASE WHEN episode_id>? THEN 0 ELSE 1 END,episode_id LIMIT ?",(now,cursor,self.maximum_per_cycle)).fetchall()
                for (episode,) in rows:
                    self.db.execute('UPDATE catchup_frontier SET cursor=? WHERE singleton=1',(episode,))
                    self._assess(episode,now)
            except Exception as error:
                with self._status_lock:self._last_error=type(error).__name__;self._last_cycle=now
                raise
            finally:self._refresh_counts()
            with self._status_lock:self._last_error=None;self._last_cycle=now
            return True
        finally:self._cycle.release()

    cycle=run_once

    def status_snapshot(self):
        with self._status_lock:
            return {'state':'stopped' if self._closed else 'enabled','poll_seconds':self.poll_seconds,'include_backlog':self.include_backlog,
                'last_cycle':self._last_cycle,'last_error':self._last_error,'counts':dict(self._counts),
                'work_items':[dict(item) for item in self._work_items]}

    def close(self):
        with self._cycle:
            if self._closed:return
            self.db.close()
            with self._status_lock:self._closed=True

def build_catchup_service(config_dir,adapter,*,binding,poll_seconds=300,include_backlog=False,maximum_per_cycle=1,clock=time.time,actor_lock=None):
    _settings(poll_seconds,include_backlog,maximum_per_cycle)
    directory=Path(config_dir)
    if not directory.is_absolute() or '..' in directory.parts or directory.resolve(strict=True)!=directory or not directory.is_dir():
        raise ValueError('canonical private recovery configuration directory required')
    if not isinstance(binding,dict) or not binding:raise ValueError('explicit native recovery binding required')
    encoded=json.dumps(binding,sort_keys=True,separators=(',',':'))
    if len(encoded)>4096:raise ValueError('bounded recovery binding required')
    fingerprint=hashlib.sha256(encoded.encode()).hexdigest();journal=directory/'catchup.sqlite3'
    if journal.exists() or journal.is_symlink():
        info=journal.lstat()
        if journal.is_symlink() or journal.resolve(strict=True)!=journal or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValueError('unsafe recovery journal')
    db=sqlite3.connect(journal,isolation_level=None,check_same_thread=False)
    try:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected={'catchup_configuration','catchup_frontier','catchup_episodes'}
        if tables and tables!=expected:raise ValueError('journal is not owned recovery state')
        if tables:
            row=db.execute('SELECT fingerprint FROM catchup_configuration WHERE singleton=1').fetchone()
            if not row or row[0]!=fingerprint:raise ValueError('recovery journal belongs to a different native workflow')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS catchup_configuration(singleton INTEGER PRIMARY KEY CHECK(singleton=1),fingerprint TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS catchup_frontier(singleton INTEGER PRIMARY KEY CHECK(singleton=1),initialized INTEGER NOT NULL,cursor INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS catchup_episodes(episode_id INTEGER PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,attempts INTEGER NOT NULL,next_at REAL NOT NULL,intent TEXT)')
        db.execute('INSERT OR IGNORE INTO catchup_configuration VALUES(1,?)',(fingerprint,))
        # Resolve the actual winner under the write transaction, including two
        # simultaneous first creators that both observed an empty schema.
        actual=db.execute('SELECT fingerprint FROM catchup_configuration WHERE singleton=1').fetchone()
        if not actual or actual[0]!=fingerprint:raise ValueError('recovery journal belongs to a different native workflow')
        db.execute('INSERT OR IGNORE INTO catchup_frontier VALUES(1,0,0)');db.commit()
        service=CatchupService(db,adapter,poll_seconds=poll_seconds,include_backlog=include_backlog,maximum_per_cycle=maximum_per_cycle,clock=clock,actor_lock=actor_lock)
        journal.chmod(0o600);return service
    except BaseException:db.close();raise
