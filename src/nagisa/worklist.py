"""Pure redaction of existing journal work; never fetches or changes state."""
import json

PUBLIC_REASONS=frozenset('''new-missing initial-backlog explicit-backlog native-observation
receipt-unqualified receipt-unavailable native-read-unavailable native-assessment-unqualified
candidate-identity-unqualified dispatch-intent dispatch-outcome-unknown native-receipt-required
malformed-native-evidence malformed-native-date incomplete-native-evidence unsupported-sonarr-version
incomplete-native-pagination unstable-native-pagination native-page-bound-exceeded unstable-missing-frontier
missing-series-evidence-incomplete native-missing-read-unavailable episode-identity-unavailable
qualified-anime-required not-monitored native-series-evidence-incomplete episode-not-aired
episode-already-owned native-queue-owner queue-ownership-incomplete command-ownership-incomplete
native-search-owner no-upgrade-policy-required no-proper-upgrade-policy-required delay-policy-incomplete
torrent-policy-disabled native-release-incomplete native-download-source-incomplete native-ranking-ambiguous
no-qualified-native-release future-native-publication native-preference-window mature-native-candidate
native-evidence-unavailable foreign-native-candidate native-dispatch-evidence-changed
native-push-outcome-ambiguous native-push-mapping-changed native-intent-incomplete
native-receipt-mapping-changed native-file-present native-file-evidence-incomplete native-grab-receipt
presentation-cap build-unqualified
native-receipt-not-yet-proven native-receipt-unavailable native-approved native-rejected native-pending native-queued'''.split())


def import_item(history_id,payload,state,states,*,reason=None):
    item={'history_id':history_id,'state':state if state in states else 'unknown'}
    if item['state']=='blocked-build' and reason is not None:
        item['reason']=public_reason(reason)
    try:
        if not isinstance(payload,str) or len(payload)>8192:return item
        data=json.loads(payload)
        if not isinstance(data,dict):return item
        for key in ('series_id','episode_id','movie_id','radarr_file_id'):
            if type(data.get(key)) is int and data[key]>0:item[key]=data[key]
    except (ValueError,TypeError):pass
    return item


def public_reason(reason):
    return reason if reason in PUBLIC_REASONS else 'native-observation'
