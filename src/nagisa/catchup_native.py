"""Conservative missing-anime adapter over supported stock Sonarr APIs.

Release bodies are opaque private evidence, never status/log resources. A push
approval is a decision, not a download receipt. The caller journals intent first.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from nagisa.http import TransportError


class CatchupNativeError(RuntimeError):
    """Only fixed, credential-free messages cross the service boundary."""


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0, maximum=2147483647):
    if type(value) is not int or not minimum<=value<=maximum:
        raise CatchupNativeError('malformed-native-evidence')
    return value


def _date(value):
    if not isinstance(value,str) or len(value)>64:
        raise CatchupNativeError('malformed-native-date')
    try: parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError: raise CatchupNativeError('malformed-native-date') from None
    if parsed.tzinfo is None: raise CatchupNativeError('malformed-native-date')
    return parsed.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class NativeCandidate:
    episode_id: int
    series_id: int
    fingerprint: str
    _private_json: str=field(repr=False)
    _issuer: object=field(repr=False,compare=False)

    def summary(self):
        payload=json.loads(self._private_json)['payload']
        return {'episode_id':self.episode_id,'series_id':self.series_id,
                'fingerprint':self.fingerprint,'title_sha256':_hash(payload['title']),
                'publish_date':payload['publishDate']}


class NativeCatchupAdapter:
    def __init__(self,sonarr_client_or_transport,*,allowed_versions=None,clock=None,maximum_records=10000):
        self.transport=getattr(sonarr_client_or_transport,'transport',sonarr_client_or_transport)
        versions=allowed_versions if allowed_versions is not None else getattr(sonarr_client_or_transport,'allowed_versions',())
        if isinstance(versions,(str,bytes)):raise ValueError('Explicit supported Sonarr versions required')
        self.allowed_versions=frozenset(versions)
        if not self.allowed_versions or any(not isinstance(v,str) or len(v)>64 for v in self.allowed_versions):
            raise ValueError('Explicit supported Sonarr versions required')
        if not callable(getattr(self.transport,'request_json',None)):raise ValueError('Sonarr JSON transport required')
        self.maximum_records=_integer(maximum_records,1,100000)
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));self._issuer=object()

    def binding_summary(self):
        return {'origin_sha256':_hash(getattr(self.transport,'origin','private-injected-transport')),
                'allowed_versions':sorted(self.allowed_versions)}

    def _get(self,route):return self.transport.request_json('GET','/api/v3/'+route)

    def _rows(self,value):
        if not isinstance(value,list) or len(value)>self.maximum_records or any(not isinstance(row,dict) for row in value):
            raise CatchupNativeError('incomplete-native-evidence')
        return value

    def _compatible(self):
        status=self._get('system/status')
        if not isinstance(status,dict) or status.get('version') not in self.allowed_versions:
            raise CatchupNativeError('unsupported-sonarr-version')
        return {'version':status['version'],**self.binding_summary()}

    def _paged(self,route):
        rows=[];seen=set();total=None
        for page in range(1,(self.maximum_records+99)//100+2):
            body=self._get(f'{route}&page={page}&pageSize=100')
            if not isinstance(body,dict):raise CatchupNativeError('incomplete-native-pagination')
            count=_integer(body.get('totalRecords'),0,self.maximum_records)
            if body.get('page')!=page or body.get('pageSize')!=100 or (total is not None and total!=count):
                raise CatchupNativeError('unstable-native-pagination')
            total=count;batch=self._rows(body.get('records'))
            if len(batch)>100 or (len(rows)+len(batch)<total and len(batch)!=100):
                raise CatchupNativeError('incomplete-native-pagination')
            for row in batch:
                identity=_integer(row.get('id'),1)
                if identity in seen:raise CatchupNativeError('unstable-native-pagination')
                seen.add(identity);rows.append(row)
            if len(rows)==total:return rows
            if len(rows)>total:raise CatchupNativeError('incomplete-native-pagination')
        raise CatchupNativeError('native-page-bound-exceeded')

    def scan_missing(self):
        try:
            self._compatible()
            rows=self._paged('wanted/missing?monitored=true&includeSeries=true&sortKey=episodes.airDateUtc&sortDirection=ascending')
            for row in rows:
                if row.get('monitored') is not True or row.get('hasFile') is not False:
                    raise CatchupNativeError('unstable-missing-frontier')
                if not isinstance(row.get('series'),dict) or row['series'].get('seriesType') not in {'anime','standard','daily'}:
                    raise CatchupNativeError('missing-series-evidence-incomplete')
                if row['series'].get('monitored') is not True:raise CatchupNativeError('unstable-missing-frontier')
            return [row['id'] for row in rows if row['series']['seriesType']=='anime']
        except (TransportError,KeyError,TypeError,ValueError) as error:
            if isinstance(error,CatchupNativeError):raise
            raise CatchupNativeError('native-missing-read-unavailable') from None

    def _context(self,episode_id):
        compatibility=self._compatible();episode=self._get(f'episode/{episode_id}')
        if not isinstance(episode,dict) or episode.get('id')!=episode_id:raise CatchupNativeError('episode-identity-unavailable')
        _integer(episode.get('id'),1)
        series_id=_integer(episode.get('seriesId'),1);series=self._get(f'series/{series_id}')
        if not isinstance(series,dict) or series.get('id')!=series_id or series.get('seriesType')!='anime':
            raise CatchupNativeError('qualified-anime-required')
        _integer(series.get('id'),1)
        if series.get('monitored') is not True or episode.get('monitored') is not True:raise CatchupNativeError('not-monitored')
        # GetAnimeEpisodes uses neither this series flag nor searchCriteria.
        # Qualification below requires the absolute branch; standard scene
        # numbering remains excluded without forcing a stock DB flag change.
        if type(series.get('useSceneNumbering')) is not bool:raise CatchupNativeError('native-series-evidence-incomplete')
        _integer(series.get('tvdbId'),1);_integer(episode.get('seasonNumber'),1);_integer(episode.get('episodeNumber'),1)
        _integer(episode.get('absoluteEpisodeNumber'),1)
        if _date(episode.get('airDateUtc'))>self.clock():raise CatchupNativeError('episode-not-aired')
        files=self._rows(self._get(f'episodefile?seriesId={series_id}'))
        if episode.get('hasFile') is not False or episode.get('episodeFileId')!=0:
            raise CatchupNativeError('episode-already-owned')
        for row in files:
            _integer(row.get('id'),1)
            if row.get('episodeIds') and episode_id in row['episodeIds']:raise CatchupNativeError('episode-already-owned')
        queue=self._paged('queue?includeUnknownSeriesItems=true&includeEpisode=true&includeSeries=true')
        for row in queue:
            if row.get('episodeId')==episode_id or (row.get('episode') or {}).get('id')==episode_id:
                raise CatchupNativeError('native-queue-owner')
            if row.get('seriesId') is None:raise CatchupNativeError('queue-ownership-incomplete')
        for command in self._rows(self._get('command')):
            if command.get('status') not in {'queued','started'}:continue
            name=command.get('name');body=command.get('body')
            if not isinstance(name,str) or not isinstance(body,dict):raise CatchupNativeError('command-ownership-incomplete')
            if name in {'EpisodeSearch','SeasonSearch','SeriesSearch','MissingEpisodeSearch','CutoffUnmetEpisodeSearch'}:
                if name=='EpisodeSearch' and isinstance(body.get('episodeIds'),list) and episode_id not in body['episodeIds']:continue
                if name!='EpisodeSearch' and body.get('seriesId') is not None and body['seriesId']!=series_id:continue
                raise CatchupNativeError('native-search-owner')
        profiles=self._rows(self._get('qualityprofile'));profile_id=_integer(series.get('qualityProfileId'),1)
        matching=[p for p in profiles if p.get('id')==profile_id]
        if len(matching)!=1 or matching[0].get('upgradeAllowed') is not False:raise CatchupNativeError('no-upgrade-policy-required')
        config=self._get('config/mediamanagement')
        # Stock DoNotPrefer skips revision preference; the strictly disabled
        # profile above still rejects upgrades. Preserve the native policy and
        # missing/ownership guards rather than demand an unnecessary setting edit.
        if not isinstance(config,dict) or config.get('downloadPropersAndRepacks') not in ('doNotUpgrade','doNotPrefer'):
            raise CatchupNativeError('no-proper-upgrade-policy-required')
        tags=series.get('tags');delays=self._rows(self._get('delayprofile'))
        if not isinstance(tags,list) or any(type(t) is not int or t<=0 for t in tags):raise CatchupNativeError('delay-policy-incomplete')
        choices=[]
        for delay in delays:
            _integer(delay.get('id'),1);_integer(delay.get('order'))
            values=delay.get('tags')
            if not isinstance(values,list) or any(type(t) is not int or t<=0 for t in values):raise CatchupNativeError('delay-policy-incomplete')
            if not values or set(values).intersection(tags):choices.append(delay)
        choices.sort(key=lambda d:d['order'])
        if not choices or (len(choices)>1 and choices[0]['order']==choices[1]['order']):raise CatchupNativeError('delay-policy-incomplete')
        delay=choices[0]
        if delay.get('enableTorrent') is not True:raise CatchupNativeError('torrent-policy-disabled')
        minutes=_integer(delay.get('torrentDelay'),0,20160)
        indexers=self._rows(self._get('indexer'))
        episode_keys=('id','seriesId','monitored','hasFile','episodeFileId','seasonNumber','episodeNumber','absoluteEpisodeNumber','airDateUtc')
        series_keys=('id','title','seriesType','monitored','qualityProfileId','tvdbId','tags','useSceneNumbering')
        guard={'compatibility':compatibility,'episode':{k:episode.get(k) for k in episode_keys},
               'series':{k:series.get(k) for k in series_keys},'quality_profile':matching[0],
               'delay_profile':delay,'proper_policy':config['downloadPropersAndRepacks'],
               'indexers':indexers,'files':files}
        return episode,series,minutes,indexers,guard

    def _payload(self,row,episode,series,indexers):
        if row.get('approved') is not True or row.get('rejected') is not False or row.get('temporarilyRejected') is not False or row.get('rejections')!=[]:
            return None
        absolute=episode['absoluteEpisodeNumber'];mapped=row.get('mappedEpisodeInfo')
        if (row.get('mappedSeriesId')!=series['id'] or not isinstance(mapped,list) or len(mapped)!=1 or not isinstance(mapped[0],dict) or mapped[0].get('id')!=episode['id']
            or row.get('fullSeason') is not False or row.get('special') is not False or row.get('sceneMapping') is not None
            or row.get('isAbsoluteNumbering') is not True or row.get('absoluteEpisodeNumbers')!=[absolute]
            or row.get('mappedAbsoluteEpisodeNumbers')!=[absolute] or row.get('episodeNumbers')!=[]):return None
        _integer(row.get('mappedSeriesId'),1);_integer(mapped[0].get('id'),1)
        _integer(row['absoluteEpisodeNumbers'][0],1);_integer(row['mappedAbsoluteEpisodeNumbers'][0],1)
        # Ordinary torrents often omit external IDs. In that case require exact
        # equality of native parsed canonical title, not a new alias mapper.
        source_tvdb=_integer(row.get('tvdbId'))
        if source_tvdb!=series['tvdbId']:
            if source_tvdb!=0 or not isinstance(series.get('title'),str) or row.get('seriesTitle')!=series['title']:return None
        if row.get('protocol')!='torrent':return None
        identifier=_integer(row.get('indexerId'),1);known=[i for i in indexers if i.get('id')==identifier]
        if len(known)!=1 or known[0].get('enableInteractiveSearch') is not True or known[0].get('enableAutomaticSearch') is not True:return None
        for key in ('title','guid','indexer'):
            if not isinstance(row.get(key),str) or not 1<=len(row[key])<=4096 or any(c in row[key] for c in ('\r','\n','\0')):
                raise CatchupNativeError('native-release-incomplete')
        _integer(row.get('size'),1,2**63-1);_integer(row.get('seeders'),0);_integer(row.get('leechers'),0);_integer(row.get('indexerFlags'),0)
        _date(row.get('publishDate'))
        url=row.get('downloadUrl');magnet=row.get('magnetUrl')
        if not isinstance(url,str) or not 1<=len(url)<=16384 or any(c in url for c in ('\r','\n','\0')):
            raise CatchupNativeError('native-download-source-incomplete')
        parsed=urlsplit(url)
        if parsed.scheme not in {'http','https','magnet'} or (parsed.scheme!='magnet' and (not parsed.hostname or parsed.username or parsed.password)):
            raise CatchupNativeError('native-download-source-incomplete')
        if magnet is not None and (not isinstance(magnet,str) or len(magnet)>16384 or any(c in magnet for c in ('\r','\n','\0')) or (magnet!='' and not magnet.startswith('magnet:?'))):
            raise CatchupNativeError('native-download-source-incomplete')
        keys=('guid','title','size','indexerId','indexer','protocol','publishDate','downloadUrl','magnetUrl','infoHash','seeders','leechers','indexerFlags','tvdbId','tvRageId','imdbId','infoUrl','commentUrl')
        return {key:row[key] for key in keys if key in row}

    def assess(self,episode_id):
        try:
            _integer(episode_id,1);episode,series,minutes,indexers,guard=self._context(episode_id)
            rows=self._rows(self._get(f'release?episodeId={episode_id}'));eligible=[];weights=set()
            for row in rows:
                weight=_integer(row.get('releaseWeight'))
                if weight in weights:raise CatchupNativeError('native-ranking-ambiguous')
                weights.add(weight);payload=self._payload(row,episode,series,indexers)
                if payload is not None:eligible.append((weight,payload))
            if not eligible:return {'status':'blocked','reason':'no-qualified-native-release'}
            payload=min(eligible,key=lambda value:value[0])[1];deadline=_date(payload['publishDate'])+dt.timedelta(minutes=minutes)
            if _date(payload['publishDate'])>self.clock():raise CatchupNativeError('future-native-publication')
            if self.clock()<deadline:return {'status':'waiting','reason':'native-preference-window','retry_at':deadline.isoformat()}
            evidence={'payload':payload,'guard':guard};fingerprint=_hash(evidence)
            candidate=NativeCandidate(episode_id,series['id'],fingerprint,json.dumps(evidence,allow_nan=False),self._issuer)
            return {'status':'eligible','reason':'mature-native-candidate','candidate':candidate}
        except (CatchupNativeError,TransportError,ValueError,TypeError,KeyError,AttributeError) as error:
            reason=str(error) if isinstance(error,CatchupNativeError) else 'native-evidence-unavailable'
            return {'status':'blocked','reason':reason}

    def dispatch(self,candidate):
        if not isinstance(candidate,NativeCandidate) or candidate._issuer is not self._issuer:raise CatchupNativeError('foreign-native-candidate')
        refreshed=self.assess(candidate.episode_id)
        if refreshed['status']!='eligible' or refreshed['candidate'].fingerprint!=candidate.fingerprint:
            raise CatchupNativeError('native-dispatch-evidence-changed')
        payload=json.loads(refreshed['candidate']._private_json)['payload']
        try:response=self.transport.request_json('POST','/api/v3/release/push',payload)
        except (TransportError,TimeoutError,OSError):raise CatchupNativeError('native-push-outcome-ambiguous') from None
        rows=self._rows(response)
        if len(rows)!=1:raise CatchupNativeError('native-push-outcome-ambiguous')
        row=rows[0]
        if row.get('mappedSeriesId')!=candidate.series_id or [v.get('id') for v in self._rows(row.get('mappedEpisodeInfo'))]!=[candidate.episode_id]:
            raise CatchupNativeError('native-push-mapping-changed')
        flags=[row.get(k) for k in ('approved','rejected','temporarilyRejected')]
        if any(type(value) is not bool for value in flags) or sum(flags)!=1:raise CatchupNativeError('native-push-outcome-ambiguous')
        status='approved' if flags[0] else 'rejected' if flags[1] else 'pending'
        return {'status':status,'reason':'native-'+status,'approved':flags[0],'rejected':flags[1],'temporarilyRejected':flags[2]}

    def receipt(self,episode_id,intent):
        try:
            _integer(episode_id,1)
            if not isinstance(intent,dict) or intent.get('episode_id')!=episode_id:raise CatchupNativeError('native-intent-incomplete')
            since=_date(intent.get('dispatch_time'));self._compatible();episode=self._get(f'episode/{episode_id}')
            if not isinstance(episode,dict) or episode.get('id')!=episode_id or episode.get('seriesId')!=intent.get('series_id'):raise CatchupNativeError('native-receipt-mapping-changed')
            file_id=_integer(episode.get('episodeFileId'))
            if episode.get('hasFile') is True and file_id:
                files=self._rows(self._get(f"episodefile?seriesId={episode['seriesId']}"))
                if any(row.get('id')==file_id for row in files):return {'status':'acquired','reason':'native-file-present'}
                raise CatchupNativeError('native-file-evidence-incomplete')
            for row in self._paged('queue?includeUnknownSeriesItems=true&includeEpisode=true&includeSeries=true'):
                if row.get('episodeId')==episode_id:
                    status='pending' if row.get('status') in {'delay','downloadClientUnavailable','fallback'} else 'queued'
                    return {'status':status,'reason':'native-'+status}
            for row in self._paged(f'history?episodeId={episode_id}&sortKey=date&sortDirection=descending'):
                if row.get('episodeId')==episode_id and row.get('eventType')=='grabbed' and _date(row.get('date'))>=since and _hash(row.get('sourceTitle'))==intent.get('title_sha256'):
                    return {'status':'queued','reason':'native-grab-receipt'}
            return {'status':'ambiguous','reason':'native-receipt-not-yet-proven'}
        except (CatchupNativeError,TransportError,ValueError,TypeError,KeyError,AttributeError):
            return {'status':'ambiguous','reason':'native-receipt-unavailable'}
