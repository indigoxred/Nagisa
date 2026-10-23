"""Read-only owner qualification for the pinned stock unmerged layout.

Consumes captured physical/source evidence and native associations. It neither
predicts destinations nor changes identities. No atomic preservation guarantee.
"""
from pathlib import Path,PurePosixPath
import hashlib
from nagisa.reconciler.arrival import PreservationError,SourceCoverageGuard


class OwnerAssociationGate:
    def __init__(self,presentation_roots,managed_roots):
        self.roots=tuple(Path(root).resolve(strict=True) for root in presentation_roots)
        self.managed={key:Path(root).resolve(strict=True) for key,root in managed_roots.items()}

    def qualify(self,owner,inventory,records,consumers,*,unmerged,subtitle_language_mappings=None):
        try:
            if type(owner) is not int or owner<=0 or unmerged is not True:
                raise PreservationError('explicit unmerged native owner required')
            owned={};video_rows=[];assets=[]
            for name,evidence in inventory.items():
                path=Path(name)
                if not path.is_absolute() or ".." in path.parts or not isinstance(evidence,list) or not evidence:
                    raise PreservationError("invalid protected inventory path/evidence")
                roots=[root for root in self.roots if path.is_relative_to(root)]
                if len(roots)!=1:raise PreservationError('inventory outside distinct configured roots')
                relative=path.relative_to(roots[0])
                if not relative.parts or relative.parts[0]!=str(owner):continue
                if path.parent.resolve(strict=False)!=path.parent or not path.parent.is_relative_to(roots[0]/str(owner)):
                    raise PreservationError("redirected or escaped owner destination")
                if evidence[0]=='sidecar-source':
                    assets.append((path,evidence));continue
                if evidence[0]!='video-source':raise PreservationError('existing owner asset consumption not qualified')
                if len(evidence)!=7 or evidence[1]!=str(roots[0]):raise PreservationError('incomplete protected source evidence')
                source=Path(evidence[2]);stat=source.stat()
                if not source.is_file() or source.resolve(strict=True)!=source or list((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))!=evidence[3:]:
                    raise PreservationError('protected source changed or inaccessible')
                if not any(source.is_relative_to(root) for root in self.managed.values()):
                    raise PreservationError('protected source outside managed roots')
                file,episodes=records[str(source)]
                if type(file['ID']) is not int or file['ID']<=0 or file['Size']!=stat.st_size:
                    raise PreservationError('native file identity incomplete')
                locations=[]
                for location in file['Locations']:
                    root=self.managed.get(location['ManagedFolderID'])
                    if root is None:continue
                    raw=location['RelativePath']
                    if not isinstance(raw,str) or '\\' in raw or '\x00' in raw or raw.startswith('//'):
                        raise PreservationError('invalid native location')
                    part=PurePosixPath(raw.lstrip('/'))
                    if not part.parts or '..' in part.parts:raise PreservationError('native location traversal')
                    registered=root/str(part)
                    if not registered.resolve(strict=False).is_relative_to(root):raise PreservationError('native location escapes managed root')
                    try:target=registered.resolve(strict=True)
                    except FileNotFoundError:continue
                    if not target.is_relative_to(root):raise PreservationError('native target escapes managed root')
                    locations.append(target)
                if source not in locations:raise PreservationError('native file does not identify protected source')
                if not isinstance(episodes,list) or not episodes:raise PreservationError('native file episode associations missing')
                ids=set()
                for episode in episodes:
                    ep=episode['IDs']['ID']
                    if type(ep) is not int or ep<=0 or ep in ids or type(episode['IDs']['ParentSeries']) is not int or episode['IDs']['ParentSeries']!=owner or episode['AniDB']['Type'] not in ('Episode','Special'):
                        raise PreservationError('ambiguous or crossover native owner associations')
                    ids.add(ep)
                if str(source) in owned and owned[str(source)]!=ids:raise PreservationError('inconsistent native source associations')
                owned[str(source)]=ids
                video_rows.append((path,source))
            for consumer in consumers:
                source=consumer['source']
                if type(consumer['owner']) is not int or type(consumer['episode']) is not int or min(consumer['owner'],consumer['episode'])<=0:
                    raise PreservationError('native Plex association identity incomplete')
                if consumer['owner']==owner and source not in owned:
                    raise PreservationError('native Plex owner references an uncaptured source')
                if source in owned and (consumer['owner']!=owner or consumer['episode'] not in owned[source]):
                    raise PreservationError('Plex/source association differs from native owner')
            if assets and subtitle_language_mappings!={}:raise PreservationError('subtitle conversion not qualified')
            for destination,evidence in assets:
                if len(evidence)!=8 or evidence[1] not in ('.ass','.ssa','.srt') or destination.suffix.lower()!=evidence[1]:
                    raise PreservationError('unsupported owner asset')
                source=Path(evidence[2]);stat=source.stat()
                if not source.is_file() or source.resolve(strict=True)!=source or stat.st_size>32*1024*1024 or list((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))!=evidence[3:7]:
                    raise PreservationError('subtitle source changed or inaccessible')
                if hashlib.sha256(source.read_bytes()).hexdigest()!=evidence[7]:raise PreservationError('subtitle bytes changed')
                matching_sources={video for path,video in video_rows if video.parent==source.parent and source.name.lower().startswith(video.stem.lower()+'.')}
                candidates=set(matching_sources)
                # Include current directory videos and native record sources:
                # unpublished overlapping stems must not be silently ignored.
                for count,video in enumerate(source.parent.iterdir(),1):
                    if count>10000:raise PreservationError('source directory qualification cap exceeded')
                    if video.suffix.lower() in SourceCoverageGuard.VIDEO_EXTENSIONS and video.is_file() and source.name.lower().startswith(video.stem.lower()+'.'):
                        candidates.add(video)
                for name in records:
                    video=Path(name)
                    if video.parent==source.parent and source.name.lower().startswith(video.stem.lower()+'.'):
                        candidates.add(video)
                if candidates!=matching_sources:raise PreservationError('unpublished subtitle source consumer ambiguous')
                if len(matching_sources)!=1:raise PreservationError('subtitle source consumer ambiguous')
                video=next(iter(matching_sources));suffix=source.name[len(video.stem):]
                matching_destinations=[path for path,target in video_rows if target==video and path.parent==destination.parent and destination.name.lower().startswith(path.stem.lower()+'.') and destination.name[len(path.stem):]==suffix]
                if len(matching_destinations)!=1:raise PreservationError('subtitle destination consumer ambiguous or changed')
                final=source.stat()
                if (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)!=(final.st_dev,final.st_ino,final.st_size,final.st_mtime_ns):raise PreservationError('subtitle changed during verification')
            return True
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('owner source/association evidence incomplete') from error
