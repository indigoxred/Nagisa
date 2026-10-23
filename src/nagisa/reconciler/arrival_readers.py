"""Bounded stock API reads for arrival delivery; no identity/numbering writes."""
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode


class StockArrivalIdentityReader:
    def __init__(self,sonarr,shoko,gate):
        self.sonarr=sonarr;self.shoko=shoko;self.gate=gate

    def __call__(self,event):
        file=self.sonarr.request_json('GET',f'/api/v3/episodefile/{event.sonarr_file_id}')
        episode=self.sonarr.request_json('GET',f'/api/v3/episode/{event.episode_id}')
        series=self.sonarr.request_json('GET',f'/api/v3/series/{event.series_id}')
        if not isinstance(series,dict) or series.get('id')!=event.series_id or type(series.get('tmdbId')) is not int or series['tmdbId']<=0:
            raise ValueError('requested show identity unavailable')
        members=self.shoko.request_json('GET',f"/api/v3/Tmdb/Show/{series['tmdbId']}/Shoko/Series?page=1&pageSize=0")
        if not isinstance(members,list) or not members or len(members)>1000:raise ValueError('stock show membership unavailable or excessive')
        try:
            owners=[row['IDs']['ID'] for row in members]
            if any(type(owner) is not int or owner<=0 for owner in owners) or len(set(owners))!=len(owners):raise ValueError('ambiguous stock membership')
        except (KeyError,TypeError):raise ValueError('malformed stock membership') from None
        # Name narrows the API lookup only; exact registered source ownership
        # determines identity. Multiple basename matches are never guessed.
        name=quote(PurePosixPath(event.imported_path).name,safe='')
        candidates=self.shoko.request_json('GET',f'/api/v3/File/PathEndsWith/{name}?includeXRefs=true&limit=100')
        if not isinstance(candidates,list) or len(candidates)>=100:raise ValueError('incomplete file lookup')
        matches=[]
        relative=PurePosixPath(event.imported_path).relative_to(self.gate.sonarr_root)
        source=(self.gate.source_root/str(relative)).resolve(strict=True)
        for candidate in candidates:
            if not isinstance(candidate,dict) or type(candidate.get('ID')) is not int or candidate['ID']<=0:raise ValueError('malformed stock file')
            exact=False
            try:
                for location in candidate['Locations']:
                    root=self.gate.managed_roots.get(location['ManagedFolderID'])
                    if root is None:continue
                    raw=location['RelativePath']
                    if not isinstance(raw,str) or '\\' in raw or '\x00' in raw or raw.startswith('//'):raise ValueError('invalid registered source')
                    path=PurePosixPath(raw.lstrip('/'))
                    if not path.parts or '..' in path.parts:raise ValueError('invalid registered source')
                    if (root/str(path)).resolve()==source:exact=True
            except (KeyError,TypeError):raise ValueError('incomplete registered source') from None
            if not exact:continue
            episodes=self.shoko.request_json('GET',f"/api/v3/File/{candidate['ID']}/Episode?includeXRefs=true&includeDataFrom=AniDB")
            matches.append(self.gate.verify(event,file,candidate,episodes,set(owners),episode))
        if len(matches)!=1:raise ValueError('exact retained source identity is missing or ambiguous')
        return matches[0]


class StockArrivalVisibilityReader:
    def __init__(self,plex,gate):self.plex=plex;self.gate=gate

    def __call__(self,verified):
        guid=f'tv.plex.agents.custom.shoko://episode/e{verified.shoko_episode_id}'
        sections={}
        for section in self.gate.sections:
            items=[];start=0;seen=set()
            for _ in range(100):
                query=urlencode({'type':4,'guid':guid,'includeGuids':1,'X-Plex-Container-Start':start,'X-Plex-Container-Size':100})
                payload=self.plex.request_json('GET',f'/library/sections/{section}/all?{query}')
                container=payload.get('MediaContainer') if isinstance(payload,dict) else None
                if not isinstance(container,dict) or not isinstance(container.get('Metadata',[]),list):raise ValueError('malformed Plex library read')
                rows=container.get('Metadata',[])
                size=container.get('size');total=container.get('totalSize',size)
                if type(size) is not int or type(total) is not int or size>100 or size!=len(rows) or min(size,total)<0 or start+size>total:raise ValueError('invalid Plex page')
                for row in rows:
                    if not isinstance(row,dict):raise ValueError('malformed Plex item')
                    if row.get('guid')!=guid:continue
                    key=str(row.get('ratingKey',''))
                    if not key.isdigit() or int(key)<=0 or key in seen:raise ValueError('invalid repeated Plex identity')
                    seen.add(key)
                    detail=self.plex.request_json('GET',f'/library/metadata/{key}?includeGuids=1')
                    body=detail.get('MediaContainer') if isinstance(detail,dict) else None
                    metadata=body.get('Metadata') if isinstance(body,dict) else None
                    if not isinstance(metadata,list) or len(metadata)!=1 or not isinstance(metadata[0],dict) or str(metadata[0].get('ratingKey'))!=key:raise ValueError('incomplete Plex detail')
                    items.extend(metadata)
                if start+size>=total:break
                if size==0:raise ValueError('Plex page did not advance')
                start+=size
            else:raise ValueError('Plex pagination exceeded bound')
            sections[section]=items
        return self.gate.visible(verified,sections)


class StockOwnerRecordReader:
    """GET-only exact-source records for existing-owner preservation.

    This is association evidence, not proof of programme content, and does not
    qualify an owner without the independent inventory/Plex/configuration gates.
    """
    def __init__(self,shoko,managed_roots):
        from pathlib import Path
        self.shoko=shoko
        self.roots={key:Path(root).resolve(strict=True) for key,root in managed_roots.items()}

    def __call__(self,sources):
        from pathlib import Path
        from nagisa.reconciler.arrival import PreservationError
        try:
            result={}
            for count,raw_source in enumerate(sources,1):
                if count>10000:raise PreservationError('owner source bound exceeded')
                source=Path(raw_source)
                if not source.is_absolute() or '..' in source.parts or source.resolve(strict=True)!=source or not source.is_file():
                    raise PreservationError('owner source inaccessible or not canonical')
                if not any(source.is_relative_to(root) for root in self.roots.values()):
                    raise PreservationError('owner source outside managed roots')
                if str(source) in result:continue
                before=source.stat()
                name=quote(source.name,safe='')
                candidates=self.shoko.request_json('GET',f'/api/v3/File/PathEndsWith/{name}?includeXRefs=true&limit=100')
                if not isinstance(candidates,list) or len(candidates)>=100:
                    raise PreservationError('native owner file lookup incomplete')
                matches=[];seen=set()
                for candidate in candidates:
                    if not isinstance(candidate,dict) or type(candidate.get('ID')) is not int or candidate['ID']<=0 or candidate['ID'] in seen:
                        raise PreservationError('native owner file identity malformed or repeated')
                    seen.add(candidate['ID'])
                    if not isinstance(candidate.get('Locations'),list) or not candidate['Locations']:
                        raise PreservationError('native owner locations missing')
                    exact=False
                    for location in candidate['Locations']:
                        root=self.roots.get(location['ManagedFolderID'])
                        if root is None:continue
                        raw=location['RelativePath']
                        if not isinstance(raw,str) or '\\' in raw or '\x00' in raw or raw.startswith('//'):
                            raise PreservationError('invalid native owner location')
                        relative=PurePosixPath(raw.lstrip('/'))
                        if not relative.parts or '..' in relative.parts:
                            raise PreservationError('native owner location traversal')
                        registered=root/str(relative)
                        if not registered.resolve(strict=False).is_relative_to(root):
                            raise PreservationError('native owner location escapes root')
                        try:target=registered.resolve(strict=True)
                        except FileNotFoundError:continue
                        if not target.is_relative_to(root):raise PreservationError('native owner target escapes root')
                        if target==source:exact=True
                    if exact:matches.append(candidate)
                if len(matches)!=1:raise PreservationError('exact owner source missing or ambiguous')
                file=matches[0]
                if type(file.get('Size')) is not int or file['Size']!=before.st_size:
                    raise PreservationError('native owner source size mismatch')
                episodes=self.shoko.request_json('GET',f"/api/v3/File/{file['ID']}/Episode?includeXRefs=true&includeDataFrom=AniDB")
                if not isinstance(episodes,list) or not episodes or len(episodes)>1000:
                    raise PreservationError('native owner episode associations missing or excessive')
                after=source.stat()
                if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                    raise PreservationError('owner source changed during native read')
                result[str(source)]=(file,episodes)
            return result
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('native owner read failed or incomplete') from error


class StockPlexOwnerReader:
    """Bounded complete native owner reads; no guessed coordinates or refreshes."""
    def __init__(self,plex,sections):
        self.plex=plex;self.sections=tuple(sections)
        if not self.sections or len(set(self.sections))!=len(self.sections) or any(type(s) is not int or s<=0 for s in self.sections):
            raise ValueError('distinct positive Plex sections required')

    def __call__(self,owner):
        import re,time
        from nagisa.reconciler.arrival import PreservationError
        try:
            if type(owner) is not int or owner<=0:raise PreservationError('positive native owner required')
            deadline=time.monotonic()+120;calls=0
            def get(path):
                nonlocal calls
                calls+=1
                if calls>1200 or time.monotonic()>=deadline:raise PreservationError('native owner read budget exceeded')
                payload=self.plex.request_json('GET',path)
                body=payload.get('MediaContainer') if isinstance(payload,dict) else None
                if not isinstance(body,dict):raise PreservationError('native Plex container missing')
                return body
            def key(value):
                value=str(value)
                if not value.isdigit() or int(value)<=0:raise PreservationError('invalid native Plex key')
                return value
            def page(path):
                result=[];start=0;total=None;seen=set()
                for count in range(100):
                    body=get(path+('&' if '?' in path else '?')+urlencode({'X-Plex-Container-Start':start,'X-Plex-Container-Size':200}))
                    rows=body.get('Metadata',[]);size=body.get('size');n=body.get('totalSize',size)
                    if not isinstance(rows,list) or type(size) is not int or type(n) is not int or size!=len(rows) or not 0<=size<=200 or not start+size<=n<=20000:
                        raise PreservationError('native owner page incomplete')
                    if total is not None and total!=n:raise PreservationError('native owner total changed during pagination')
                    if 'offset' in body and body['offset']!=start:raise PreservationError('native owner offset differs')
                    total=n
                    for row in rows:
                        if not isinstance(row,dict):raise PreservationError('malformed native owner row')
                        identity=key(row['ratingKey'])
                        if identity in seen:raise PreservationError('repeated native owner key')
                        seen.add(identity);result.append(row)
                    start+=size
                    if start==total:return result
                    if not size:raise PreservationError('native owner page did not advance')
                raise PreservationError('native owner pagination bound exceeded')
            def detail(identity,kind,section):
                identity=key(identity)
                rows=get('/library/metadata/'+identity+'?includeGuids=1&skipRefresh=1').get('Metadata')
                if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise PreservationError('native owner detail incomplete')
                item=rows[0]
                if key(item['ratingKey'])!=identity or item['type']!=kind or int(item['librarySectionID'])!=section or item.get('deletedAt'):
                    raise PreservationError('native owner detail identity differs')
                return item
            show_guid=f'tv.plex.agents.custom.shoko://show/{owner}'
            sections={}
            for section in self.sections:
                shows=page(f'/library/sections/{section}/all?type=2&includeGuids=1')
                matches=[row for row in shows if row.get('guid')==show_guid]
                if len(matches)>1:raise PreservationError('native owner show ambiguous')
                if not matches:sections[section]=[];continue
                show=detail(matches[0]['ratingKey'],'show',section)
                if show.get('guid')!=show_guid:raise PreservationError('native owner show changed')
                rows=page('/library/metadata/'+key(show['ratingKey'])+'/allLeaves?includeGuids=1')
                items=[];seasons={}
                for row in rows:
                    item=detail(row['ratingKey'],'episode',section)
                    if key(item['grandparentRatingKey'])!=key(show['ratingKey']):raise PreservationError('native episode belongs to other owner')
                    season_key=key(item['parentRatingKey'])
                    if season_key not in seasons:seasons[season_key]=detail(season_key,'season',section)
                    season=seasons[season_key]
                    if key(season['parentRatingKey'])!=key(show['ratingKey']) or type(season['index']) is not int or season['index']<0 or type(item['parentIndex']) is not int or item['parentIndex']!=season['index'] or type(item['index']) is not int or item['index']<0:
                        raise PreservationError('native episode season association differs')
                    for field,value in (('parentGuid',season['guid']),('grandparentGuid',show['guid'])):
                        if not isinstance(value,str) or not value or (item.get(field) and item[field]!=value):raise PreservationError('native ancestor GUID differs')
                        item[field]=value
                    if not isinstance(item.get('guid'),str) or not re.fullmatch(r'tv\.plex\.agents\.custom\.shoko://episode/e([1-9][0-9]*)(?:p[1-9][0-9]*)?',item['guid']):
                        raise PreservationError('unqualified native episode GUID')
                    if not isinstance(item.get('Media'),list) or not item['Media']:raise PreservationError('native episode media missing')
                    for media in item['Media']:
                        if not isinstance(media,dict) or not isinstance(media.get('Part'),list) or not media['Part']:raise PreservationError('native episode parts missing')
                        for part in media['Part']:
                            if not isinstance(part,dict) or not isinstance(part.get('file'),str) or not part['file'] or type(part.get('size')) not in (int,str) or int(part['size'])<=0:
                                raise PreservationError('native episode part incomplete')
                    items.append(item)
                sections[section]=items
            return sections
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('native owner consumer read failed or incomplete') from error
