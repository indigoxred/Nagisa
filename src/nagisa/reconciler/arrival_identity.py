"""Source/owner boundary for arrival delivery, not an episode matcher.

Consumes stock records. No hash establishes content truth here, no identity is
changed, and no Plex episode coordinates are invented. Combined/uncertain files
remain review items rather than being silently forced through delivery.
"""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


@dataclass(frozen=True)
class VerifiedArrival:
    history_id: int
    shoko_file_id: int
    shoko_series_id: int
    shoko_episode_id: int
    source: Path
    source_stat: tuple[int,int,int,int]
    source_sha1: str | None = None


class ArrivalIdentityGate:
    def __init__(self,sonarr_root,source_root,managed_roots):
        self.sonarr_root=PurePosixPath(sonarr_root)
        self.source_root=Path(source_root).resolve(strict=True)
        self.managed_roots={key:Path(value).resolve(strict=True) for key,value in managed_roots.items()}
        if not self.sonarr_root.is_absolute() or '..' in self.sonarr_root.parts or str(self.sonarr_root)=='/' or not self.source_root.is_dir():
            raise ValueError('explicit source mapping required')

    def verify(self,event,sonarr_file,shoko_file,episodes,stock_show_membership,sonarr_episode):
        try:
            if any(type(value) is not int or value<=0 for value in (event.history_id,event.series_id,event.episode_id,event.sonarr_file_id,event.source_size)):
                raise ValueError('invalid import identity')
            if any(sonarr_file[key]!=expected for key,expected in [('id',event.sonarr_file_id),('seriesId',event.series_id),('path',event.imported_path),('size',event.source_size)]):
                raise ValueError('retained Sonarr ownership does not match import')
            if any(sonarr_episode[key]!=expected for key,expected in [('id',event.episode_id),('seriesId',event.series_id),('episodeFileId',event.sonarr_file_id)]):
                raise ValueError('retained Sonarr episode association does not match import')
            imported=PurePosixPath(event.imported_path)
            relative=imported.relative_to(self.sonarr_root)
            if not relative.parts or '..' in relative.parts:
                raise ValueError('import escapes configured root')
            source=(self.source_root/str(relative)).resolve(strict=True)
            if not source.is_relative_to(self.source_root) or not source.is_file():
                raise ValueError('source inaccessible or outside root')
            info=source.stat()
            if info.st_size!=event.source_size or shoko_file['Size']!=event.source_size or type(shoko_file['ID']) is not int or shoko_file['ID']<=0:
                raise ValueError('source/file size mismatch')
            candidates=set()
            for location in shoko_file['Locations']:
                root=self.managed_roots.get(location['ManagedFolderID'])
                if root is None:continue
                raw=location['RelativePath']
                if not isinstance(raw,str) or '\\' in raw or '\x00' in raw or raw.startswith('//'):
                    raise ValueError('invalid stock relative path')
                path=PurePosixPath(raw.lstrip('/'))
                if not path.parts or '..' in path.parts:
                    raise ValueError('stock relative path traversal')
                registered=root/str(path)
                # Resolve existing symlink parents even when the final file is
                # missing; missing alternates must not conceal an escaping link.
                if not registered.resolve(strict=False).is_relative_to(root):
                    raise ValueError('stock source escapes managed root')
                try:
                    target=registered.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if not target.is_relative_to(root):
                    raise ValueError('stock source escapes managed root')
                candidates.add(target)
            if source not in candidates:
                raise ValueError('Shoko location does not identify retained source')
            if not isinstance(episodes,list) or len(episodes)!=1:
                raise ValueError('combined or missing episode identity requires review')
            episode=episodes[0]
            owner=episode['IDs']['ParentSeries']
            episode_id=episode['IDs']['ID']
            if type(owner) is not int or type(episode_id) is not int or min(owner,episode_id)<=0 or owner not in stock_show_membership:
                raise ValueError('stock show membership does not match requested show')
            if episode['AniDB']['Type']!='Episode':
                raise ValueError('special or extra arrival requires separate qualification')
            hashes=shoko_file.get('Hashes',[])
            sha1=None
            if isinstance(hashes,list):
                candidates=[row.get('Value') for row in hashes if isinstance(row,dict) and row.get('Type')=='SHA1']
                if len(candidates)==1 and isinstance(candidates[0],str) and re.fullmatch(r'[0-9a-fA-F]{40}',candidates[0]):
                    sha1=candidates[0].lower()
            return VerifiedArrival(event.history_id,shoko_file['ID'],owner,episode_id,source,(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns),sha1)
        except (KeyError,TypeError,OSError,AttributeError) as exc:
            raise ValueError('arrival source/owner evidence incomplete') from exc
