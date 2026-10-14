import json

from osu_scrobbler.display import render
from osu_scrobbler.models import Track
from osu_scrobbler.playback import DUPLICATE_MESSAGE
from osu_scrobbler.watcher import display_info


def test_default_identification_keeps_track_album_and_hides_internal_metadata():
    track = Track("防人想歌", "夏川陽子", "防人想歌", "夏川陽子", 112,
                  recording_id="recording-uuid", release_id="release-uuid", album_fallback=True,
                  details={"source_links": {"recording": "https://musicbrainz.org/recording/recording-uuid"}})
    output = render({"identification": "Matched", "track": track.to_dict(), "candidates": [track.to_dict()]})
    assert output == ""
    assert "夏川陽子 — 防人想歌" in render({"selected": track.to_dict()})


def test_fallback_message_is_brief_and_status_keeps_optional_choices():
    tracks = [Track(f"Song {index}", "Artist", f"Album {index}", "Artist").to_dict() for index in range(1, 6)]
    fallback = Track("Selected song", "Artist", "Selected song", "Artist", album_fallback=True).to_dict()
    output = render({"identification": "Ambiguous", "track": fallback, "beatmap_fallback": True, "candidates": tracks})
    assert output == ""
    status = render({"active_seconds": 0, "phase": "menu", "candidates": tracks, "track": fallback})
    assert "5. Artist — Song 5" in status


def test_info_is_brief_by_default_and_full_preserves_recording_release_details():
    track = Track("Song", "Singer", "Album", "Various Artists", 120, "recording-uuid", "release-uuid",
                  details={"recording": {"genres": [{"name": "pop", "count": 4}], "tags": [{"name": "vocal"}],
                                         "duration": 120, "id": "recording-uuid", "title": "Canonical song", "artist": "Singer"},
                           "release": {"date": "2026-10-13", "country": "JP", "type": "Album", "labels": ["Label"],
                                       "id": "release-uuid"}, "source_links": {"recording": "https://musicbrainz.org/recording/recording-uuid"}})
    response = {"info": display_info(track)}
    brief = render(response)
    assert len(brief.splitlines()) == 3
    assert "Singer — Song" in brief
    assert "Album artist: Various Artists" in brief
    assert "uuid" not in brief
    full = render(response, full=True)
    assert "Recording (MusicBrainz)" in full and "Release (MusicBrainz)" in full
    assert "Genres: pop" in full and "Tags: vocal" in full and "Label: Label" in full
    assert "ISRCs: unknown" in full
    assert "https://musicbrainz.org/recording/recording-uuid" in full
    assert "Canonical song" in full


def test_success_and_export_do_not_print_json_or_internal_ids():
    output = render({"delivery": "accepted", "track": Track("Song", "Artist", "Album", "Artist").to_dict(),
                     "id": "delivery-uuid", "message": "Scrobble accepted by Last.fm."})
    assert output == "Scrobbled: Artist — Song"
    assert render({"saved": "auto_export", "value": True, "restart_required": False}) == "Auto-export: on."
    assert render({"exported": 0, "destination": "C:\\Users\\User\\scrobbles.txt"}) == "Nothing new to export."


def test_duplicate_message_remains_exact_and_errors_stay_visible():
    assert render({"error": DUPLICATE_MESSAGE}) == DUPLICATE_MESSAGE
    assert "Ignored" in render({"delivery": "ignored", "message": "Timestamp is too old."})
    assert "Outcome uncertain" in render({"delivery": "uncertain", "message": "Review before retrying."})


def test_normal_phase_chatter_is_silent_but_loss_is_visible():
    for phase in ("playing", "paused", "menu", "failed", "results"):
        assert render({"phase": phase}) == ""
    assert "Disconnected" in render({"phase": "disconnected"})
    assert "Connected" in render({"connected": True})


def test_metadata_cannot_inject_terminal_control_characters():
    output = render({"selected": {"artist": "Singer\n\t", "title": "Song\x1b[2J"}})
    assert "\x1b" not in output and "\n" not in output and "\t" not in output
    assert "Singer" in output and "Song" in output


def test_json_opt_in_preserves_original_structured_data():
    response = {"info": {"recording": {"id": "uuid", "title": "曲"}}, "selection_key": "map"}
    assert json.loads(render(response, json_output=True)) == response


def test_force_shows_exact_submitted_fields_in_readable_form():
    output = render({"exact_metadata": Track("Track", "Artist", "Album", "Album Artist", 112, "mbid").to_dict(),
                     "playback_start": 1700000000, "dry_run": False})
    for expected in ("Artist — Track", "Album: Album", "Album artist: Album Artist"):
        assert expected in output

    assert "mbid" not in output and "1700000000" not in output
