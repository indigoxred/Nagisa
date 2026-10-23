"""Private idle-test episode preservation observer; not wired into production.

The caller must supply complete, hydrated stock Plex reads for every section.
This is not full franchise/artwork qualification or concurrent playback logic.
"""
from pathlib import Path
from nagisa.reconciler.arrival import PreservationError


class PrivatePlexEpisodeGuard:
    FIELDS=('ratingKey','guid','parentGuid','grandparentGuid','parentIndex',
            'index','viewCount','viewOffset','lastViewedAt')

    def __init__(self,sections,presentation_roots,source_roots):
        self.sections=tuple(sections)
        self.roots=tuple(Path(p).resolve(strict=True) for p in presentation_roots)
        self.sources=tuple(Path(p).resolve(strict=True) for p in source_roots)
        if not self.sections or len(set(self.sections))!=len(self.sections) or not self.roots or not self.sources:
            raise ValueError('distinct sections and explicit roots required')

    def capture(self,section_items,*,approved_sources=None,baseline_keys=None,new_sources=()):
        result={}
        try:
            if set(section_items)!=set(self.sections):
                raise PreservationError('complete configured library reads required')
            approved=None
            new_stamps={}
            if approved_sources is not None:
                if not approved_sources:raise PreservationError('exact approved originals required')
                approved={}
                for candidate in approved_sources:
                    source=Path(candidate)
                    if not source.is_absolute() or '..' in source.parts or source.resolve(strict=True)!=source or not any(source.is_relative_to(root) for root in self.sources) or not source.is_file():
                        raise PreservationError('approved original outside protected sources')
                    info=source.stat()
                    stamp=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
                    if info.st_ino<=0 or stamp in approved:
                        raise PreservationError('ambiguous approved original identity')
                    approved[stamp]=source
            for candidate in new_sources:
                source=Path(candidate)
                if not source.is_absolute() or '..' in source.parts or source.resolve(strict=True)!=source or not any(source.is_relative_to(root) for root in self.sources) or not source.is_file():
                    raise PreservationError('expected arrival outside protected sources')
                info=source.stat()
                stamp=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
                if info.st_ino<=0:raise PreservationError('expected arrival identity unavailable')
                if stamp in new_stamps:raise PreservationError('ambiguous expected arrival identity')
                new_stamps[stamp]=source
            for section in self.sections:
                for item in section_items[section]:
                    if item['type']!='episode' or item.get('deletedAt') or int(item['librarySectionID'])!=section:
                        raise PreservationError('active episode from expected library required')
                    if any(item.get(field) is None for field in self.FIELDS[:6]):
                        raise PreservationError('native episode identity incomplete')
                    key=(section,str(item['ratingKey']))
                    if key in result:raise PreservationError('duplicate native episode key')
                    parts=set()
                    for media in item['Media']:
                        for part in media['Part']:
                            path=Path(part['file'])
                            if not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(root) for root in self.roots):
                                raise PreservationError('episode part outside approved presentation')
                            resolved=path.resolve(strict=True)
                            if part.get('accessible') is False or not resolved.is_file():
                                raise PreservationError('episode source unavailable or outside approved sources')
                            stat=resolved.stat()
                            stamp=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
                            if baseline_keys is not None and key not in baseline_keys:
                                # A newly created card must use this exact
                                # arrival, even when Plex uses a bind alias.
                                if stamp not in new_stamps:raise PreservationError('new episode is not the expected arrival')
                                source=new_stamps[stamp]
                            elif approved is None:
                                source=resolved
                                if not any(source.is_relative_to(root) for root in self.sources):
                                    raise PreservationError('episode source unavailable or outside approved sources')
                            else:
                                source=approved.get(stamp)
                                if source is None:raise PreservationError('episode part is not an approved original')
                            if int(part['size'])!=stat.st_size:raise PreservationError('episode source size mismatch')
                            parts.add((str(source),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
                    if not parts:raise PreservationError('hydrated accessible episode parts required')
                    result[key]=(tuple(item.get(field) for field in self.FIELDS),tuple(sorted(parts)))
            return result
        except (KeyError,ValueError,TypeError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('native episode preservation could not be proven') from error

    def verify(self,before,section_items,*,approved_sources=None,new_sources=()):
        if approved_sources is None and before:
            try:approved_sources={part[0] for value in before.values() for part in value[1]}
            except (KeyError,IndexError,TypeError,ValueError) as error:
                raise PreservationError('invalid native preservation baseline') from error
        after=self.capture(section_items,approved_sources=approved_sources,baseline_keys=set(before),new_sources=new_sources)
        if any(after.get(key)!=value for key,value in before.items()):
            raise PreservationError('existing Plex episode identity, watch state or source lost')
        return after


class PrivatePlexOwnerPreservation:
    """Durable baseline for the approved idle private arrival checks.

    Exact watch comparison is conservative; concurrent playback and atomic
    stock cleanup are not qualified by this observer.
    """
    def __init__(self,reader,guard):
        if not callable(reader):raise ValueError('complete native owner reader required')
        self.reader=reader;self.guard=guard

    def capture(self,owner,*,approved_sources=None,baseline_keys=None,new_source=None,new_episode_id=None):
        import json
        if type(owner) is not int or owner<=0:raise PreservationError('positive native owner required')
        return self.from_capture(owner,self.guard.capture(self.reader(owner),approved_sources=approved_sources,baseline_keys=baseline_keys,new_sources=(new_source,) if new_source is not None else ()),new_source=new_source,new_episode_id=new_episode_id)

    @staticmethod
    def from_capture(owner,captured,*,new_source=None,new_episode_id=None):
        import json
        payload={'schema':1,'owner':owner,'episodes':[
            {'section':section,'key':key,'evidence':value}
            for (section,key),value in sorted(captured.items())]}
        if new_source is not None:payload['new_source']=str(new_source)
        if new_episode_id is not None:
            if type(new_episode_id) is not int or new_episode_id<=0:raise PreservationError('positive expected episode required')
            payload['new_episode_id']=new_episode_id
        return json.loads(json.dumps(payload,allow_nan=False))

    def _items(self,snapshot):
        if not isinstance(snapshot,dict) or not {'schema','owner','episodes'}<=set(snapshot) or not set(snapshot)<={'schema','owner','episodes','new_source','new_episode_id'} or ('new_source' in snapshot)!=('new_episode_id' in snapshot) or type(snapshot['schema']) is not int or snapshot['schema']!=1 or type(snapshot['owner']) is not int or snapshot['owner']<=0 or not isinstance(snapshot['episodes'],list):
            raise PreservationError('native baseline missing or malformed')
        result={}
        for row in snapshot['episodes']:
            if not isinstance(row,dict) or set(row)!= {'section','key','evidence'} or type(row['section']) is not int or row['section'] not in self.guard.sections or not isinstance(row['key'],str) or not row['key']:
                raise PreservationError('native baseline episode malformed')
            key=(row['section'],row['key']);value=row['evidence']
            if key in result or not isinstance(value,list) or len(value)!=2 or not isinstance(value[0],list) or len(value[0])!=len(self.guard.FIELDS) or str(value[0][0])!=row['key'] or not isinstance(value[1],list) or not value[1]:
                raise PreservationError('native baseline evidence malformed')
            result[key]=value
        return result

    def validate(self,snapshot,owner):
        if not isinstance(snapshot,dict) or type(snapshot.get('owner')) is not int or snapshot['owner']!=owner:
            raise PreservationError('qualified native owner snapshot required')
        self._items(snapshot)

    def verify(self,before):
        try:
            expected=self._items(before)
            approved_sources={part[0] for value in expected.values() for part in value[1]}
            new_source=before.get('new_source')
            new_episode_id=before.get('new_episode_id')
            after=self._items(self.capture(before['owner'],approved_sources=approved_sources or None,baseline_keys=set(expected),new_source=new_source,new_episode_id=new_episode_id))
            new_guid=f'tv.plex.agents.custom.shoko://episode/e{new_episode_id}' if type(new_episode_id) is int and new_episode_id>0 else None
            new_parent=f'tv.plex.agents.custom.shoko://show/{before["owner"]}'
            for key,value in after.items():
                if key not in expected and (new_guid is None or value[0][1]!=new_guid or value[0][3]!=new_parent):
                    raise PreservationError('new native Plex episode identity differs from expected arrival')
            for key,value in expected.items():
                current=after.get(key)
                if current is None:
                    raise PreservationError(f'existing native Plex episode section={key[0]} key={key[1]} missing')
                if current!=value:
                    fields=[field for field,old,new in zip(self.guard.FIELDS,value[0],current[0]) if old!=new]
                    if value[1]!=current[1]:fields.append('source')
                    raise PreservationError(f'existing native Plex episode section={key[0]} key={key[1]} changed fields={",".join(fields)}')
            return after
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('durable native episode preservation unavailable') from error
