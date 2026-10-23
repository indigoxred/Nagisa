"""Explicit startup assembly for the approved, still-isolated arrival candidate.

No deployment is implied by this factory. Full consumer/preservation acceptance
and deployment review remain required; the private native observer is not a
solution for concurrent playback or atomic stock cleanup.
"""
from dataclasses import dataclass,replace
from pathlib import Path, PurePosixPath
import hashlib,json,os,sqlite3,stat
from nagisa.http import JsonTransport
from nagisa.reconciler.arrival import SourceCoverageGuard,RelayArrivalClient
from nagisa.reconciler.arrival_identity import ArrivalIdentityGate
from nagisa.reconciler.arrival_loop import ArrivalLoop
from nagisa.reconciler.arrival_owner import OwnerAssociationGate
from nagisa.reconciler.arrival_preflight import OwnerPreflight,StockOwnerScopeReader
from nagisa.reconciler.arrival_preservation import PrivatePlexEpisodeGuard,PrivatePlexOwnerPreservation
from nagisa.reconciler.arrival_readers import StockArrivalIdentityReader,StockArrivalVisibilityReader,StockOwnerRecordReader,StockPlexOwnerReader
from nagisa.reconciler.arrival_service import ArrivalService
from nagisa.reconciler.arrival_boundary import StockArrivalBoundary
from nagisa.reconciler.arrival_visibility import PlexArrivalGate
from nagisa.reconciler.sonarr import SonarrClient

_REQUIRED=frozenset({'schema','generated_roots','plex_presentation_roots','source_roots','managed_roots','sonarr_source_root','sonarr_import_root','relay_config_directory','vfs_root','initial_watermark'})
_MOVIE_KEYS=frozenset({'movie_generation_mode','movie_vfs_root','movie_generated_roots'})
_OPTIONAL=frozenset({'poll_seconds','settle_seconds'})|_MOVIE_KEYS

def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate arrival configuration key')
        result[key]=value
    return result

def _directory(value):
    if not isinstance(value,str) or not value or '\x00' in value:
        raise ValueError('explicit arrival directory required')
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or path==Path(path.anchor) or path.resolve(strict=True)!=path or not path.is_dir():
        raise ValueError('canonical non-root arrival directory required')
    return path

def _roots(values):
    if not isinstance(values,list) or not 1<=len(values)<=8:raise ValueError('bounded arrival root list required')
    paths=tuple(_directory(value) for value in values)
    if len(set(paths))!=len(paths):raise ValueError('distinct arrival roots required')
    return paths

def _movie_roots(values):
    if not isinstance(values,list) or not 1<=len(values)<=8:raise ValueError('bounded movie root list required')
    paths=[]
    for value in values:
        if not isinstance(value,str) or not value or '\x00' in value:raise ValueError('explicit movie root required')
        path=Path(value)
        if not path.is_absolute() or '..' in path.parts or path.is_symlink() or path.parent.resolve(strict=True)!=path.parent or not path.parent.is_dir():
            raise ValueError('canonical existing movie output parent required')
        if os.path.lexists(path):_directory(value)
        paths.append(path)
    if len(set(paths))!=len(paths):raise ValueError('distinct movie output roots required')
    return tuple(paths)

@dataclass(frozen=True)
class ArrivalSettings:
    generated_roots:tuple
    plex_roots:tuple
    source_roots:tuple
    managed_roots:dict
    sonarr_source_root:Path
    relay_config_directory:Path
    vfs_root:str
    initial_watermark:int
    sonarr_import_root:str
    poll_seconds:int=15
    settle_seconds:int=30
    movie_generation_mode:str='Disabled'
    movie_vfs_root:str|None=None
    movie_generated_roots:tuple=()

    @classmethod
    def from_file(cls,path):
        try:
            path=Path(path)
            if not path.is_absolute() or '..' in path.parts or path.is_symlink() or path.resolve(strict=True)!=path or not path.is_file() or path.stat().st_size>16384:
                raise ValueError('small regular canonical arrival configuration required')
            payload=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=_unique_object)
            if not isinstance(payload,dict) or not _REQUIRED<=payload.keys() or payload.keys()-(_REQUIRED|_OPTIONAL) or type(payload['schema']) is not int or payload['schema']!=1:
                raise ValueError('supported explicit arrival configuration required')
            roots=_roots(payload['generated_roots']);plex=_roots(payload['plex_presentation_roots']);sources=_roots(payload['source_roots'])
            vfs=payload['vfs_root']
            if not isinstance(vfs,str) or not vfs or Path(vfs).name!=vfs or vfs in ('.','..') or any(root.name!=vfs for root in roots):
                raise ValueError('stock VFS root must match each generated directory')
            movie_mode='Disabled';movie_vfs=None;movie_roots=()
            if _MOVIE_KEYS & payload.keys():
                if not _MOVIE_KEYS<=payload.keys() or payload['movie_generation_mode']!='EnabledMaintain':
                    raise ValueError('explicit maintained movie protection configuration required')
                movie_mode=payload['movie_generation_mode'];movie_vfs=payload['movie_vfs_root']
                movie_roots=_movie_roots(payload['movie_generated_roots'])
                if not isinstance(movie_vfs,str) or not movie_vfs or '\x00' in movie_vfs or Path(movie_vfs).name!=movie_vfs or movie_vfs in ('.','..') or movie_vfs.casefold()==vfs.casefold() or any(root.name!=movie_vfs for root in movie_roots):
                    raise ValueError('distinct stock movie VFS root must match each protected directory')
                all_roots=roots+movie_roots
                if any(root==other or root.is_relative_to(other) or other.is_relative_to(root) for i,root in enumerate(all_roots) for other in all_roots[i+1:]):
                    raise ValueError('distinct nonoverlapping protected output roots required')
            raw=payload['managed_roots']
            if not isinstance(raw,dict) or not raw or len(raw)>32:raise ValueError('explicit managed source mapping required')
            managed={}
            for key,value in raw.items():
                if not isinstance(key,str) or not key.isdigit() or int(key)<=0 or str(int(key))!=key:
                    raise ValueError('positive native managed-folder ID required')
                root=_directory(value)
                if not any(root.is_relative_to(base) for base in sources):raise ValueError('managed root outside approved sources')
                managed[int(key)]=root
            if any(not os.path.lexists(root) and root.parent not in managed.values() for root in movie_roots):
                raise ValueError('movie output parent must be an explicit managed folder')
            source=_directory(payload['sonarr_source_root'])
            if not any(source.is_relative_to(base) for base in sources):raise ValueError('Sonarr source outside approved roots')
            sonarr_root=payload['sonarr_import_root']
            if not isinstance(sonarr_root,str) or not sonarr_root or '\x00' in sonarr_root or '\\' in sonarr_root:
                raise ValueError('explicit canonical arrival Sonarr root required')
            native_root=PurePosixPath(sonarr_root)
            if not native_root.is_absolute() or '..' in native_root.parts or str(native_root)!=sonarr_root or sonarr_root=='/' or sonarr_root.startswith('//'):
                raise ValueError('explicit canonical arrival Sonarr root required')
            watermark=payload['initial_watermark'];poll=payload.get('poll_seconds',15);settle=payload.get('settle_seconds',30)
            if type(watermark) is not int or watermark<0 or type(poll) is not int or not 15<=poll<=300 or type(settle) is not int or not 15<=settle<=300:
                raise ValueError('bounded explicit arrival timing/frontier required')
            return cls(roots,plex,sources,managed,source,_directory(payload['relay_config_directory']),vfs,watermark,sonarr_root,poll,settle,movie_mode,movie_vfs,movie_roots)
        except (OSError,TypeError,KeyError,UnicodeError) as error:
            raise ValueError('arrival configuration inaccessible or incomplete') from error

    def binding(self,config):
        binding={'generated_roots':list(map(str,self.generated_roots)),'plex_roots':list(map(str,self.plex_roots)),
          'source_roots':list(map(str,self.source_roots)),'managed_roots':{str(k):str(v) for k,v in self.managed_roots.items()},
          'sonarr_source_root':str(self.sonarr_source_root),'relay_config_directory':str(self.relay_config_directory),
          'vfs_root':self.vfs_root,'initial_watermark':self.initial_watermark,'sections':list(config.plex_section_ids),
          'sonarr_root':self.sonarr_import_root,'origins':[config.sonarr_url,config.shoko_url,config.plex_url]}
        if self.movie_generation_mode!='Disabled':
            binding.update(movie_generation_mode=self.movie_generation_mode,movie_vfs_root=self.movie_vfs_root,movie_generated_roots=list(map(str,self.movie_generated_roots)))
        return binding

def build_arrival_service(config,sonarr,*,journal_name='arrival.sqlite3',movie=None):
    if movie is not None:
        if journal_name!='arrival.sqlite3':raise ValueError('movie arrivals require their separate journal')
        journal_name='movie-arrival.sqlite3'
    if journal_name not in ('arrival.sqlite3','subtitle-relay.sqlite3','movie-arrival.sqlite3'):
        raise ValueError('unsupported delivery journal')
    configured=getattr(config,'arrival_config_file',None)
    if configured is None:return None
    directory=_directory(str(config.config_dir));path=Path(configured)
    if not path.is_absolute() or not path.is_relative_to(directory):raise ValueError('arrival configuration must belong to CONFIG_DIR')
    settings=ArrivalSettings.from_file(path)
    from nagisa.reconciler.models import ImportHistoryEvent
    event_type=ImportHistoryEvent;provider='Sonarr'
    if movie is not None:
        from nagisa.delivery import _native
        from nagisa.reconciler.movie_arrival import RadarrArrivalClient,MovieImportHistoryEvent
        if not isinstance(getattr(config,'radarr_url',None),str) or not isinstance(getattr(config,'radarr_token',None),str) or not config.radarr_token:
            raise ValueError('private native Radarr configuration required')
        source=_directory(movie['movie_arrival_source_root'])
        if not any(source.is_relative_to(base) for base in settings.source_roots):raise ValueError('movie source outside protected originals')
        frontier=movie['movie_arrival_initial_watermark'];poll=movie['movie_arrival_poll_seconds']
        if type(frontier) is not int or frontier<0 or type(poll) is not int or not 15<=poll<=300:raise ValueError('bounded movie frontier/timing required')
        settings=replace(settings,sonarr_import_root=_native(movie['movie_arrival_import_root']),sonarr_source_root=source,initial_watermark=frontier,poll_seconds=poll)
        arrival_sonarr=RadarrArrivalClient(JsonTransport(config.radarr_url,headers={'X-Api-Key':config.radarr_token},timeout=config.request_timeout_seconds),settings.sonarr_import_root)
        event_type=MovieImportHistoryEvent;provider='Radarr'
    else:
        arrival_sonarr=SonarrClient(sonarr.transport,allowed_versions=config.sonarr_allowed_versions,import_root=settings.sonarr_import_root)
    # Arrival reads the same installed Sonarr endpoint but has its own source
    # boundary. The existing reconciliation client and /Shoko duties stay intact.
    # Read clients and the sole stock builder remain distinct: long synchronous
    # rebuilds do not give ordinary read requests a 120-second timeout.
    shoko=JsonTransport(config.shoko_url,headers={'apikey':config.shoko_token},timeout=config.request_timeout_seconds,maximum_bytes=16*1024*1024)
    dispatch_transport=JsonTransport(config.shoko_url,headers={'apikey':config.shoko_token},timeout=120,maximum_bytes=16*1024*1024)
    plex=JsonTransport(config.plex_url,headers={'X-Plex-Token':config.plex_token,'X-Plex-Pms-Api-Version':'1.2.2'},timeout=config.request_timeout_seconds,maximum_bytes=16*1024*1024)
    # Movie paths use native episode IDs. Inventory protection includes them,
    # while TV owner/identity readers continue to use only standard VFS roots.
    guard=SourceCoverageGuard(settings.generated_roots+settings.movie_generated_roots,settings.source_roots,strict_roots=settings.movie_generated_roots,optional_roots=settings.movie_generated_roots)
    identity=StockArrivalIdentityReader(arrival_sonarr.transport,shoko,ArrivalIdentityGate(settings.sonarr_import_root,settings.sonarr_source_root,settings.managed_roots))
    if movie is not None:
        from nagisa.reconciler.movie_arrival import StockMovieArrivalIdentityReader
        identity=StockMovieArrivalIdentityReader(arrival_sonarr.transport,shoko,settings.sonarr_import_root,settings.sonarr_source_root,settings.managed_roots)
    view=StockArrivalVisibilityReader(plex,PlexArrivalGate(config.plex_section_ids,settings.generated_roots+settings.plex_roots))
    native=PrivatePlexEpisodeGuard(config.plex_section_ids,settings.generated_roots+settings.plex_roots,settings.source_roots)
    owner_reader=StockPlexOwnerReader(plex,config.plex_section_ids)
    preservation=PrivatePlexOwnerPreservation(owner_reader,native)
    preflight=OwnerPreflight(OwnerAssociationGate(settings.generated_roots,settings.managed_roots),guard,StockOwnerRecordReader(shoko,settings.managed_roots),owner_reader,native,StockOwnerScopeReader(shoko,settings.relay_config_directory,settings.vfs_root))
    boundary=StockArrivalBoundary(shoko,config.shoko_allowed_versions,settings.generated_roots,settings.vfs_root,
        movie_generation_mode=settings.movie_generation_mode,movie_vfs_root=settings.movie_vfs_root,movie_generated_roots=settings.movie_generated_roots)
    journal=directory/journal_name
    if journal.exists() or journal.is_symlink():
        if journal.is_symlink() or journal.resolve(strict=True)!=journal or not stat.S_ISREG(journal.stat().st_mode) or journal.stat().st_nlink!=1:raise ValueError('unsafe existing arrival journal')
    connection=sqlite3.connect(journal,isolation_level=None,check_same_thread=False)
    try:
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'arrival_configuration' not in tables:raise ValueError('existing database is not owned arrival state')
        binding=settings.binding(config)
        if movie is not None:binding.update(provider='Radarr',radarr_origin=config.radarr_url,radarr_version='6.4.4.10685')
        fingerprint=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if tables:
            prior=connection.execute('SELECT fingerprint FROM arrival_configuration WHERE singleton=1').fetchone()
            if not prior or prior[0]!=fingerprint:raise ValueError('arrival journal belongs to a different configured workflow')
        journal.chmod(0o600)
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS arrival_configuration(singleton INTEGER PRIMARY KEY CHECK(singleton=1),fingerprint TEXT NOT NULL)')
        prior=connection.execute('SELECT fingerprint FROM arrival_configuration WHERE singleton=1').fetchone()
        if prior and prior[0]!=fingerprint:raise ValueError('arrival journal belongs to a different configured workflow')
        connection.execute('INSERT OR IGNORE INTO arrival_configuration VALUES(1,?)',(fingerprint,));connection.commit()
        loop=ArrivalLoop(connection,guard,identity,view,boundary.preflight(preflight.capture),boundary.dispatch(RelayArrivalClient(dispatch_transport).rebuild),initial_watermark=settings.initial_watermark,settle_seconds=settings.settle_seconds,native_guard=preservation,event_type=event_type)
        loop.hold_upgrade_backlog()
        return ArrivalService(arrival_sonarr,loop,poll_seconds=settings.poll_seconds,provider=provider)
    except BaseException:
        connection.close();raise
