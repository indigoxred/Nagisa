"""Read-only composition for the approved arrival duty, never a second mapper.

Readiness uses native records, complete native owner consumers and physical
source evidence. It does not make stock Relay cleanup atomic or non-destructive.
"""
from pathlib import Path
import hashlib,json,re
from nagisa.reconciler.arrival import PreservationError
from nagisa.reconciler.arrival_identity import VerifiedArrival
from nagisa.reconciler.arrival_preservation import PrivatePlexOwnerPreservation


class StockOwnerScopeReader:
    def __init__(self,shoko,config_directory,expected_vfs_root):
        self.shoko=shoko;self.directory=Path(config_directory)
        self.expected=expected_vfs_root
        if not self.directory.is_absolute() or '..' in self.directory.parts or self.directory.resolve(strict=True)!=self.directory or not self.directory.is_dir():
            raise ValueError('canonical readable stock Relay configuration directory required')
        if not isinstance(self.expected,str) or not self.expected or Path(self.expected).name!=self.expected or self.expected in ('.','..'):
            raise ValueError('explicit stock VFS root basename required')

    def __call__(self,owner):
        try:
            if type(owner) is not int or owner<=0:raise PreservationError('positive native owner required')
            payload=self.shoko.request_json('GET','/api/plugin/ShokoRelay/config')['payload']
            advanced=payload['Advanced']
            if advanced['MergeTmdbSeries'] is not False or advanced['DisableVfsGeneration'] is not False or advanced['DeferVfsCreationUntilFixup'] is not False or advanced['VfsRootPath']!=self.expected:
                raise PreservationError('stock unmerged owner scope not qualified')
            mappings=advanced['SubtitleLanguageMappings']
            if not isinstance(mappings,dict):raise PreservationError('native subtitle mapping configuration missing')
            series=self.shoko.request_json('GET',f'/api/v3/Series/{owner}?includeDataFrom=AniDB')
            if type(series['IDs']['ID']) is not int or series['IDs']['ID']!=owner or type(series['IDs']['AniDB']) is not int or series['IDs']['AniDB']<=0:
                raise PreservationError('native series/AniDB owner identity incomplete')
            aid=series['IDs']['AniDB']
            # The exact supported override CSV is read, never written. Absence
            # requires an accessible canonical directory, not a guessed default.
            if self.directory.resolve(strict=True)!=self.directory or not self.directory.is_dir():raise PreservationError('stock configuration directory changed')
            path=self.directory/'anidb_vfs_overrides.csv'
            try:stat=path.lstat()
            except FileNotFoundError:stat=None
            digest=None
            if stat is not None:
                if path.is_symlink() or not path.is_file() or stat.st_size>1048576:raise PreservationError('native override file inaccessible or excessive')
                raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
                for line in raw.decode('utf-8-sig').splitlines():
                    if not line.strip() or line.strip().startswith('#'):continue
                    for cell in line.split(','):
                        try:value=int(cell.strip())
                        except ValueError:continue
                        if value==aid:raise PreservationError('native owner participates in an override')
                after=path.stat()
                if (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise PreservationError('native override file changed while read')
            fingerprint=hashlib.sha256(json.dumps({'advanced':advanced,'owner':owner,'anidb':aid,'overrideSHA256':digest},sort_keys=True,separators=(',',':')).encode()).hexdigest()
            return {'unmerged':True,'subtitle_language_mappings':mappings,'fingerprint':fingerprint}
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('native owner scope read failed or incomplete') from error


class OwnerPreflight:
    def __init__(self,owner_gate,inventory_guard,record_reader,plex_reader,native_guard,scope_reader):
        self.owner_gate=owner_gate;self.inventory_guard=inventory_guard
        self.record_reader=record_reader;self.plex_reader=plex_reader
        self.native_guard=native_guard;self.scope_reader=scope_reader

    def _owned(self,owner,inventory):
        return {name:value for name,value in inventory.items() if any(Path(name).is_relative_to(root/str(owner)) for root in self.owner_gate.roots)}

    def __call__(self,verified,inventory):
        self.capture(verified,inventory)
        return True

    def capture(self,verified,inventory):
        try:
            if not isinstance(verified,VerifiedArrival):raise PreservationError('verified native arrival required')
            owner=verified.shoko_series_id
            if any(type(v) is not int or v<=0 for v in (verified.shoko_file_id,owner,verified.shoko_episode_id)):
                raise PreservationError('native arrival binding incomplete')
            source=verified.source;stat=source.stat()
            if source.resolve(strict=True)!=source or (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=verified.source_stat:
                raise PreservationError('arrival source binding changed')
            scope=self.scope_reader(owner)
            owned=self._owned(owner,inventory)
            sources=set()
            for value in owned.values():
                if isinstance(value,list) and value and value[0]=='video-source':
                    if len(value)!=7 or not isinstance(value[2],str):raise PreservationError('protected source evidence incomplete')
                    sources.add(Path(value[2]))
            records=self.record_reader(sources|{source})
            file,episodes=records[str(source)]
            if file['ID']!=verified.shoko_file_id or len(episodes)!=1 or episodes[0]['IDs']['ID']!=verified.shoko_episode_id or episodes[0]['IDs']['ParentSeries']!=owner or episodes[0]['AniDB']['Type']!='Episode':
                raise PreservationError('arrival native binding changed')
            sections=self.plex_reader(owner)
            # Capture also checks complete configured sections, native keys,
            # coordinates, accessible Part files, size and canonical sources.
            native_snapshot=self.native_guard.capture(sections,approved_sources=sources if sources else None)
            consumers=[]
            for section,items in sections.items():
                native_sources=set()
                for item in items:
                    match=re.fullmatch(r'tv\.plex\.agents\.custom\.shoko://episode/e([1-9][0-9]*)',item['guid'])
                    if not match or item['grandparentGuid']!=f'tv.plex.agents.custom.shoko://show/{owner}':raise PreservationError('native owner consumer identity differs')
                    # The capture binds each Plex path to its exact Shoko
                    # original by file identity. Use that original in owner
                    # evidence: Plex and Shoko may have different bind paths.
                    for part in native_snapshot[(section,str(item['ratingKey']))][1]:
                        native_source=Path(part[0]);native_sources.add(native_source)
                        consumers.append({'source':str(native_source),'owner':owner,'episode':int(match.group(1))})
                if native_sources!=sources:raise PreservationError('native owner/source coverage differs in a configured library')
            self.owner_gate.qualify(owner,owned,records,consumers,unmerged=scope['unmerged'],subtitle_language_mappings=scope['subtitle_language_mappings'])
            after=self.inventory_guard.verify(inventory)
            if self._owned(owner,after)!=owned:raise PreservationError('owner presentation changed during preflight')
            if self.scope_reader(owner)!=scope:raise PreservationError('native scope changed during preflight')
            stat=source.stat()
            if not source.is_file() or source.resolve(strict=True)!=source or (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=verified.source_stat:
                raise PreservationError('arrival source changed during preflight')
            return PrivatePlexOwnerPreservation.from_capture(owner,native_snapshot,new_source=source,new_episode_id=verified.shoko_episode_id)
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('owner preflight failed or incomplete') from error
