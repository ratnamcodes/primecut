"""probe.py turns recorded ffprobe JSON into a correct VideoInfo, its errors
read well, and the subprocess helpers never go through a shell.

Offline: no ffprobe or ffmpeg is run. The parser is exercised through
``VideoInfo.from_ffprobe`` on the recorded dicts in ``ffprobe_recordings``
plus hand-built variations of them; the runners are exercised with
``subprocess.run`` monkeypatched to capture what they would have executed.
"""

import copy
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

import primecut.probe as probe_module
from primecut.probe import (
    STDERR_TAIL_CHARS,
    FFmpegError,
    ToolchainError,
    UnreadableMediaError,
    VideoInfo,
    probe,
    run_ffmpeg,
    run_ffprobe,
)
from tests.ffprobe_recordings import HDR, MULTITRACK, PHONE_PORTRAIT, RTL, SCREENREC

UNREADABLE_SENTENCE = "cannot read {path}: not a media file, or the file is truncated"


def parse(data: dict[str, Any], name: str = "clip.mp4") -> VideoInfo:
    return VideoInfo.from_ffprobe(data, Path(name))


def video_stream(**overrides: Any) -> dict[str, Any]:
    """A plain SDR 1080p25 video stream dict, with fields overridden or removed (None)."""
    stream: dict[str, Any] = {
        "index": 0,
        "codec_name": "h264",
        "codec_type": "video",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
        "duration": "10.000000",
        "disposition": {"default": 1},
        "tags": {"language": "und"},
    }
    for key, value in overrides.items():
        if value is None:
            stream.pop(key, None)
        else:
            stream[key] = value
    return stream


def audio_stream(index: int = 1, **overrides: Any) -> dict[str, Any]:
    stream: dict[str, Any] = {
        "index": index,
        "codec_name": "aac",
        "codec_type": "audio",
        "sample_rate": "48000",
        "channels": 2,
        "duration": "10.000000",
        "disposition": {"default": 0},
        "tags": {},
    }
    stream.update(overrides)
    return stream


def clip(*streams: dict[str, Any], **format_overrides: Any) -> dict[str, Any]:
    fmt: dict[str, Any] = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10.000000"}
    fmt.update(format_overrides)
    return {"streams": list(streams), "format": fmt}


# --- frame rate --------------------------------------------------------------


def test_fps_is_parsed_as_a_fraction():
    info = parse(PHONE_PORTRAIT)
    assert info.fps == pytest.approx(30000 / 1001)
    assert info.fps == pytest.approx(29.97, abs=0.001)
    assert info.fps != 30000
    assert info.r_frame_rate == "30000/1001"
    assert info.avg_frame_rate == "610800/20381"


def test_fps_ntsc_film():
    assert parse(RTL).fps == pytest.approx(24000 / 1001)
    assert parse(RTL).fps == pytest.approx(23.976, abs=0.001)


def test_fps_integer_rate_string():
    assert parse(clip(video_stream(r_frame_rate="30"))).fps == 30.0


def test_fps_zero_when_rate_missing_or_undefined():
    assert parse(clip(video_stream(r_frame_rate="0/0"))).fps == 0.0
    assert parse(clip(video_stream(r_frame_rate=None))).fps == 0.0
    assert parse(clip(video_stream(r_frame_rate="garbage"))).fps == 0.0


# --- variable frame rate -----------------------------------------------------


def test_vfr_when_header_and_average_disagree():
    info = parse(SCREENREC)
    assert info.is_vfr is True
    assert info.fps == 120.0
    assert info.avg_frame_rate == "4168/91"


def test_not_vfr_when_rates_nearly_agree():
    # 30000/1001 vs 610800/20381 differ by 0.003%: a rounding artefact of the
    # container timebase, not a variable frame rate.
    assert parse(PHONE_PORTRAIT).is_vfr is False


def test_not_vfr_when_rates_identical():
    assert parse(RTL).is_vfr is False


def test_not_vfr_when_average_unknown():
    # A missing or 0/0 average is "no information", not "variable".
    assert parse(clip(video_stream(avg_frame_rate="0/0"))).is_vfr is False
    assert parse(clip(video_stream(avg_frame_rate=None))).is_vfr is False


# --- rotation ----------------------------------------------------------------


def test_rotation_from_display_matrix_side_data():
    info = parse(PHONE_PORTRAIT)
    assert info.rotation == 90
    # Stored size is reported as stored; the mezzanine bakes the turn in.
    assert (info.width, info.height) == (1920, 1080)


def test_rotation_from_legacy_rotate_tag():
    assert parse(clip(video_stream(tags={"rotate": "90"}))).rotation == 90
    assert parse(clip(video_stream(tags={"rotate": "270"}))).rotation == 270
    assert parse(clip(video_stream(tags={"rotate": "180"}))).rotation == 180
    assert parse(clip(video_stream(tags={"rotate": "-90"}))).rotation == 270
    assert parse(clip(video_stream(tags={"rotate": "-270"}))).rotation == 90


@pytest.mark.parametrize(
    ("side_data_rotation", "expected"),
    [
        (-90, 90),  # the real phone clip
        (90, 270),
        (-180, 180),
        (180, 180),
        (-270, 270),  # -270 ccw is +90 ccw is 270 clockwise
        (270, 90),
        (0, 0),
        (-89.6, 90),  # snapped to the nearest quarter turn
        (-90.0, 90),
    ],
)
def test_rotation_normalized_to_quarter_turns(side_data_rotation, expected):
    stream = video_stream(
        side_data_list=[{"side_data_type": "Display Matrix", "rotation": side_data_rotation}]
    )
    assert parse(clip(stream)).rotation == expected


def test_display_matrix_wins_over_legacy_tag():
    stream = video_stream(
        side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}],
        tags={"rotate": "180"},
    )
    assert parse(clip(stream)).rotation == 90


def test_rotation_zero_when_absent():
    assert parse(RTL).rotation == 0
    assert parse(SCREENREC).rotation == 0
    # Other side data without a rotation key is ignored.
    stream = video_stream(side_data_list=[{"side_data_type": "Content Light Level"}])
    assert parse(clip(stream)).rotation == 0


# --- HDR ---------------------------------------------------------------------


def test_hdr_pq():
    info = parse(HDR)
    assert info.is_hdr is True
    assert info.color_transfer == "smpte2084"
    assert info.color_primaries == "bt2020"
    assert info.pix_fmt == "yuv420p10le"


def test_hdr_hlg():
    assert parse(clip(video_stream(color_transfer="arib-std-b67"))).is_hdr is True


def test_sdr():
    assert parse(PHONE_PORTRAIT).is_hdr is False
    assert parse(clip(video_stream(color_transfer="bt709"))).is_hdr is False
    assert parse(clip(video_stream(color_transfer="unknown"))).is_hdr is False
    assert parse(clip(video_stream(color_transfer=None))).is_hdr is False
    # 10-bit alone is not HDR.
    assert parse(clip(video_stream(pix_fmt="yuv420p10le"))).is_hdr is False


# --- audio tracks ------------------------------------------------------------


def test_multitrack_audio_is_fully_described():
    info = parse(MULTITRACK)
    assert info.has_audio is True
    assert info.audio_track_count == 3
    assert info.audio_codec == "aac"
    mic, system, music = info.audio_tracks

    assert (mic.index, mic.stream_index) == (0, 1)
    assert mic.codec == "aac"
    assert mic.channels == 2
    assert mic.sample_rate == 48000
    assert mic.language == "eng"
    assert mic.title == "Mic"
    assert mic.is_default is False

    assert (system.index, system.stream_index) == (1, 2)
    assert system.channels == 1
    assert system.title == "System audio"
    assert system.is_default is True

    assert (music.index, music.stream_index) == (2, 3)
    assert music.language is None
    assert music.title == "Music"
    assert music.is_default is False


def test_track_index_counts_audio_streams_only():
    # phone_portrait: audio is absolute stream 1, but a:0 for -map purposes.
    (track,) = parse(PHONE_PORTRAIT).audio_tracks
    assert track.index == 0
    assert track.stream_index == 1
    assert track.sample_rate == 44100


def test_title_falls_back_to_mov_name_tag():
    # MOV/MP4 store a per-track title under "name"; Matroska under "title".
    data = clip(video_stream(), audio_stream(tags={"name": "Mic/Aux"}))
    assert parse(data).audio_tracks[0].title == "Mic/Aux"


def test_language_tag_is_kept_verbatim():
    # "und" is what the MOV muxer wrote; the parser reports it, not None.
    assert parse(PHONE_PORTRAIT).audio_tracks[0].language == "und"
    assert parse(RTL).audio_tracks[0].language == "eng"
    assert parse(clip(video_stream(), audio_stream(tags={}))).audio_tracks[0].language is None


def test_no_audio_stream():
    info = parse(SCREENREC)
    assert info.has_audio is False
    assert info.audio_track_count == 0
    assert info.audio_tracks == []
    assert info.audio_codec is None


def test_track_label_reads_well():
    label = parse(MULTITRACK).audio_tracks[1].label()
    assert label == "a:1 (aac, 1 ch, lang=eng, title='System audio')"


# --- inputs without video ----------------------------------------------------


def test_audio_only_input_still_probes():
    data = clip(audio_stream(index=0), format_name="mp3", duration="600.5")
    info = parse(data, "episode.mp3")
    assert info.video_codec is None
    assert (info.width, info.height) == (0, 0)
    assert info.fps == 0.0
    assert (info.r_frame_rate, info.avg_frame_rate) == ("0/0", "0/0")
    assert info.is_vfr is False
    assert info.rotation == 0
    assert info.is_hdr is False
    assert info.pix_fmt is None
    assert info.has_audio is True
    assert info.audio_codec == "aac"
    assert info.duration == 600.5
    assert info.container == "mp3"


def test_embedded_cover_art_is_not_a_video_stream():
    cover = video_stream(codec_name="mjpeg", width=600, height=600, disposition={"attached_pic": 1})
    info = parse(clip(audio_stream(index=0), cover))
    assert info.video_codec is None
    assert info.width == 0


# --- duration, container, bitrate --------------------------------------------


def test_format_level_fields():
    info = parse(PHONE_PORTRAIT, "phone_portrait.mov")
    assert info.path == Path("phone_portrait.mov")
    assert info.duration == pytest.approx(33.968333)
    assert info.container == "mov,mp4,m4a,3gp,3g2,mj2"
    assert info.bitrate == 13863690
    assert info.video_codec == "hevc"
    assert parse(MULTITRACK).container == "matroska,webm"


def test_duration_falls_back_to_video_then_audio_stream():
    no_format_duration = clip(
        video_stream(duration="12.5"), audio_stream(duration="12.0"), duration=None
    )
    del no_format_duration["format"]["duration"]
    assert parse(no_format_duration).duration == 12.5

    audio_only = clip(audio_stream(index=0, duration="7.25"))
    del audio_only["format"]["duration"]
    assert parse(audio_only).duration == 7.25


def test_no_duration_anywhere_is_unreadable():
    data = clip(video_stream(duration=None))
    del data["format"]["duration"]
    with pytest.raises(UnreadableMediaError):
        parse(data)


def test_missing_bitrate_is_none():
    data = clip(video_stream())
    assert parse(data).bitrate is None
    data["format"]["bit_rate"] = "N/A"
    assert parse(data).bitrate is None


# --- unreadable input --------------------------------------------------------


def test_empty_streams_is_unreadable():
    data = {"streams": [], "format": {"filename": "corrupt.mp4", "format_name": "mov"}}
    with pytest.raises(UnreadableMediaError) as exc:
        parse(data, "corrupt.mp4")
    assert str(exc.value).startswith(UNREADABLE_SENTENCE.format(path="corrupt.mp4"))
    assert exc.value.path == Path("corrupt.mp4")


def test_empty_dict_is_unreadable():
    with pytest.raises(UnreadableMediaError) as exc:
        parse({}, "corrupt.mp4")
    assert str(exc.value).startswith(UNREADABLE_SENTENCE.format(path="corrupt.mp4"))


def test_unreadable_is_an_ffmpeg_error():
    # Callers that catch the broad class still catch the specific one.
    assert issubclass(UnreadableMediaError, FFmpegError)


def test_unreadable_str_puts_sentence_first_then_command():
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "bad file.mp4"]
    err = UnreadableMediaError("bad file.mp4", cmd, 1, "moov atom not found")
    lines = str(err).splitlines()
    assert lines[0] == UNREADABLE_SENTENCE.format(path="bad file.mp4")
    assert lines[1] == shlex.join(cmd)
    assert lines[-1] == "moov atom not found"


# --- FFmpegError -------------------------------------------------------------


def test_ffmpeg_error_str_is_command_then_stderr_tail():
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-i", "my file.mov", "out.mp4"]
    err = FFmpegError(cmd, 1, "[libx264 @ 0x1] something\nConversion failed!\n")
    lines = str(err).splitlines()
    # Shell-quoted, so the line can be pasted back into a terminal as-is.
    assert lines[0] == shlex.join(cmd)
    assert "'my file.mov'" in lines[0]
    # Exactly two parts: the command, then the stderr tail. The exit status
    # lives on the attribute, not in the message.
    assert lines[1:] == ["[libx264 @ 0x1] something", "Conversion failed!"]
    assert "exit status" not in str(err)
    assert err.cmd == cmd
    assert err.returncode == 1
    assert isinstance(err, RuntimeError)


def test_ffmpeg_error_keeps_only_the_stderr_tail():
    long_stderr = "".join(f"line {n:05d}\n" for n in range(1000))  # 11 000 chars
    assert len(long_stderr) > 10_000
    err = FFmpegError(["ffmpeg"], 1, long_stderr)
    assert len(err.stderr) == STDERR_TAIL_CHARS == 4000
    assert err.stderr == long_stderr[-4000:]
    assert str(err).endswith("line 00999")
    assert "line 00000" not in str(err)


def test_ffmpeg_error_note_replaces_exit_status():
    err = FFmpegError(["ffmpeg", "-i", "x"], -1, "", note="timed out after 5 s and was killed")
    assert str(err) == "ffmpeg -i x\ntimed out after 5 s and was killed"


# --- subprocess helpers ------------------------------------------------------


class FakeRun:
    """Stand-in for subprocess.run that records every call."""

    def __init__(self, returncode: int = 0, stdout: str = "{}", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], self.returncode, self.stdout, self.stderr)


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fake = FakeRun(stdout='{"streams": [], "format": {}}')
    monkeypatch.setattr(probe_module.subprocess, "run", fake)
    return fake


HOSTILE_NAME = "weird; rm -rf ~ \"quoted\" 'single' $(x) & |.mov"


def test_run_ffprobe_passes_a_list_and_never_a_shell(fake_run):
    run_ffprobe(["-print_format", "json", "-show_streams", HOSTILE_NAME])
    (args, kwargs), = fake_run.calls
    cmd = args[0]
    assert isinstance(cmd, list)
    assert not isinstance(cmd, str)
    assert not kwargs.get("shell")
    assert cmd[:3] == ["ffprobe", "-v", "error"]
    # The hostile filename arrives as one untouched element, never re-split.
    assert cmd[-1] == HOSTILE_NAME
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_run_ffmpeg_passes_a_list_and_never_a_shell(fake_run):
    run_ffmpeg(["-i", HOSTILE_NAME, "-y", "out.mp4"], timeout=123)
    (args, kwargs), = fake_run.calls
    cmd = args[0]
    assert isinstance(cmd, list)
    assert not kwargs.get("shell")
    assert cmd[:3] == ["ffmpeg", "-hide_banner", "-nostdin"]
    assert cmd[3:] == ["-i", HOSTILE_NAME, "-y", "out.mp4"]
    assert kwargs["timeout"] == 123


def test_run_ffprobe_returns_parsed_json(fake_run):
    assert run_ffprobe(["x.mp4"]) == {"streams": [], "format": {}}


def test_run_ffprobe_nonzero_exit_raises(monkeypatch):
    fake = FakeRun(returncode=1, stdout="{\n\n}", stderr="x.mp4: Invalid data found when processing input")
    monkeypatch.setattr(probe_module.subprocess, "run", fake)
    with pytest.raises(FFmpegError) as exc:
        run_ffprobe(["x.mp4"])
    assert exc.value.returncode == 1
    assert "Invalid data" in exc.value.stderr
    assert str(exc.value).splitlines()[0] == "ffprobe -v error x.mp4"


def test_run_ffprobe_non_json_output_is_unreadable(monkeypatch):
    monkeypatch.setattr(probe_module.subprocess, "run", FakeRun(stdout="not json at all"))
    with pytest.raises(UnreadableMediaError) as exc:
        run_ffprobe(["-print_format", "json", "x.mp4"])
    assert str(exc.value).startswith(UNREADABLE_SENTENCE.format(path="x.mp4"))
    assert "not a JSON object" in str(exc.value)


def test_run_ffmpeg_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(probe_module.subprocess, "run", FakeRun(returncode=234, stderr="boom"))
    with pytest.raises(FFmpegError) as exc:
        run_ffmpeg(["-i", "x"])
    assert exc.value.returncode == 234
    assert exc.value.cmd == ["ffmpeg", "-hide_banner", "-nostdin", "-i", "x"]
    assert str(exc.value).endswith("boom")


def test_run_ffmpeg_timeout_becomes_ffmpeg_error(monkeypatch):
    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], stderr=b"frame= 12 fps=0.5")

    monkeypatch.setattr(probe_module.subprocess, "run", timing_out)
    with pytest.raises(FFmpegError) as exc:
        run_ffmpeg(["-i", "x"], timeout=7)
    assert exc.value.returncode == -1
    assert "timed out after 7 s" in str(exc.value)
    assert exc.value.stderr == "frame= 12 fps=0.5"


def test_missing_binary_is_a_toolchain_error(monkeypatch):
    def no_such_binary(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(probe_module.subprocess, "run", no_such_binary)
    with pytest.raises(ToolchainError, match="ffprobe was not found on PATH"):
        run_ffprobe(["x"])


# --- probe() -----------------------------------------------------------------


def test_probe_missing_file_raises_file_not_found(fake_run, tmp_path):
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "nope.mp4")
    assert fake_run.calls == []


def test_probe_wraps_ffprobe_failure_as_unreadable(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"\x00" * 16)
    fake = FakeRun(returncode=1, stdout="{\n\n}", stderr="error reading header")
    monkeypatch.setattr(probe_module.subprocess, "run", fake)
    with pytest.raises(UnreadableMediaError) as exc:
        probe(bad)
    text = str(exc.value)
    assert text.startswith(UNREADABLE_SENTENCE.format(path=bad))
    assert "ffprobe -v error -print_format json -show_streams -show_format" in text
    assert text.endswith("error reading header")
    assert exc.value.returncode == 1
    # Raised `from None`: one short message, not a chained traceback wall.
    assert exc.value.__suppress_context__ is True


def test_probe_parses_recorded_output(monkeypatch, tmp_path):
    import json

    src = tmp_path / "phone_portrait.mov"
    src.write_bytes(b"not really a movie")
    fake = FakeRun(stdout=json.dumps(PHONE_PORTRAIT))
    monkeypatch.setattr(probe_module.subprocess, "run", fake)
    info = probe(src)
    assert info.path == src
    assert info.rotation == 90
    assert info.fps == pytest.approx(29.97, abs=0.001)
    (args, _), = fake.calls
    assert args[0] == [
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(src)
    ]  # fmt: skip


def test_probe_anchors_a_leading_dash_name_to_the_cwd(fake_run, monkeypatch, tmp_path):
    # ffprobe takes the input as a positional argument; a bare "-x.mp4" would
    # be parsed as an option and misreported as an unreadable file.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "-x.mp4").write_bytes(b"not really a movie")
    with pytest.raises(UnreadableMediaError):  # fake_run answers with no streams
        probe(Path("-x.mp4"))
    (args, _), = fake_run.calls
    assert args[0][-1] == str(tmp_path / "-x.mp4")
    assert not args[0][-1].startswith("-")


def test_recordings_are_not_mutated_by_parsing():
    before = copy.deepcopy(MULTITRACK)
    parse(MULTITRACK)
    assert MULTITRACK == before
