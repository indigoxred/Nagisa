"""Typed native Radarr imports; exact stock movie associations, never a mapper."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from nagisa.reconciler.arrival_identity import VerifiedArrival
from nagisa.reconciler.arrival_readers import StockOwnerRecordReader


class MovieIdentityBlocked(ValueError):
    """Native ownership changed or needs separately approved qualification."""


def _positive(value):
    if type(value) is int and value>0:return value
    if isinstance(value,str) and value.isdigit() and str(int(value))==value and int(value)>0:return int(value)
    raise ValueError('positive native identity required')


def _path(value):
    if not isinstance(value,str) or '\\' in value or '\x00' in value or value.startswith('//'):
        raise ValueError('canonical native path required')
    path=PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or str(path)!=value or value=='/':
        raise ValueError('canonical native path required')
    return path


@dataclass(frozen=True)
class MovieImportHistoryEvent:
    history_id:int
    movie_id:int
    radarr_file_id:int
    download_id:str
    imported_path:str
    dropped_path:str
    # Native Radarr import history may not retain size. None never claims a
    # historical measurement; current ownership and physical size are read later.
    source_size:int|None=None


class RadarrArrivalClient:
    def __init__(self,transport,import_root):
        self.transport=transport;self.import_root=_path(import_root)

    def compatibility(self):
        payload=self.transport.request_json('GET','/api/v3/system/status')
        version=payload.get('version') if isinstance(payload,dict) else None
        return version=='6.4.4.10685',version,'qualified' if version=='6.4.4.10685' else 'unsupported-radarr-version'

    def import_history_after(self,watermark):
        if type(watermark) is not int or watermark<0:raise ValueError('nonnegative Radarr frontier required')
        events=[];seen=set();previous=None;total=None
        for page in range(1,101):
            payload=self.transport.request_json('GET',f'/api/v3/history?page={page}&pageSize=250&sortKey=id&sortDirection=descending')
            if not isinstance(payload,dict) or type(payload.get('totalRecords')) is not int or payload['totalRecords']<0 or not isinstance(payload.get('records'),list):
                raise ValueError('incomplete Radarr history')
            if total is not None and total!=payload['totalRecords']:raise ValueError('Radarr history changed during pagination')
            total=payload['totalRecords'];records=payload['records']
            if len(records)>250 or (page-1)*250+len(records)>total:raise ValueError('invalid Radarr history page')
            for row in records:
                if not isinstance(row,dict):raise ValueError('malformed Radarr history')
                identity=_positive(row.get('id'))
                if identity in seen or previous is not None and identity>=previous:raise ValueError('unstable Radarr history order')
                seen.add(identity);previous=identity
                if identity<=watermark:return sorted(events,key=lambda e:e.history_id)
                if row.get('eventType')!='downloadFolderImported':continue
                data=row.get('data')
                if not isinstance(data,dict):raise ValueError('missing Radarr import evidence')
                imported=_path(data.get('importedPath'))
                if not imported.is_relative_to(self.import_root):continue
                if imported==self.import_root:raise ValueError('import is not a file path')
                movie=_positive(row.get('movieId'));file_id=_positive(data.get('fileId'))
                dropped=_path(data.get('droppedPath'))
                size=_positive(data['size']) if 'size' in data else None
                download=row.get('downloadId','')
                if not isinstance(download,str) or len(download)>256 or any(ord(c)<32 for c in download):raise ValueError('invalid native download identity')
                events.append(MovieImportHistoryEvent(identity,movie,file_id,download,str(imported),str(dropped),size))
            if page*250>=total:return sorted(events,key=lambda e:e.history_id)
            if len(records)!=250:raise ValueError('incomplete Radarr history window')
        raise ValueError('Radarr history bound exceeded')


class StockMovieArrivalIdentityReader:
    def __init__(self,radarr,shoko,import_root,source_root,managed_roots):
        self.radarr=radarr;self.shoko=shoko;self.import_root=_path(import_root)
        self.source_root=Path(source_root).resolve(strict=True)
        if not self.source_root.is_dir():raise ValueError('movie source directory required')
        self.records=StockOwnerRecordReader(shoko,managed_roots)

    def __call__(self,event):
        try:
            if not isinstance(event,MovieImportHistoryEvent):raise ValueError('typed Radarr import required')
            for value in (event.history_id,event.movie_id,event.radarr_file_id):
                if type(value) is not int or value<=0:raise ValueError('invalid Radarr import identity')
            if event.source_size is not None and (type(event.source_size) is not int or event.source_size<=0):raise ValueError('invalid recorded import size')
            movie=self.radarr.request_json('GET',f'/api/v3/movie/{event.movie_id}')
            file=self.radarr.request_json('GET',f'/api/v3/moviefile/{event.radarr_file_id}')
            if not isinstance(movie,dict) or movie.get('id')!=event.movie_id or movie.get('movieFileId')!=event.radarr_file_id:
                raise MovieIdentityBlocked('native movie association differs')
            if not isinstance(file,dict) or any(file.get(k)!=v for k,v in [('id',event.radarr_file_id),('movieId',event.movie_id),('path',event.imported_path)]):
                raise MovieIdentityBlocked('native moviefile association differs')
            size=file.get('size')
            if type(size) is not int or size<=0:raise ValueError('positive current moviefile size required')
            if event.source_size is not None and event.source_size!=size:raise MovieIdentityBlocked('recorded import size differs from retained file')
            imported=_path(event.imported_path);movie_path=_path(movie['path'])
            if not movie_path.is_relative_to(self.import_root) or not imported.is_relative_to(movie_path):raise ValueError('native movie root differs')
            relative=imported.relative_to(self.import_root)
            source=(self.source_root/relative.as_posix()).resolve(strict=True)
            if not relative.parts or not source.is_relative_to(self.source_root) or not source.is_file():raise ValueError('movie source outside configured root')
            before=source.stat()
            if before.st_size!=size:raise ValueError('movie source size differs')
            tmdb=_positive(movie['tmdbId'])
            series=self.shoko.request_json('GET',f'/api/v3/Tmdb/Movie/{tmdb}/Shoko/Series?page=1&pageSize=0')
            episodes=self.shoko.request_json('GET',f'/api/v3/Tmdb/Movie/{tmdb}/Shoko/Episode?page=1&pageSize=0')
            def members(rows,episode=False):
                if not isinstance(rows,list) or not 1<=len(rows)<=1000:raise ValueError('native movie membership unavailable')
                result=[]
                for row in rows:
                    identity=_positive(row['IDs']['ID'])
                    result.append((_positive(row['IDs']['ParentSeries']),identity) if episode else identity)
                if len(set(result))!=len(result):raise ValueError('ambiguous native movie membership')
                return set(result)
            owners=members(series);mapped=members(episodes,True)
            if len(owners)!=1 or len(mapped)!=1:raise MovieIdentityBlocked('ambiguous stock movie ownership requires review')
            native_file,native_episodes=self.records([source])[str(source)]
            if len(native_episodes)!=1:raise MovieIdentityBlocked('combined movie identity requires review')
            episode=native_episodes[0];owner=_positive(episode['IDs']['ParentSeries']);ident=_positive(episode['IDs']['ID'])
            if owner not in owners or (owner,ident) not in mapped or episode['AniDB']['Type']!='Episode':
                raise MovieIdentityBlocked('stock movie and file episode associations differ or need separate qualification')
            after=source.stat();stamp=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
            if stamp!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('movie source changed during recognition read')
            return VerifiedArrival(event.history_id,native_file['ID'],owner,ident,source,stamp)
        except (KeyError,TypeError,AttributeError,OSError,RuntimeError) as error:
            raise ValueError('movie source/identity evidence unavailable') from error
