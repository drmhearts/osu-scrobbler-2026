"""Small terminal messages; structured command data stays available as JSON."""

import json
import math
from datetime import datetime, timezone


def text(value):
    if value is None or value == "" or value == [] or value == {}:
        return "unknown"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(text(item.get("name") if isinstance(item, dict) else item) for item in value)
    # Metadata must not inject extra lines or terminal control sequences.
    return " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())


def song(track):
    track = track or {}
    return f"{text(track.get('artist_unicode') or track.get('artist'))} — {text(track.get('title_unicode') or track.get('title'))}"


def album(track):
    return f"Album: {text(track.get('album'))}" + (" (title fallback)" if track.get("album_fallback") else "")


def duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return "unknown"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}:{seconds:02d}"


def candidates(tracks, limit=None):
    shown = tracks if limit is None else tracks[:limit]
    lines = [f"  {index}. {song(track)} | {text(track.get('album'))}"
             + (" (title fallback)" if track.get("album_fallback") else "")
             for index, track in enumerate(shown, 1)]
    if tracks:
        lines.append("Choose: config choose N" + ("; status shows all candidates." if len(shown) < len(tracks) else ""))
    return lines


def info(value, full=False):
    selected = value.get("selected_metadata") or {}
    recording = value.get("recording") or {}
    release = value.get("release") or {}
    fallback = release.get("album_source") == "track-title fallback"
    lines = [song(selected), album({**selected, "album_fallback": fallback}),
             f"Album artist: {text(selected.get('album_artist'))}"]
    if not full:
        return "\n".join(lines)
    lines.extend(["", "Recording (MusicBrainz)", f"  Track: {song(recording)}",
                  f"  Duration: {duration(recording.get('duration'))}"])
    for key, label in (("genres", "Genres"), ("tags", "Tags"), ("isrcs", "ISRCs"),
                       ("first_release_date", "First release"), ("disambiguation", "Version"), ("id", "ID")):
        lines.append(f"  {label}: {text(recording.get(key))}")
    lines.extend(["", "Release (MusicBrainz)"])
    for key, label in (("title", "Title"), ("artist", "Artist"), ("album_source", "Album source"),
                       ("date", "Date"), ("country", "Country"), ("type", "Type"), ("status", "Status"),
                       ("secondary_types", "Other types"), ("labels", "Label"), ("genres", "Genres"),
                       ("tags", "Tags"), ("id", "ID"), ("release_group_id", "Release group")):
        lines.append(f"  {label}: {text(release.get(key))}")
    for key, link in (value.get("source_links") or {}).items():
        lines.append(f"  {key.replace('_', ' ').capitalize()} link: {text(link)}")
    return "\n".join(lines)


def render(value, *, full=False, json_output=False):
    if json_output:
        return json.dumps({"message": value} if isinstance(value, str) else value, ensure_ascii=False)
    if isinstance(value, str):
        return text(value)
    if set(value) == {"message"}:
        return text(value["message"])
    if "error" in value:
        # Preserve the requested duplicate-submission wording exactly.
        return text(value["error"])
    if "selected" in value:
        return f"Selected: {song(value['selected'])}"
    if "identification" in value:
        return ""
    if "threshold" in value:
        return "Timer: " + text(value["threshold"])
    if "phase" in value and "active_seconds" not in value:
        return "Disconnected; waiting for osu! / tosu." if value["phase"] == "disconnected" else ""
    if "connected" in value:
        return "Connected to osu!."
    if "DRY RUN - would submit" in value:
        track = value["DRY RUN - would submit"]["metadata"]
        return f"DRY RUN — would scrobble: {song(track)}\n{album(track)}"
    if "queued" in value and isinstance(value["queued"], dict):
        return f"Queued (offline): {song(value['queued']['metadata'])}" if value.get("offline") else ""
    if "delivery" in value and "active_seconds" not in value:
        status = value["delivery"]
        if status == "accepted":
            return f"Scrobbled: {song(value['track'])}" if value.get("track") else "Scrobbled."
        label = {"retry": "Queued for retry", "uncertain": "Outcome uncertain; inspect queue", "ignored": "Ignored", "failed": "Delivery failed"}.get(status, text(status))
        return f"{label}: {text(value.get('message'))}"
    if "exact_metadata" in value:
        track = value["exact_metadata"]
        lines = [f"{'DRY RUN — would force' if value.get('dry_run') else 'Force queued'}: {song(track)}",
                 album(track), f"Album artist: {text(track.get('album_artist'))}"]
        return "\n".join(lines)
    if "active_seconds" in value:
        track = value.get("track") or value.get("beatmap")
        lines = [song(track) if track else "No selected track."]
        if value.get("track"):
            lines.append(album(value["track"]))
        required = value.get("required_seconds")
        elapsed = value.get("active_seconds", 0)
        lines.append(f"{text(value.get('phase')).capitalize()} | {elapsed:g}s played / {text(required)}s required")
        if value.get("dry_run"):
            lines.append("Dry run: no Last.fm writes.")
        if value.get("suppressed"):
            lines.append("Already scrobbled. Start a fresh listen to scrobble again.")
        if value.get("candidates"):
            lines.extend(candidates(value["candidates"]))
        elif not value.get("track"):
            lines.append(text(value.get("lookup")))
        lines.append(f"Last.fm: {text(value.get('delivery'))}")
        if value.get("queue"):
            lines.append(f"Queue: {len(value['queue'])} unresolved; run queue list.")
        return "\n".join(lines)
    if "info" in value:
        return info(value["info"], full)
    if "exported" in value:
        count = value["exported"]
        if not count:
            return "Nothing new to export."
        line = f"Exported {count} scrobble{'s' if count != 1 else ''}."
        if value.get("destination"):
            line += " File: " + text(str(value["destination"]).replace("\\", "/").rsplit("/", 1)[-1])
        return line
    if "saved" in value:
        key = value["saved"]
        label = "Auto-export" if key == "auto_export" else key.replace('_', ' ').capitalize()
        line = f"{label}: {text(value.get('value'))}."
        return line + (" Restart watch to apply." if value.get("restart_required") else "")
    if "config" in value:
        return "\n".join(f"{key}: {text(item) if item != '' else 'default'}" for key, item in value["config"].items())
    if "saved_correction" in value:
        return f"Correction saved: {song(value['saved_correction'])}\n{album(value['saved_correction'])}"
    if "cleared" in value:
        return "Correction cleared; identifying again."
    if "history" in value:
        rows = value["history"]
        if not rows:
            return "No submission history."
        lines = []
        for row in rows[:20]:
            when = datetime.fromtimestamp(row["requested_at"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            lines.append(f"{when} | {text(row['status'])} | {song(row['track'])} | {text(row['id'])}")
        if len(rows) > 20:
            lines.append(f"{len(rows) - 20} older entries; --json queue list shows all.")
        return "\n".join(lines)
    if "requeued" in value:
        return "Delivery queued again; uncertain retries can duplicate a scrobble." if value["requeued"] else "No retry queued."
    if "authorized" in value:
        return "Last.fm authorized. Credentials saved securely."
    if "stopping" in value:
        return "Stopped."
    if "fixture_complete" in value:
        return f"Demo complete: {value['dry_run_submissions']} dry-run scrobbles; none sent."
    return "Done."
