"""Read-only delivery evidence; the stock provider remains the sole mapper."""
from pathlib import Path


class PlexArrivalGate:
    def __init__(self,sections,presentation_roots):
        self.sections=tuple(sections)
        self.roots=tuple(Path(root).absolute() for root in presentation_roots)
        if len(self.sections)!=2 or len(set(self.sections))!=2 or any(type(s) is not int or s<=0 for s in self.sections) or not self.roots:
            raise ValueError('two distinct libraries and explicit presentation roots required')

    def visible(self,verified,section_items):
        expected=f'tv.plex.agents.custom.shoko://episode/e{verified.shoko_episode_id}'
        show=f'tv.plex.agents.custom.shoko://show/{verified.shoko_series_id}'
        try:
            source_info=verified.source.stat()
            source_stamp=(source_info.st_dev,source_info.st_ino,
                          source_info.st_size,source_info.st_mtime_ns)
            if not verified.source.is_file() or source_info.st_ino<=0 or source_stamp!=verified.source_stat:
                return False
            for section in self.sections:
                matches=[]
                for item in section_items.get(section,[]):
                    if item.get('type')!='episode' or item.get('guid')!=expected or item.get('grandparentGuid')!=show or item.get('deletedAt') or int(item.get('librarySectionID',0))!=section:
                        continue
                    parts=[part for media in item.get('Media',[])
                           for part in media.get('Part',[])]
                    if not parts:return False
                    for part in parts:
                        path=Path(part['file'])
                        if not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(root) for root in self.roots):return False
                        if part.get('accessible') is False or int(part.get('size',0))!=verified.source_stat[2] or not path.is_file():return False
                        info=path.stat()
                        if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=source_stamp:return False
                    matches.append(str(item['ratingKey']))
                if len(matches)!=1:return False
            return True
        except (KeyError,ValueError,TypeError,OSError,AttributeError):
            return False
