"""Native server/version and physical output scope checks for TV arrival only."""
from pathlib import Path
import os
from nagisa.reconciler.arrival import PreservationError,NotDispatched

class StockArrivalBoundary:
    def __init__(self,shoko,allowed_versions,generated_roots,vfs_root,*,movie_generation_mode='Disabled',movie_vfs_root=None,movie_generated_roots=()):
        self.shoko=shoko;self.allowed=frozenset(allowed_versions)
        self.roots=frozenset(map(Path,generated_roots));self.vfs=vfs_root
        self.movie_mode=movie_generation_mode;self.movie_vfs=movie_vfs_root
        self.movie_roots=frozenset(map(Path,movie_generated_roots))
        if not self.allowed:raise ValueError('explicit approved Shoko versions required')
        if self.movie_mode not in ('Disabled','EnabledMaintain'):
            raise ValueError('explicit supported movie protection mode required')
        if self.movie_mode=='Disabled':
            if self.movie_vfs is not None or self.movie_roots:raise ValueError('disabled movie scope must remain empty')
        elif not isinstance(self.movie_vfs,str) or not self.movie_vfs or '\x00' in self.movie_vfs or Path(self.movie_vfs).name!=self.movie_vfs or self.movie_vfs in ('.','..') or self.movie_vfs.casefold()==self.vfs.casefold() or not self.movie_roots or any(root.name!=self.movie_vfs for root in self.movie_roots):
            raise ValueError('explicit distinct maintained movie output roots required')

    def check(self):
        try:
            version=self.shoko.request_json('GET','/api/v3/Init/Version')['Server']['Version']
            status=self.shoko.request_json('GET','/api/v3/Init/Status')
            if not isinstance(version,str) or version not in self.allowed or status['State']!='Started' or status['DatabaseBlocked']['Blocked'] is not False:
                raise PreservationError('arrival Shoko version/readiness is not approved')
            advanced=self.shoko.request_json('GET','/api/plugin/ShokoRelay/config')['payload']['Advanced']
            mode=advanced['MovieGenerationMode']
            expected_number=0 if self.movie_mode=='Disabled' else 1
            qualified_mode=mode==self.movie_mode or (type(mode) in (int,float) and mode==expected_number)
            if advanced['VfsRootPath']!=self.vfs or not qualified_mode:
                raise PreservationError('arrival standard TV output scope is not qualified')
            if self.movie_mode=='EnabledMaintain' and advanced['MovieVfsRootPath']!=self.movie_vfs:
                raise PreservationError('maintained movie output root differs from protected scope')
            exclusions=advanced['ManagedFolderExclusions']
            if exclusions is None:exclusions=''
            if not isinstance(exclusions,str):raise PreservationError('native folder exclusions incomplete')
            excluded={s.strip().casefold() for s in exclusions.splitlines() if s.strip()}
            folders=self.shoko.request_json('GET','/api/v3/ManagedFolder')
            if not isinstance(folders,list) or not 1<=len(folders)<=32:raise PreservationError('complete bounded native managed folders required')
            seen=set();covered=set();movie_covered=set()
            for folder in folders:
                ident=folder['ID'];name=folder['Name'];raw=folder['Path'];kind=folder['DropFolderType']
                if type(ident) is not int or ident<=0 or ident in seen or not isinstance(name,str) or not isinstance(raw,str) or kind not in ('None','Source','Destination','Both'):
                    raise PreservationError('native managed-folder record incomplete')
                seen.add(ident);parent=Path(raw)
                # Same literal native mount paths are required. Source translation
                # cannot prove where the separate stock writer prunes links.
                if not parent.is_absolute() or '..' in parent.parts or parent==Path(parent.anchor) or parent.resolve(strict=True)!=parent or not parent.is_dir():
                    raise PreservationError('native managed output parent is inaccessible')
                output=parent/self.vfs
                enabled=kind!='Source' and str(ident).casefold() not in excluded and name.casefold() not in excluded
                exists=os.path.lexists(output)
                if exists:
                    if output.resolve(strict=True)!=output or not output.is_dir() or output not in self.roots:
                        raise PreservationError('stock prune output is outside protected roots')
                    covered.add(output)
                elif enabled:
                    raise PreservationError('enabled stock output is not protected')
                if self.movie_mode=='EnabledMaintain':
                    movie_output=parent/self.movie_vfs
                    if os.path.lexists(movie_output):
                        if movie_output.resolve(strict=True)!=movie_output or not movie_output.is_dir() or movie_output not in self.movie_roots:
                            raise PreservationError('stock movie output is outside protected roots')
                        movie_covered.add(movie_output)
                    elif enabled:
                        if movie_output not in self.movie_roots:
                            raise PreservationError('enabled stock movie output is not protected')
                        # Stock creates MovieVFS only when content exists. Its
                        # canonical configured parent remains protected now and
                        # after the first native movie output is created.
                        movie_covered.add(movie_output)
            if covered!=self.roots:raise PreservationError('configured roots differ from native output roots')
            if movie_covered!=self.movie_roots:raise PreservationError('configured movie roots differ from native output roots')
            return True
        except (KeyError,TypeError,ValueError,OSError,AttributeError,RuntimeError) as error:
            if isinstance(error,PreservationError):raise
            raise PreservationError('native arrival boundary read failed') from error

    def preflight(self,operation):
        def checked(*args,**kwargs):
            self.check();return operation(*args,**kwargs)
        return checked

    def dispatch(self,operation):
        def checked(*args,**kwargs):
            try:self.check()
            except PreservationError as error:raise NotDispatched('native boundary refused before stock dispatch') from error
            return operation(*args,**kwargs)
        return checked
