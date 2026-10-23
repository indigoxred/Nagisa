"""Escaped, dependency-free HTML views."""

from __future__ import annotations

import html
import json
from typing import Any


def _safe(value: Any, fallback: str = "Not available") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def _mode(value: Any) -> tuple[str, str]:
    normalized = str(value or "unknown").lower()
    if normalized == "enabled":
        return "Active", "good"
    if normalized == "report":
        return "Observing only", "info"
    return "Unavailable", "bad"


def _badge(label: str, css_class: str) -> str:
    return f"<span class='badge {css_class}'>{html.escape(label)}</span>"


def _human_token(value: Any) -> str:
    token = str(value or "unknown")
    labels = {
        "PASS_DEFAULT": "Current ordering is safe",
        "SELECT_ALTERNATE": "A safer season ordering was found",
        "REVIEW_REQUIRED": "Ordering needs manual review",
        "INCOMPLETE_EVIDENCE": "Waiting for complete ordering evidence",
        "LATE_DISCOVERY_REVIEW": "Plex showed this title before Nagisa's ordering check settled",
        "plex-presented-the-show-before-selector-decision": "Plex showed this title before Nagisa's ordering check settled",
        "Plex presented the show before selector decision": "Plex showed this title before Nagisa's ordering check settled",
        "HISTORICAL_REVIEW": "Protected historical show needs manual review",
        "waiting": "Waiting for another safe observation",
        "ready": "Ready for an exact Sonarr rescan",
        "blocked": "Blocked by a safety check",
        "completed": "Retirement verified",
        "cycle": "Reconciliation check completed",
        "decision": "Ordering decision recorded",
        "rescan-issued": "Exact Sonarr rescan requested",
        "action-verification": "Sonarr retirement result checked",
        "event-skipped": "Import event ignored safely",
        "queue-item-rejected": "Impossible future episode rejected",
        "operator-lock": "Manual protection added",
        "apply-verified": "Ordering change verified",
        "undo-verified": "Nagisa ordering change undone",
        "ambiguous": "More than one ordering is plausible",
        "missing-full-english-subtitle": "English subtitles are missing",
        "missing-japanese-audio": "Japanese audio is missing",
        "source-still-present": "Shoko has not moved the file from /Shoko yet",
        "series-queue-not-empty": "The delivered episode is still in Sonarr's queue",
        "shoko-file-identity-missing": "Shoko has not recognized this exact file",
        "shoko-destination-unresolved": "Shoko has not produced one exact destination",
        "regular-provider-conflict": "Shoko has conflicting regular-episode identities",
        "provider-identity-not-exact": "Plex does not yet show the exact Shoko episode identity",
        "observation-incomplete": "A required file observation is incomplete",
        "dependency-unhealthy": "A required service is temporarily unavailable",
        "import-root-unhealthy": "The /Shoko import path is unavailable",
        "anime-root-unhealthy": "The /Anime destination path is unavailable",
        "plex-part-not-playable": "Plex cannot yet play the exact Shoko destination",
        "torrent-retention-unproven": "The retained torrent copy could not be verified",
        "shoko-plex-media-evidence-changed": "Shoko or Plex no longer matches the verified file",
        "stored-evidence-unreadable": "Stored file evidence could not be read",
        "stored-verification-unreadable": "Stored action evidence could not be read",
    }
    return labels.get(token, token.replace("_", " ").replace("-", " ").strip().title())


def page(title: str, content: str, csrf: str) -> bytes:
    safe_title = html.escape(title)
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>{safe_title} · Nagisa</title>
<style>
:root{{--bg:#07101f;--panel:#101d31;--panel2:#162640;--line:#2b4264;--text:#eef5ff;--muted:#a8b8cd;--accent:#72c7ff;--good:#56d38b;--warn:#f3bd5b;--bad:#ff7f87}}
*{{box-sizing:border-box}}body{{font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:linear-gradient(180deg,#07101f,#0b1526 45%,#07101f);color:var(--text)}}
nav{{position:sticky;top:0;z-index:2;background:rgba(7,16,31,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}}.nav-inner,main{{max-width:1240px;margin:auto;padding:1rem 1.25rem}}.brand{{font-size:1.1rem;margin-right:1.5rem}}nav a{{display:inline-block;color:var(--muted);text-decoration:none;padding:.45rem .65rem;border-radius:7px;margin:.1rem}}nav a:hover{{background:var(--panel2);color:var(--text)}}
h1{{font-size:2rem;margin:1.2rem 0 .25rem}}h2{{margin:.1rem 0 .55rem;font-size:1.18rem}}h3{{margin:.2rem 0}}p{{margin:.45rem 0;color:var(--muted)}}strong{{color:var(--text)}}
section,.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1.15rem;margin:1rem 0;box-shadow:0 10px 28px rgba(0,0,0,.14)}}.intro{{border-left:4px solid var(--accent)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:1rem;margin:1rem 0}}.cards .card{{margin:0;min-height:155px}}.metric{{font-size:1.5rem;font-weight:700;color:var(--text);margin:.35rem 0}}.small{{font-size:.88rem}}.muted{{color:var(--muted)}}
.badge{{display:inline-block;border-radius:999px;padding:.2rem .58rem;font-size:.8rem;font-weight:700;letter-spacing:.01em}}.badge.good{{color:#b9f6d0;background:#153c2a}}.badge.info{{color:#c6eaff;background:#173b56}}.badge.warn{{color:#ffe2a6;background:#4b3618}}.badge.bad{{color:#ffd0d3;background:#51252c}}.ok{{color:var(--good)}}.blocked{{color:var(--bad)}}
.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:680px}}td,th{{border-bottom:1px solid var(--line);padding:.72rem;text-align:left;vertical-align:top}}th{{color:#c8d8eb;font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}}tr:last-child td{{border-bottom:0}}code{{color:#c8e4ff;background:#0a1526;padding:.12rem .3rem;border-radius:4px;overflow-wrap:anywhere}}.case{{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:1rem;margin:.75rem 0}}.case details{{margin:.55rem 0;color:var(--muted)}}.case summary{{cursor:pointer;color:var(--text);font-weight:650}}.case table{{margin-top:.65rem}}
button,input{{font:inherit;padding:.55rem .7rem;margin:.2rem;border-radius:7px;border:1px solid var(--line)}}button{{background:#1974aa;color:white;cursor:pointer;font-weight:650}}button:hover{{background:#218bc9}}input{{background:#091426;color:var(--text)}}form{{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem;margin:.55rem 0}}.note{{border-left:3px solid var(--warn);padding-left:.8rem}}.event-detail{{color:var(--muted);font-size:.9rem}}a{{color:var(--accent)}}
@media(max-width:700px){{.nav-inner{{padding:.7rem}}.brand{{display:block;margin-bottom:.35rem}}nav a{{font-size:.9rem;padding:.35rem}}main{{padding:.75rem}}h1{{font-size:1.6rem}}}}
</style></head>
<body><nav><div class=nav-inner><strong class=brand>Nagisa</strong><a href=/>Overview</a><a href=/ordering>TMDB Ordering</a><a href=/reconciler>Reconciler</a><a href=/arrival>Arrival delivery</a><a href=/delivery>Delivery settings</a><a href=/folders>Folders</a><a href=/activity>Activity Log</a><a href=/system>Health / Updates</a></div></nav>
<main><h1>{safe_title}</h1>{content}<input type=hidden id=csrf value='{html.escape(csrf)}'></main></body></html>""".encode("utf-8")


def overview(runtime: Any) -> str:
    ordering = runtime.ordering.status_snapshot()
    reconciler = runtime.reconciler.status_snapshot()
    guard = runtime.future_guard.status_snapshot()
    ordering_label, ordering_class = _mode(ordering.get("mode"))
    reconciler_label, reconciler_class = _mode(reconciler.get("mode"))
    guard_label, guard_class = _mode(guard.get("mode"))
    queues = reconciler.get("queues", {}) if isinstance(reconciler.get("queues"), dict) else {}
    blocked = len(queues.get("blocked", []))
    waiting = len(queues.get("waiting", []))
    return f"""<section class=intro><h2>What Nagisa is doing</h2>
<p>Nagisa is a safety companion for the native Shoko and Sonarr workflow. It does not replace Shoko matching, ShokoRelay, Sonarr, or Seerr.</p></section>
<div class=cards>
<article class=card><h2>TMDB Ordering</h2>{_badge(ordering_label, ordering_class)}
<div class=metric>{int(ordering.get('baseline_count', 0))} existing shows protected</div>
<p>Compares Shoko's available TMDB episode orders and explains safer season-local numbering. It never changes historical or manual choices automatically.</p>
<p class=small><a href=/ordering>Review ordering decisions</a></p></article>
<article class=card><h2>Reconciler</h2>{_badge(reconciler_label, reconciler_class)}
<div class=metric>{blocked} blocked · {waiting} waiting</div>
<p>Checks whether Sonarr can safely retire imported Anime after Shoko has moved it and Plex can play the exact destination file.</p>
<p class=small><a href=/reconciler>Review imports and payload tracks</a></p></article>
<article class=card><h2>Future-airdate guard</h2>{_badge(guard_label, guard_class)}
<div class=metric>{int(guard.get('rejected', 0))} rejected last check</div>
<p>Checks normal TV downloads against their real air date and rejects only exact items more than {int(guard.get('threshold_hours', 24))} hours early.</p>
<p class=small>Ambiguous identities are left untouched.</p></article>
</div>"""


def ordering(runtime: Any, shoko_origin: str, csrf: str) -> str:
    snapshot = runtime.ordering.status_snapshot()
    mode_label, mode_class = _mode(snapshot.get("mode"))
    rows = []
    for item in snapshot.get("review", []):
        show_id = int(item.get("show_id", 0))
        title = html.escape(str(item.get("title") or f"Shoko show {show_id}"))
        raw_reason = str(item.get("reason", "review"))
        historical = bool(item.get("historical", False))
        late_discovery = raw_reason in {
            "LATE_DISCOVERY_REVIEW",
            "Plex presented the show before selector decision",
            "plex-presented-the-show-before-selector-decision",
        }
        if historical:
            reason = (
                "<strong>Existed when Nagisa created its protected baseline</strong>"
                "<div class=event-detail>Nothing failed. Nagisa protects established "
                "shows from automatic ordering changes.</div>"
            )
        elif late_discovery:
            reason = (
                "<strong>Newly discovered after Nagisa&#x27;s baseline</strong>"
                "<div class=event-detail>Plex finished first, before Nagisa had two "
                "stable observations. Nothing failed.</div>"
            )
        else:
            reason = html.escape(_human_token(raw_reason))
        link = f"{shoko_origin}/webui/#/tmdb-show/{show_id}"
        rows.append(f"<tr><td><strong>{title}</strong><div class=event-detail>Shoko ID {show_id}</div></td><td>{reason}</td><td><div class=small>Only investigate if Plex numbering looks wrong.</div><a href='/ordering/show/{show_id}'>Preview TMDB season layouts</a> · <a href='{html.escape(link)}' target=_blank rel='noreferrer'>Open in Shoko</a></td></tr>")
    table = "".join(rows) or "<tr><td colspan=3>No future-only review rows</td></tr>"
    return f"""<section class=intro><h2>What this page decides</h2>{_badge(mode_label, mode_class)}
<p>Nagisa compares every TMDB ordering Shoko exposes for a newly discovered show. It looks for complete one-to-one episode coverage, season-local numbering, separated specials and no gaps or collisions.</p>
<p>A row here does not mean its current ordering is wrong. It means the evidence was ambiguous, incomplete, protected, or the show was already visible in Plex before Nagisa had two stable observations. That timing case may be an older show first seen after Nagisa's baseline or a new import where Plex finished first.</p>
<p class=small>Use the preview only when you are investigating visible numbering. It is a read-only season-layout comparison; if Plex already looks correct, no action is needed.</p>
<p class=note>Historical shows and every manual Shoko choice are protected. In observing-only mode, Nagisa records recommendations but cannot apply them.</p></section>
<section><h2>Shows needing attention</h2><div class=table-wrap><table><tr><th>Item</th><th>Why it is here</th><th>Next step</th></tr>{table}</table></div></section>
<section><h2>Guarded controls</h2><p>Each control retrieves current evidence again before doing anything.</p>
<h3>Evaluate one show again</h3><p class=small>Use Shoko's numeric TMDB show ID to refresh its read-only comparison.</p>
<form method=post action=/ordering/evaluate><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><input name=show_id required inputmode=numeric placeholder='Shoko show ID'><button>Evaluate</button></form>
<h3>Protect a manual decision</h3><p class=small>Locks a show so Nagisa will never propose an automatic change for it. The actual ordering remains managed in Shoko.</p>
<form method=post action=/ordering/lock><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><input name=show_id required inputmode=numeric placeholder='Shoko show ID'><input name=reason required maxlength=200 placeholder='Why this is protected'><button>Lock / ignore</button></form>
<h3>Undo a Nagisa-owned change</h3><p class=small>Undo is limited to a change Nagisa previously applied, and only while current ordering and evidence still match the recorded apply.</p>
<form method=post action=/ordering/undo><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><input name=decision_id required inputmode=numeric placeholder='Nagisa decision ID'><button>Undo Nagisa-owned apply</button></form></section>"""


def _tree(value: dict[Any, Any]) -> str:
    if not value:
        return "No complete coordinate tree"
    return " · ".join(
        f"Season {int(season)}: {int(count)} episodes"
        for season, count in sorted(value.items(), key=lambda row: int(row[0]))
    )


def ordering_detail(runtime: Any, show_id: int, shoko_origin: str, csrf: str) -> str:
    comparison = runtime.ordering.comparison_snapshot(show_id)
    protection_labels = {
        "historical": "Existing show protected from automatic changes",
        "late-discovery": "Plex presented this show before Nagisa could decide",
        "manual": "Manual Shoko choice protected",
    }
    protection = protection_labels.get(
        comparison.get("protection_kind"), "No manual protection"
    )
    header = f"""<section><p>Shoko TMDB show <strong>{show_id}</strong> · Outcome: <code>{html.escape(str(comparison.get('outcome')))}</code></p>
<p>Current: <code>{html.escape(str(comparison.get('current_ordering_id')))}</code> · Recommended: <code>{html.escape(str(comparison.get('selected_ordering_id')))}</code></p>
<p>Protection: <strong>{html.escape(protection)}</strong></p>
<p><a href='{html.escape(shoko_origin)}/webui/#/tmdb-show/{show_id}' target=_blank rel='noreferrer'>Open in Shoko</a></p></section>"""
    sections = []
    for candidate in comparison.get("candidates", []):
        ordering_id = html.escape(str(candidate.get("ordering_id")))
        badges = []
        if candidate.get("is_current"):
            badges.append("current")
        if candidate.get("is_selected"):
            badges.append("recommended")
        failures = candidate.get("failures", [])
        reasons = "Passes every automatic gate" if not failures else ", ".join(html.escape(str(row)) for row in failures)
        rows = []
        for row in candidate.get("rows", []):
            identity = f"AniDB {int(row['anidb_series_id'])} {html.escape(str(row['kind']))} {int(row['anidb_episode_number'])}"
            rows.append(
                "<tr>"
                f"<td>{identity}</td><td>{int(row.get('shoko_episode_id', 0))}</td>"
                f"<td>{html.escape(str(row.get('current') or 'unmapped'))}</td>"
                f"<td>{html.escape(str(row.get('candidate') or 'unmapped'))}</td>"
                f"<td>{'yes' if row.get('file_backed') else 'no'}</td></tr>"
            )
        sections.append(
            f"""<section><h2>{ordering_id}{' · ' + ' · '.join(badges) if badges else ''}</h2>
<p>{html.escape(_tree(candidate.get('tree', {})))}</p><p>Gate result: {reasons}</p>
<table><tr><th>AniDB identity</th><th>Shoko episode</th><th>Current coordinate</th><th>Candidate coordinate</th><th>File-backed</th></tr>{''.join(rows)}</table></section>"""
        )
    apply = ""
    if comparison.get("operator_apply_allowed"):
        apply = f"""<p class=note>This is an explicit operator action. Nagisa will fetch the evidence twice, require one unique complete recommendation, preserve manual choices, write through Shoko's supported API, and verify the result before recording an undo point.</p><form method=post action=/ordering/apply><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><input type=hidden name=show_id value='{show_id}'><button>Apply reviewed ordering</button></form>"""
    return header + "".join(sections) + f"<section><h2>Guarded controls</h2>{apply}<form method=post action=/ordering/lock><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><input type=hidden name=show_id value='{show_id}'><input name=reason required maxlength=200 placeholder='Reason'><button>Lock / ignore</button></form></section>"


_ATTENTION_REASONS = {
    "missing-full-english-subtitle",
    "missing-japanese-audio",
    "media-policy-failed",
    "unsafe-attachment",
    "resolution-below-floor",
    "shoko-file-identity-missing",
    "shoko-destination-unresolved",
    "regular-provider-conflict",
    "provider-identity-not-exact",
}
_SHOKO_ATTENTION_REASONS = {
    "shoko-file-identity-missing",
    "shoko-destination-unresolved",
    "regular-provider-conflict",
    "provider-identity-not-exact",
}


def _reconciler_group(status: str, reasons: set[str]) -> str:
    if status == "completed":
        return "resolved"
    if status == "ready":
        return "ready"
    if status == "waiting":
        return "processing" if "source-still-present" in reasons else "confirmation"
    if reasons & _ATTENTION_REASONS:
        return "attention"
    return "safety"


def _reconciler_next_step(group: str, reasons: set[str], unrecognized_url: str) -> str:
    if group == "processing":
        return "No action needed while Shoko imports and moves this file."
    if group == "confirmation":
        return "No action needed. Nagisa needs the same complete result twice before one exact Sonarr rescan."
    if group == "ready":
        return "No action needed. The normal guarded cycle can issue one exact Sonarr rescan."
    if group == "resolved":
        return "No action needed. Sonarr retirement and no-reacquisition were verified."
    if reasons & _SHOKO_ATTENTION_REASONS:
        return (
            f"<a href='{html.escape(unrecognized_url)}' target=_blank rel='noreferrer'>"
            "Open Shoko Unrecognized Files</a> and link it only when the identity is certain."
        )
    if group == "attention":
        return "Review the actual payload. Nagisa has not removed or changed the file."
    return "Nagisa stopped safely. No action is required unless this condition persists."


def reconciler(runtime: Any, shoko_origin: str, csrf: str) -> str:
    snapshot = runtime.reconciler.status_snapshot()
    queues = snapshot.get("queues", {})
    guard = runtime.future_guard.status_snapshot()
    unrecognized_url = f"{shoko_origin}/webui/utilities/unrecognized/files"
    grouped: dict[str, list[dict[str, Any]]] = {
        "attention": [], "processing": [], "confirmation": [],
        "ready": [], "safety": [], "resolved": [],
    }
    for status in ("blocked", "waiting", "ready", "completed"):
        series_rows = queues.get(status, []) if isinstance(queues, dict) else []
        for series in series_rows:
            reasons = {str(reason) for reason in series.get("reasons", [])}
            grouped[_reconciler_group(status, reasons)].append(series)

    group_labels = {
        "attention": ("Needs your attention", "These items need a payload review or a confident manual Shoko match."),
        "processing": ("Processing normally", "Shoko still has the source file. This is expected and normally needs no intervention."),
        "confirmation": ("Waiting for confirmation", "The first safe observation passed; Nagisa deliberately waits for the same result again."),
        "ready": ("Ready for safe retirement", "All current gates passed and the exact Sonarr rescan is ready."),
        "safety": ("Safety hold", "Nagisa stopped because current evidence is incomplete or conflicting."),
        "resolved": ("Resolved", "The exact Sonarr retirement and follow-up checks completed."),
    }
    sections = []
    for group in ("attention", "processing", "confirmation", "ready", "safety", "resolved"):
        cases = []
        for series in grouped[group]:
            title = html.escape(str(series.get("title") or f"Sonarr item {int(series.get('series_id', 0))}"))
            episode_labels = [
                f"{html.escape(str(episode.get('coordinate', 'Unknown episode')))} · {html.escape(str(episode.get('title') or 'Untitled episode'))}"
                for episode in series.get("episodes", [])
            ]
            episode_details = "<br>".join(episode_labels) or "No episode details recorded"
            reason_tokens = {str(reason) for reason in series.get("reasons", [])}
            reasons = ", ".join(
                html.escape(_human_token(reason)) for reason in sorted(reason_tokens)
            ) or "No current safety warning"
            files = series.get("files", [])
            file_rows = []
            for file_row in files:
                policy = file_row.get("policy", {})
                policy_passed = policy.get("compliant") is True
                policy_label = "Media policy passed" if policy_passed else "Media policy needs review"
                policy_reasons = ", ".join(
                    html.escape(_human_token(reason)) for reason in policy.get("reasons", [])
                ) or "All media gates passed"
                audio = ", ".join(_track_label(track) for track in file_row.get("audio_tracks", [])) or "No audio tracks recorded"
                subtitles = ", ".join(_track_label(track) for track in file_row.get("subtitle_tracks", [])) or "No subtitle tracks recorded"
                file_rows.append(
                    f"<tr><td><code>{html.escape(str(file_row.get('evidence_path', 'unavailable')))}</code></td>"
                    f"<td><strong class={'ok' if policy_passed else 'blocked'}>{policy_label}</strong><br>{policy_reasons}</td>"
                    f"<td>{audio}</td><td>{subtitles}</td></tr>"
                )
            file_evidence = (
                "<div class=table-wrap><table><tr><th>Shoko destination</th><th>Media policy</th>"
                f"<th>Audio tracks</th><th>Subtitle tracks</th></tr>{''.join(file_rows)}</table></div>"
                if file_rows
                else "<p class=small>Detailed file and track evidence has not been recorded yet.</p>"
            )
            next_step = _reconciler_next_step(group, reason_tokens, unrecognized_url)
            cases.append(
                f"<article class=case><h3>{title}</h3>"
                f"<p class=small>Sonarr ID {int(series.get('series_id', 0))}</p>"
                f"<details><summary>{len(episode_labels)} exact episode{'s' if len(episode_labels) != 1 else ''}</summary><div class=event-detail>{episode_details}</div></details>"
                f"<p><strong>Why it is here:</strong> {reasons}</p>"
                f"<p><strong>What happens next:</strong> {next_step}</p>{file_evidence}</article>"
            )
        if cases:
            label, explanation = group_labels[group]
            sections.append(
                f"<section><h2>{label}</h2><p>{explanation}</p>{''.join(cases)}</section>"
            )
    mode_label, mode_class = _mode(snapshot.get("mode"))
    guard_label, guard_class = _mode(guard.get("mode"))
    guard_last = _safe(guard.get("last_cycle"), "Not checked since restart")
    return f"""<section class=intro><h2>Safe Sonarr retirement</h2>{_badge(mode_label, mode_class)}
<p>After Shoko moves an Anime file, Nagisa checks the exact Sonarr history item, Shoko identity, playable Plex destination, retained torrent and real audio/subtitle tracks. Two matching complete observations are required before one exact Sonarr rescan.</p>
<p class=note><strong>No automatic deletion:</strong> Nagisa never deletes these media files. Missing or conflicting evidence stops the action.</p>
<p><a href='{html.escape(unrecognized_url)}' target=_blank rel='noreferrer'>Open Shoko Unrecognized Files</a> for files that genuinely need a confident manual match.</p>
<p class=small>Internal history position: {int(snapshot.get('watermark', 0))}. This is only where Nagisa last read Sonarr history—not a media count.</p>
<p class=small>Nagisa checks automatically on its configured schedule. You normally do not need this button; it runs the same guarded check early and does not bypass any safety gate.</p>
<form method=post action=/reconciler/run><input type=hidden name=csrf value='{html.escape(csrf)}'><input type=hidden name=confirm value=CONFIRM><button>Check imports now</button></form></section>{''.join(sections) or '<section><p>No reconciliation observations yet.</p></section>'}
<section><h2>Future-airdate protection</h2>{_badge(guard_label, guard_class)}
<p>Only an exact normal-TV queue item more than <strong>{int(guard.get('threshold_hours', 24))} hours</strong> before its real air date can be rejected. Ambiguous identities fail closed and remain untouched.</p>
<div class=cards><div><strong>Last check</strong><p>{guard_last}</p></div><div><strong>Items examined</strong><p>{int(guard.get('observed', 0))}</p></div><div><strong>Rejected</strong><p>{int(guard.get('rejected', 0))}</p></div><div><strong>Blocked safely</strong><p>{int(guard.get('blocked', 0))}</p></div></div></section>"""


def _track_label(track: dict[str, Any]) -> str:
    language = html.escape(str(track.get("language") or "und"))
    codec = html.escape(str(track.get("codec") or "unknown"))
    title = html.escape(str(track.get("title") or "untitled"))
    flags = []
    if track.get("default"):
        flags.append("default")
    if track.get("forced"):
        flags.append("forced")
    suffix = f" ({', '.join(flags)})" if flags else ""
    return f"{language} · {codec} · {title}{suffix}"


def activity(runtime: Any, query: str = "") -> str:
    rows = runtime.audit.rows()
    rendered = []
    normalized_query = str(query).strip().casefold()
    module_labels = {
        "ordering": "TMDB ordering",
        "reconciler": "Import reconciler",
        "future-guard": "Future-airdate protection",
        "system": "System",
    }
    for row in rows:
        evidence = row.get("evidence", {}) if isinstance(row.get("evidence"), dict) else {}
        module = str(row.get("module", "system"))
        event_type = str(row.get("event_type", "event"))
        subject = "System"
        if evidence.get("show_id"):
            subject = f"Shoko show {int(evidence['show_id'])}"
        elif evidence.get("series_id"):
            subject = f"Sonarr series {int(evidence['series_id'])}"
        elif evidence.get("episode_id"):
            subject = f"Episode {int(evidence['episode_id'])}"
        outcome = evidence.get("outcome") or evidence.get("status") or event_type
        detail_parts = []
        if evidence.get("prior_ordering") is not None:
            detail_parts.append(f"Previous order: {_safe(evidence.get('prior_ordering'), 'default')}")
        if evidence.get("selected_ordering") is not None:
            detail_parts.append(f"Recommended order: {_safe(evidence.get('selected_ordering'))}")
        if evidence.get("reason"):
            detail_parts.append(f"Reason: {_human_token(evidence.get('reason'))}")
        for key, label in (("waiting", "Waiting"), ("blocked", "Blocked"), ("ready", "Ready"), ("completed", "Completed")):
            if key in evidence:
                detail_parts.append(f"{label}: {int(evidence.get(key, 0))}")
        details = " · ".join(detail_parts) or "No additional action details"
        search_text = " ".join((
            str(row.get("recorded_at", "")),
            module_labels.get(module, _human_token(module)),
            _human_token(outcome),
            _human_token(event_type),
            subject,
            details,
        )).casefold()
        if normalized_query and normalized_query not in search_text:
            continue
        rendered.append(
            "<tr>"
            f"<td>{_safe(row.get('recorded_at'))}</td>"
            f"<td>{html.escape(module_labels.get(module, _human_token(module)))}</td>"
            f"<td><strong>{html.escape(_human_token(outcome))}</strong><div class=event-detail>{html.escape(_human_token(event_type))}</div></td>"
            f"<td><strong>{html.escape(subject)}</strong><div class=event-detail>{details}</div></td>"
            "</tr>"
        )
    body = "".join(rendered) or "<tr><td colspan=4>No recorded activity yet.</td></tr>"
    return f"""<section class=intro><h2>Permanent, redacted history</h2>
<p>These are Nagisa's decisions and supported-API actions in plain language. Authentication data and query-bearing URLs are never stored here.</p></section>
<section><h2>Search activity</h2><form method=get action=/activity><input type=search name=q maxlength=100 value='{html.escape(query)}' placeholder='Title, module, outcome or ID'><button>Search activity</button><a href=/activity>Clear</a></form></section>
<section><div class=table-wrap><table><tr><th>When</th><th>Area</th><th>What happened</th><th>Item and explanation</th></tr>{body}</table></div></section>"""


def system(runtime: Any) -> str:
    health = runtime.health_snapshot()
    deployment = runtime.deployment_snapshot()
    overall_ok = bool(health.get("healthy"))
    cards = []
    modules = (
        ("ordering", "Ordering observer"),
        ("reconciler", "Import reconciler"),
        ("future_guard", "Future-airdate protection"),
        ("arrival", "Arrival delivery"),
        ("subtitles", "Original-release subtitles"),
        ("catchup", "Missed anime recovery"),
    )
    for key, title in modules:
        if key not in health:
            continue
        state = health.get(key, {}) if isinstance(health.get(key), dict) else {}
        ok = bool(state.get("ok"))
        error = state.get("error")
        explanation = "No errors reported" if not error else f"Stopped safely: {_safe(error)}"
        cards.append(
            f"<article class=card><h2>{title}</h2>{_badge('Healthy' if ok else 'Needs attention', 'good' if ok else 'bad')}"
            f"<p><strong>Last successful check</strong><br>{_safe(state.get('last_cycle'), 'Not checked since restart')}</p>"
            f"<p class=small>{explanation}</p></article>"
        )
    deployment_detail = (
        f"<p><strong>Current image</strong><br><code>{_safe(deployment.get('image'))}</code></p>"
        f"<p><strong>Previous rollback image</strong><br><code>{_safe(deployment.get('previous_image'))}</code></p>"
        f"<p><strong>Installed</strong><br>{_safe(deployment.get('installed_at'))}</p>"
        f"<p><strong>Backup ID</strong><br><code>{_safe(deployment.get('backup_id'))}</code></p>"
        if deployment.get("available")
        else "<p>Deployment and rollback metadata is not available. Nagisa remains fail-closed.</p>"
    )
    return f"""<section class=intro><h2>Overall status</h2>{_badge('Healthy' if overall_ok else 'Needs attention', 'good' if overall_ok else 'bad')}
<p>Database: <strong>{_safe(health.get('database'))}</strong> · Web interface: <strong>{'running' if health.get('web') else 'not running'}</strong></p>
<p>Nagisa version: <strong>{_safe(health.get('app_version'))}</strong> · State schema: <strong>{_safe(health.get('state_schema'))}</strong></p></section>
<div class=cards>{''.join(cards)}</div>
<section><h2>Installed version and rollback</h2>{deployment_detail}</section>
<section><h2>Settings and updates</h2><p>Nagisa never changes its own image. Unraid installs the configured versioned image, while `/config` keeps its database, history and evidence across container recreation.</p>
<p><strong>Unknown versions stop automatic actions.</strong> Read-only information may remain visible, but any action that depends on an unqualified Shoko or Sonarr API version fails closed until compatibility is verified.</p><p><a href=/folders>Review every configured folder Nagisa uses</a></p></section>"""


def folder_table(rows: list[dict]) -> str:
    body = ''.join('<tr><td><code>'+_safe(row['path'])+'</code></td><td>'+
                   _safe(row['purpose'])+'</td><td>'+_safe(row['access'])+
                   '</td><td>'+_safe(row.get('location', 'container'))+
                   '</td><td>'+_safe(row.get('owner', 'Nagisa'))+
                   '</td><td>'+_safe(row.get('change', 'existing').title())+
                   '</td></tr>' for row in rows)
    return ('<div class=table-wrap><table><tr><th>Folder or file</th>'
            '<th>Purpose</th><th>Access</th><th>Location</th>'
            '<th>Owner</th><th>Project change</th></tr>'+
            (body or '<tr><td colspan=6>No configured folders</td></tr>')+
            '</table></div>')


def folders(runtime: Any) -> str:
    return ('<section class=intro><h2>Configured folders</h2>'
            '<p>These are Nagisa container paths and the explicitly recorded NAS folders created or changed for this project. '
            'A stock Seerr, Shoko or Plex installation does not require '
            'Nagisa to remove these paths. Check the matching Unraid Docker '
            'template for the host-side mount locations.</p></section>'
            '<section>'+folder_table(runtime.folder_snapshot())+'</section>')


def _work_table(status,*,movie=False) -> str:
    from nagisa.worklist import public_reason
    states=frozenset('pending waiting-identity blocked-identity blocked-source ambiguous waiting-visibility blocked-preservation settling-visibility awaiting-visibility delivered unqualified-owner waiting-relay blocked-build waiting-download blocked-owner conflict blocked-limit waiting-refresh ready held-backlog waiting blocked queued awaiting-receipt rejected acquired unknown'.split())
    items=status.get('work_items',[]) if isinstance(status,dict) else []
    rows=[]
    for item in items[:100] if isinstance(items,list) else []:
        if not isinstance(item,dict):continue
        ids=[str(item[key]) if type(item.get(key)) is int and item[key]>0 else '—' for key in (('history_id','movie_id','radarr_file_id') if movie else ('history_id','series_id','episode_id'))]
        state=item.get('state') if isinstance(item.get('state'),str) and item.get('state') in states else 'unknown'
        reason=public_reason(item.get('reason')) if isinstance(item.get('reason'),str) else None
        rows.append('<tr>'+''.join('<td>'+value+'</td>' for value in ids)+
            '<td>'+_safe('Waiting for recognition or identity evidence' if state=='waiting-identity' else _human_token(state))+'</td><td>'+(_safe(_human_token(reason)) if reason else '—')+'</td></tr>')
    headings='<th>Radarr history ID</th><th>Radarr movie ID</th><th>Radarr file ID</th>' if movie else '<th>Sonarr history ID</th><th>Sonarr series ID</th><th>Sonarr episode ID</th>'
    return '<p>Up to 100 unfinished or review items from the saved journal. IDs can be checked in the existing applications.</p><div class=table-wrap><table><tr>'+headings+'<th>State</th><th>Reason</th></tr>'+(''.join(rows) or '<tr><td colspan=5>No saved work items</td></tr>')+'</table></div>'


def arrival(runtime: Any) -> str:
    status=runtime.arrival_snapshot()
    labels={
        'pending':'Waiting for its first check',
        'waiting-identity':'Waiting for recognition or identity evidence',
        'delivered':'Visible in both Plex libraries',
        'unqualified-owner':'Existing show did not pass preservation checks',
        'waiting-relay':'Waiting for Relay to become idle',
        'settling-visibility':'Visible; checking it stays available',
        'awaiting-visibility':'Waiting for Plex to show the episode',
        'ambiguous':'Rebuild outcome uncertain; automatic retry stopped',
    }
    rows=''.join(f"<tr><td>{_safe(labels.get(name,name.replace('-', ' ').capitalize()))}</td><td>{int(count)}</td></tr>" for name,count in sorted(status.get('counts',{}).items()))
    return f"""<section class=intro><h2>Arrival delivery</h2>
<p>Asks stock Relay to build recognized imports, then checks their original source appears in both configured Plex libraries. It does not identify files or choose episode numbering.</p>
<p>Status: <strong>{_safe(status.get('state'))}</strong></p>
<p>Last check error: {_safe(status.get('last_error'), 'None')}</p></section>
<section><h2>Imported episodes</h2><div class=table-wrap><table><thead><tr><th>Current state</th><th>Episodes</th></tr></thead><tbody>{rows or '<tr><td colspan=2>No arrivals enrolled</td></tr>'}</tbody></table></div>
<p>A recognition wait can also mean missing, ambiguous or unavailable identity evidence; it does not prove an absent AniDB hash.</p>{_work_table(status)}</section>"""


def delivery(runtime, csrf_token) -> str:
    settings = runtime.delivery_snapshot()
    subtitle = runtime.subtitle_snapshot()
    cards = ["<section><h2>Original-release subtitles</h2>",
        f"<p>Status: {_safe(subtitle.get('state'))}</p>",
        f"<p>Last check error: {_safe(subtitle.get('last_error'), 'None')}</p>",
        '<p>Copies confidently matched subtitles from the original download. Ambiguous matches and different existing files are left alone. Pending Plex confirmation remains visible here.</p>']
    counts = subtitle.get('counts', {})
    if isinstance(counts, dict):
        cards.append('<p>'+', '.join(f'{_safe(key)}: {_safe(value)}' for key,value in sorted(counts.items()))+'</p>')
    cards.append(_work_table(subtitle)+'</section>')
    recovery=getattr(runtime,'catchup_snapshot',lambda:{'state':'disabled'})()
    recovery_state=recovery.get('state') if isinstance(recovery,dict) else None
    if recovery_state not in ('enabled','disabled','stopped'):recovery_state='unavailable'
    cards.append('<section><h2>Missed anime recovery status</h2><p>Status: '+_safe(recovery_state)+'</p>')
    recovery_counts=recovery.get('counts',{}) if isinstance(recovery,dict) else {}
    if isinstance(recovery_counts,dict):
        visible_counts=[f'{state}: {recovery_counts[state]}' for state in ('ready','held-backlog','waiting','blocked','queued','pending','ambiguous','awaiting-receipt','rejected','acquired') if type(recovery_counts.get(state)) is int and recovery_counts[state]>=0]
        if visible_counts:cards.append('<p>'+', '.join(visible_counts)+'</p>')
    cards.append(_work_table(recovery)+'</section>')
    movie=getattr(runtime,'movie_arrival_snapshot',lambda:{'state':'disabled'})()
    movie_state=movie.get('state') if isinstance(movie,dict) else None
    if movie_state not in ('enabled','disabled','stopped'):movie_state='unavailable'
    cards.append('<section><h2>Anime movie arrival status</h2><p>Status: '+_safe(movie_state)+'</p><p>Recognized native Radarr imports are checked in the selected Anime libraries using Shoko episode identity. Historical imports remain held at the reviewed starting point.</p>')
    cards.append(_work_table(movie,movie=True)+'</section>')
    if not settings.get('configured'):
        cards.append("<section><h2>Delivery controls</h2><p>Disabled until reviewed delivery roots and an import starting point are configured. Existing Nagisa duties are unchanged.</p></section>")
        return ''.join(cards)
    saved = settings['saved']
    cards.append("<section><h2>Delivery controls</h2><p>Changes are saved for the next normal Nagisa restart. Saving does not restart production or reset its journals.</p>")
    if settings.get('restart_required'):
        cards.append('<p><strong>Saved settings differ from the running settings. Restart required.</strong></p>')
    cards.append('<p>Running arrival: '+_safe(settings['active']['arrival_enabled'])+'; running anime movie arrivals: '+_safe(settings['active']['movie_arrival_enabled'])+'; running subtitles: '+_safe(settings['active']['subtitles_enabled'])+'; running missed-anime recovery: '+_safe(settings['active']['catchup_enabled'])+'</p>')
    if settings.get('editable'):
        cards.append(f"<form method='post' action='/delivery/settings'><input type='hidden' name='csrf' value='{_safe(csrf_token)}'><input type='hidden' name='revision' value='{_safe(settings['revision'])}'>")
        for key,label in (('arrival_enabled','Arrival delivery'),('movie_arrival_enabled','Anime movie arrival delivery'),('subtitles_enabled','Original subtitles'),('reconciliation_enabled','Retained-file reconciliation'),('catchup_enabled','Missed anime episode recovery'),('catchup_include_backlog','Enroll historical missing anime episodes')):
            options = ''.join(f"<option value='{str(value).lower()}'{' selected' if saved[key]==value else ''}>{'Enabled' if value else 'Disabled'}</option>" for value in (False,True))
            cards.append(f"<label>{label} <select name='{key}'>{options}</select></label>")
        for key,label,low,high in (('arrival_poll_seconds','Arrival check interval (seconds)',15,300),
            ('movie_arrival_poll_seconds','Anime movie arrival check interval (seconds)',15,300),
            ('catchup_poll_seconds','Anime recovery check interval (seconds)',300,3600),
            ('subtitle_poll_seconds','Subtitle check interval (seconds)',15,300),
            ('subtitle_max_file_bytes','Maximum subtitle file size (bytes)',1024,16777216),
            ('subtitle_max_total_bytes','Maximum subtitles per import (bytes)',1024,67108864)):
            cards.append(f"<label>{label} <input type='number' name='{key}' min='{low}' max='{high}' value='{saved[key]}' required></label>")
        bindings={key:saved[key] for key in runtime.delivery_settings.BINDINGS}
        cards.append('<p>Anime recovery leaves existing files and queued work with Sonarr. Historical missing episodes are enrolled only when explicitly enabled. Changes take effect after restart.</p>')
        cards.append("<details><summary>Advanced: reviewed folders and Plex libraries</summary><p>Only already-approved mounts and libraries are accepted. Binding changes require a reviewed journal transition before restart; saved history is never reset automatically.</p><textarea name='bindings' rows='12' cols='70'>"+_safe(json.dumps(bindings,sort_keys=True,indent=2))+"</textarea></details>")
        cards.append("<label>Settings access key <input type='password' name='access_key' autocomplete='off' required></label><label>Type CONFIRM <input name='confirm' required></label><button type='submit'>Save for next restart</button></form>")
    else:
        cards.append('<p>Controls are read-only. A private settings access key must be configured to allow changes.</p>')
    cards.append('<h3>Reviewed folder bindings</h3><pre>'+_safe({key:saved[key] for key in ('managed_roots','plex_mounts','subtitle_source_mounts','subtitle_destination_root','subtitle_destination_mount','subtitle_initial_watermark','subtitle_plex_section_ids','movie_arrival_import_root','movie_arrival_source_root','movie_arrival_initial_watermark')})+'</pre></section>')
    return ''.join(cards)
