"""build_mezzanine_cmd emits the documented ffmpeg command for every input
shape in the fixture pack, and make_mezzanine / extract_audio are cache-first
and atomic.

Offline: VideoInfo objects are parsed from the recorded ffprobe dicts in
``ffprobe_recordings``; ``run_ffmpeg`` is monkeypatched wherever a function
would otherwise start an encode.
"""

import logging
from pathlib import Path
from typing import Any

import pytest

import primecut.mezzanine as mezzanine
from primecut.config import settings
from primecut.mezzanine import (
    HDR_TONEMAP_CHAIN,
    SDR_CHAIN,
    SILENT_AUDIO_SOURCE,
    ToolchainError,
    build_extract_audio_cmd,
    build_mezzanine_cmd,
    choose_audio_track,
    extract_audio,
    make_mezzanine,
)
from primecut.probe import FFmpegError, VideoInfo
from tests.ffprobe_recordings import HDR, MULTITRACK, PHONE_PORTRAIT, RTL, SCREENREC

SRC = Path("in/phone portrait.mov")
OUT = Path("work/phone_portrait/mezzanine.mp4")


def info_from(data: dict[str, Any], name: str = "clip.mov") -> VideoInfo:
    return VideoInfo.from_ffprobe(data, Path(name))


def with_tracks(defaults: list[bool], channels: list[int]) -> VideoInfo:
    """MULTITRACK with the default flags and channel counts rewritten."""
    info = info_from(MULTITRACK, "obs.mkv")
    tracks = [
        t.model_copy(update={"is_default": d, "channels": c})
        for t, d, c in zip(info.audio_tracks, defaults, channels)
    ]
    return info.model_copy(update={"audio_tracks": tracks})


def value_after(args: list[str], flag: str) -> str:
    """The argument following the single occurrence of ``flag``."""
    positions = [i for i, a in enumerate(args) if a == flag]
    assert len(positions) == 1, f"{flag} appears {len(positions)} times in {args}"
    return args[positions[0] + 1]


def values_after(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def has_pair(args: list[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(args, args[1:]))


def phone_cmd(audio_track: int | None = None) -> list[str]:
    return build_mezzanine_cmd(SRC, OUT, info_from(PHONE_PORTRAIT), audio_track)


# --- stream mapping ----------------------------------------------------------


def test_input_and_output_paths_are_single_arguments():
    args = phone_cmd()
    assert args[:2] == ["-i", str(SRC)]
    assert args[-1] == str(OUT)
    assert args[-2] == "-y"


def test_maps_first_video_and_the_chosen_audio_only():
    args = phone_cmd()
    assert has_pair(args, "-map", "0:v:0")
    # Exactly two maps: the mebx data streams of the phone clip are dropped.
    assert values_after(args, "-map") == ["0:v:0", "0:a:0"]


def test_default_flag_beats_channel_count():
    # The documented trap: OBS-style file whose DEFAULT flag sits on the mono
    # system-audio track while the stereo mic has more channels. Nothing
    # requested means the default flag wins, so this keeps a:1.
    info = info_from(MULTITRACK, "obs.mkv")
    assert choose_audio_track(info, None).title == "System audio"
    args = build_mezzanine_cmd(SRC, OUT, info, None)
    assert values_after(args, "-map") == ["0:v:0", "0:a:1"]


def test_explicit_request_beats_default_flag():
    info = info_from(MULTITRACK, "obs.mkv")
    assert choose_audio_track(info, 0).title == "Mic"
    args = build_mezzanine_cmd(SRC, OUT, info, 0)
    assert values_after(args, "-map") == ["0:v:0", "0:a:0"]
    assert choose_audio_track(info, 2).title == "Music"


def test_most_channels_beats_index_when_nothing_is_default():
    info = with_tracks(defaults=[False, False, False], channels=[1, 1, 6])
    assert choose_audio_track(info, None).index == 2
    info = with_tracks(defaults=[False, False, False], channels=[2, 1, 1])
    assert choose_audio_track(info, None).index == 0


def test_channel_ties_go_to_the_lower_index():
    info = with_tracks(defaults=[False, False, False], channels=[2, 2, 2])
    assert choose_audio_track(info, None).index == 0


def test_first_default_wins_when_several_are_flagged():
    # MOV muxers flag every stream default; that must still mean track 0.
    info = with_tracks(defaults=[True, True, True], channels=[1, 6, 1])
    assert choose_audio_track(info, None).index == 0


def test_falls_back_to_track_zero():
    info = with_tracks(defaults=[False, False, False], channels=[0, 0, 0])
    assert choose_audio_track(info, None).index == 0


def test_requesting_a_missing_track_raises():
    info = info_from(MULTITRACK, "obs.mkv")
    with pytest.raises(ValueError) as exc:
        build_mezzanine_cmd(SRC, OUT, info, 3)
    message = str(exc.value)
    assert "a:3" in message
    assert "obs.mkv" in message
    # The message lists what does exist, so the fix is one flag away.
    assert "a:0" in message and "Mic" in message and "System audio" in message


def test_requesting_a_track_on_a_silent_input_raises():
    with pytest.raises(ValueError, match="a:0"):
        build_mezzanine_cmd(SRC, OUT, info_from(SCREENREC), 0)


def test_no_audio_input_gets_a_synthesized_silent_track(caplog):
    caplog.set_level(logging.WARNING, logger="primecut.mezzanine")
    info = info_from(SCREENREC, "screenrec.mov")
    assert choose_audio_track(info, None) is None
    args = build_mezzanine_cmd(SRC, OUT, info, None)
    assert values_after(args, "-i") == [str(SRC), SILENT_AUDIO_SOURCE]
    assert has_pair(args, "-f", "lavfi")
    assert args.index("lavfi") < args.index(SILENT_AUDIO_SOURCE)
    assert values_after(args, "-map") == ["0:v:0", "1:a:0"]
    assert "-shortest" in args
    assert not any(a.startswith("0:a:") for a in args)
    # Still one stereo 48 kHz AAC track, like every other mezzanine.
    assert has_pair(args, "-c:a", "aac") and has_pair(args, "-ac", "2")
    assert any("silent" in rec.message for rec in caplog.records if rec.levelno == logging.WARNING)


def test_inputs_with_audio_do_not_get_the_silent_source():
    args = phone_cmd()
    assert SILENT_AUDIO_SOURCE not in args
    assert "-shortest" not in args
    assert "lavfi" not in args


def test_audio_only_input_is_rejected_clearly():
    audio_only = {
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3", "channels": 2}],
        "format": {"format_name": "mp3", "duration": "600"},
    }
    with pytest.raises(ValueError, match="no video stream"):
        build_mezzanine_cmd(Path("episode.mp3"), OUT, info_from(audio_only, "episode.mp3"), None)


def test_audio_choice_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="primecut.mezzanine")
    build_mezzanine_cmd(SRC, OUT, info_from(MULTITRACK, "obs.mkv"), None)
    messages = [rec.message for rec in caplog.records]
    assert any(
        "a:1" in m and "System audio" in m and "lang=eng" in m and "of 3" in m for m in messages
    )


# --- video chain -------------------------------------------------------------


def test_hdr_input_gets_the_tonemap_chain():
    args = build_mezzanine_cmd(SRC, OUT, info_from(HDR), None)
    assert value_after(args, "-vf") == HDR_TONEMAP_CHAIN
    assert HDR_TONEMAP_CHAIN == (
        "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
        "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )


@pytest.mark.parametrize("recording", [PHONE_PORTRAIT, SCREENREC, RTL, MULTITRACK])
def test_sdr_input_gets_only_the_pixel_format(recording):
    args = build_mezzanine_cmd(SRC, OUT, info_from(recording), None)
    assert value_after(args, "-vf") == SDR_CHAIN == "format=yuv420p"
    assert "zscale" not in " ".join(args)
    assert "tonemap" not in " ".join(args)


def test_constant_frame_rate_and_gop():
    args = phone_cmd()
    assert has_pair(args, "-fps_mode", "cfr")
    assert has_pair(args, "-r", str(settings.target_fps))
    assert has_pair(args, "-g", str(2 * settings.target_fps))
    assert value_after(args, "-r") == "30"
    assert value_after(args, "-g") == "60"
    # Scene-change detection off, otherwise -g is only a maximum and x264
    # restarts the GOP at every cut: keyframes land on 0, 2, 4, ... s.
    assert has_pair(args, "-sc_threshold", "0")
    # The deprecated spelling never appears.
    assert "-vsync" not in args


def test_rotation_is_left_to_autorotate():
    assert "-noautorotate" not in phone_cmd()
    assert "-noautorotate" not in build_mezzanine_cmd(SRC, OUT, info_from(SCREENREC), None)
    # And no copy codec anywhere: a copied stream would keep the original's
    # rotation matrix next to re-encoded pixels.
    assert "copy" not in phone_cmd()


def test_video_encode_flags():
    args = phone_cmd()
    assert has_pair(args, "-c:v", "libx264")
    assert has_pair(args, "-crf", str(settings.crf))
    assert has_pair(args, "-preset", settings.x264_preset)
    assert has_pair(args, "-pix_fmt", "yuv420p")
    assert has_pair(args, "-movflags", "+faststart")
    assert has_pair(args, "-f", "mp4")


def test_audio_encode_flags():
    args = phone_cmd()
    assert has_pair(args, "-af", "aresample=async=1")
    assert has_pair(args, "-c:a", "aac")
    assert has_pair(args, "-b:a", settings.audio_bitrate)
    assert has_pair(args, "-ac", "2")
    assert has_pair(args, "-ar", "48000")


def test_command_is_all_strings():
    for recording in (PHONE_PORTRAIT, SCREENREC, HDR, MULTITRACK):
        args = build_mezzanine_cmd(SRC, OUT, info_from(recording), None)
        assert all(isinstance(a, str) for a in args), args


def test_extract_audio_command():
    args = build_extract_audio_cmd(Path("m.mp4"), Path("audio.wav.part"))
    assert args[:2] == ["-i", "m.mp4"]
    assert "-vn" in args
    assert has_pair(args, "-ac", "1")
    assert has_pair(args, "-ar", "16000")
    assert has_pair(args, "-c:a", "pcm_s16le")
    assert has_pair(args, "-f", "wav")
    assert args[-2:] == ["-y", "audio.wav.part"]


# --- ffmpeg_has_filter -------------------------------------------------------


FILTERS_EXCERPT = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
  ------
 TS aap               AA->A      Apply Affine Projection algorithm to first audio stream.
 .. anullsrc          |->A       Null audio source, return empty audio frames.
 .. aresample         A->A       Resample audio data.
 .. format            V->V       Convert the input video to one of the specified pixel formats.
 .. scale2ref         VV->VV     Scale the input video size and/or convert the image format to the given reference.
 .S tonemap           V->V       Conversion to/from different dynamic ranges.
 T.. zscale           V->V       Apply resizing, colorspace and bit depth conversion.
"""


def test_parse_filter_names_reads_the_name_column():
    names = mezzanine._parse_filter_names(FILTERS_EXCERPT)
    assert {"aap", "anullsrc", "aresample", "format", "scale2ref", "tonemap", "zscale"} <= names
    # Legend lines and the header contribute nothing.
    assert "=" not in names
    assert "Timeline" not in names
    assert "Filters:" not in names
    assert "------" not in names


def test_ffmpeg_has_filter_uses_cached_listing(monkeypatch):
    calls: list[list[str]] = []

    class Done:
        stdout = FILTERS_EXCERPT

    def fake_run(args, **kwargs):
        calls.append(args)
        return Done()

    monkeypatch.setattr(mezzanine, "run_ffmpeg", fake_run)
    mezzanine._ffmpeg_filters.cache_clear()
    try:
        assert mezzanine.ffmpeg_has_filter("tonemap") is True
        assert mezzanine.ffmpeg_has_filter("zscale") is True
        assert mezzanine.ffmpeg_has_filter("libplacebo") is False
        assert calls == [["-filters"]]
    finally:
        mezzanine._ffmpeg_filters.cache_clear()


# --- make_mezzanine / extract_audio ------------------------------------------


@pytest.fixture
def never_run(monkeypatch):
    """Fail the test if anything would start ffmpeg or ffprobe."""

    def boom(*args, **kwargs):
        raise AssertionError(f"subprocess must not run: {args} {kwargs}")

    monkeypatch.setattr(mezzanine, "run_ffmpeg", boom)
    monkeypatch.setattr(mezzanine, "probe", boom)


def test_make_mezzanine_is_cache_first(tmp_path, never_run):
    src = tmp_path / "in.mov"
    out = tmp_path / "work" / "in" / "mezzanine.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"existing")
    # Neither src nor info is needed when out already exists.
    assert make_mezzanine(src, out) == out
    assert make_mezzanine(src, out, info=info_from(PHONE_PORTRAIT), audio_track=0) == out
    assert out.read_bytes() == b"existing"


def test_cached_mezzanine_warns_about_an_ignored_audio_track(tmp_path, never_run, caplog):
    caplog.set_level(logging.WARNING, logger="primecut.mezzanine")
    out = tmp_path / "mezzanine.mp4"
    out.write_bytes(b"existing")
    info = info_from(MULTITRACK, "obs.mkv")
    assert make_mezzanine(tmp_path / "obs.mkv", out, audio_track=0, info=info) == out
    assert any("audio_track=0" in r.message and "not applied" in r.message for r in caplog.records)
    # An index that does not exist is still rejected, cache or no cache.
    with pytest.raises(ValueError, match="a:7"):
        make_mezzanine(tmp_path / "obs.mkv", out, audio_track=7, info=info)
    assert out.read_bytes() == b"existing"


def test_extract_audio_is_cache_first(tmp_path, never_run):
    out = tmp_path / "audio.wav"
    out.write_bytes(b"existing")
    assert extract_audio(tmp_path / "mezzanine.mp4", out) == out
    assert out.read_bytes() == b"existing"


class FakeEncoder:
    """A run_ffmpeg stand-in that writes the output path (last arg) like ffmpeg would."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> None:
        self.calls.append(list(args))
        Path(args[-1]).write_bytes(b"encoded")
        if self.fail:
            raise FFmpegError(["ffmpeg", *args], 1, "Conversion failed!")


def test_make_mezzanine_writes_through_a_part_file(tmp_path, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    monkeypatch.setattr(mezzanine, "probe", lambda p: info_from(PHONE_PORTRAIT, str(p)))
    src = tmp_path / "phone.mov"
    out = tmp_path / "work" / "phone" / "mezzanine.mp4"

    assert make_mezzanine(src, out) == out
    assert out.read_bytes() == b"encoded"
    assert not out.with_name("mezzanine.mp4.part").exists()
    (args,) = encoder.calls
    # ffmpeg was pointed at the .part path with the container made explicit,
    # and the rename onto the real name happened only afterwards.
    assert args[-1] == str(out.with_name("mezzanine.mp4.part"))
    assert has_pair(args, "-f", "mp4")
    assert args[:2] == ["-i", str(src)]


def test_part_path_is_absolute_even_for_a_relative_out(tmp_path, monkeypatch):
    # The .part path is ffmpeg's last positional argument, so it must never
    # start with "-"; anchoring it to the cwd guarantees that. ``out`` itself
    # is returned exactly as given.
    monkeypatch.chdir(tmp_path)
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    out = Path("-work") / "mezzanine.mp4"
    out.parent.mkdir()
    assert make_mezzanine(Path("-in.mov"), out, info=info_from(PHONE_PORTRAIT)) == out
    (args,) = encoder.calls
    assert args[-1] == str(tmp_path / "-work" / "mezzanine.mp4.part")
    assert out.read_bytes() == b"encoded"
    wav = Path("-work") / "audio.wav"
    assert extract_audio(out, wav) == wav
    assert encoder.calls[-1][-1] == str(tmp_path / "-work" / "audio.wav.part")


def test_encode_timeout_scales_with_input_duration(tmp_path, monkeypatch):
    timeouts: list[int] = []

    def recording_encoder(args, **kwargs):
        timeouts.append(kwargs["timeout"])
        Path(args[-1]).write_bytes(b"encoded")

    monkeypatch.setattr(mezzanine, "run_ffmpeg", recording_encoder)
    short = info_from(PHONE_PORTRAIT)  # 34 s: the one-hour floor applies
    make_mezzanine(tmp_path / "a.mov", tmp_path / "a" / "mezzanine.mp4", info=short)
    podcast = short.model_copy(update={"duration": 3 * 3600.0})  # 3 h: 4x the input
    make_mezzanine(tmp_path / "b.mov", tmp_path / "b" / "mezzanine.mp4", info=podcast)
    assert timeouts == [3600, 12 * 3600]


def test_make_mezzanine_removes_the_part_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(mezzanine, "run_ffmpeg", FakeEncoder(fail=True))
    src = tmp_path / "phone.mov"
    out = tmp_path / "mezzanine.mp4"
    with pytest.raises(FFmpegError):
        make_mezzanine(src, out, info=info_from(PHONE_PORTRAIT))
    assert not out.exists()
    assert not out.with_name("mezzanine.mp4.part").exists()
    # And the next call is not fooled into thinking the encode succeeded.
    assert list(tmp_path.iterdir()) == []


def test_extract_audio_writes_through_a_part_file(tmp_path, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    mezz = tmp_path / "mezzanine.mp4"
    out = tmp_path / "audio.wav"
    assert extract_audio(mezz, out) == out
    assert out.read_bytes() == b"encoded"
    assert not out.with_name("audio.wav.part").exists()
    (args,) = encoder.calls
    assert args == build_extract_audio_cmd(mezz, out.with_name("audio.wav.part"))


def test_hdr_without_zscale_is_a_toolchain_error(tmp_path, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    monkeypatch.setattr(mezzanine, "ffmpeg_has_filter", lambda name: False)
    src = tmp_path / "iphone_hdr.mov"
    with pytest.raises(ToolchainError) as exc:
        make_mezzanine(src, tmp_path / "mezzanine.mp4", info=info_from(HDR, str(src)))
    message = str(exc.value)
    assert "zscale" in message and "libzimg" in message
    assert "iphone_hdr.mov" in message
    assert "smpte2084" in message
    # No silent fallback to a washed-out conversion.
    assert encoder.calls == []
    assert not (tmp_path / "mezzanine.mp4").exists()


def test_hdr_with_zscale_encodes_with_the_tonemap_chain(tmp_path, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    monkeypatch.setattr(mezzanine, "ffmpeg_has_filter", lambda name: name == "zscale")
    out = tmp_path / "mezzanine.mp4"
    make_mezzanine(tmp_path / "hdr.mp4", out, info=info_from(HDR))
    (args,) = encoder.calls
    assert value_after(args, "-vf") == HDR_TONEMAP_CHAIN
    assert out.exists()


def test_sdr_input_never_asks_about_zscale(tmp_path, monkeypatch):
    monkeypatch.setattr(mezzanine, "run_ffmpeg", FakeEncoder())

    def no_filter_check(name):
        raise AssertionError("ffmpeg -filters must not be consulted for SDR input")

    monkeypatch.setattr(mezzanine, "ffmpeg_has_filter", no_filter_check)
    make_mezzanine(tmp_path / "phone.mov", tmp_path / "mezzanine.mp4", info=info_from(PHONE_PORTRAIT))


def test_make_mezzanine_passes_audio_track_through(tmp_path, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(mezzanine, "run_ffmpeg", encoder)
    make_mezzanine(
        tmp_path / "obs.mkv", tmp_path / "mezzanine.mp4", audio_track=2, info=info_from(MULTITRACK)
    )
    (args,) = encoder.calls
    assert values_after(args, "-map") == ["0:v:0", "0:a:2"]
