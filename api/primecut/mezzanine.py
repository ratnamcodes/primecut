"""Normalize on ingest: one re-encode into a mezzanine every later stage can trust.

Turns whatever the user dropped in (a VFR screen recording, a sideways HEVC
phone clip, an HDR capture, an OBS file with the voice on the second audio
track) into one predictable file: constant-frame-rate 8-bit H.264 at
``settings.target_fps`` with a 2-second GOP, upright, SDR BT.709, with exactly
one stereo 48 kHz AAC track. A mono 16 kHz WAV sidecar for transcription is
then extracted from that mezzanine, never from the original, so ASR timestamps
and video math share one clock.

Every function here is cache-first (an existing output is returned untouched)
and atomic (ffmpeg writes to ``<out>.part`` which is renamed onto ``<out>``
only on success), so a crashed encode never leaves a half-written file that
passes the cache check on the next run. The command builders are pure so the
tests can assert on the exact argument list without running ffmpeg.
"""

import functools
import logging
import os
import re
import shlex
from pathlib import Path

from primecut.config import settings
from primecut.probe import AudioTrack, ToolchainError, VideoInfo, probe, run_ffmpeg

__all__ = [
    "HDR_TONEMAP_CHAIN",
    "SDR_CHAIN",
    "SILENT_AUDIO_SOURCE",
    "ToolchainError",
    "build_extract_audio_cmd",
    "build_mezzanine_cmd",
    "choose_audio_track",
    "extract_audio",
    "ffmpeg_has_filter",
    "make_mezzanine",
]

log = logging.getLogger(__name__)

# Tone map PQ/HLG down to BT.709 before the 8-bit encode: linearize (npl=100
# nits is SDR reference white), go to float RGB, apply the hable curve, then
# back to limited-range BT.709 YUV. Without this, iPhone HDR footage comes out
# grey and washed; with it, colors survive. Needs an ffmpeg built with libzimg
# (the zscale filter); make_mezzanine checks before it tries.
HDR_TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)

# SDR input only needs the pixel format pinned (10-bit SDR, 4:2:2, and
# RGB screen captures all exist in the wild).
SDR_CHAIN = "format=yuv420p"

# Second input used when the source has no audio stream at all (QuickTime
# screen recordings). Later stages rely on audio.wav existing for every
# mezzanine, so silence is synthesized here once instead of special-cased in
# every stage downstream.
SILENT_AUDIO_SOURCE = "anullsrc=channel_layout=stereo:sample_rate=48000"

# Suffix of the in-progress output. A crashed encode leaves <out>.part, which
# the cache check ignores, rather than a half-written <out> that passes it.
PART_SUFFIX = ".part"

# How long make_mezzanine lets ffmpeg run: the larger of one hour and this
# many seconds per second of input. x264 medium at 1080p runs near 1x realtime
# on a small cloud worker, and podcasts run two to three hours, so a flat hour
# would kill a legitimate encode, delete its .part, and retry from scratch.
ENCODE_TIMEOUT_FLOOR_SECONDS = 3600
ENCODE_TIMEOUT_PER_INPUT_SECOND = 4

# One row of `ffmpeg -filters`: a flags column (letters and dots), the filter
# name, then an "in->out" signature. The legend lines above the table never
# carry the "->" so they do not match.
_FILTER_LINE = re.compile(r"^\s*[A-Z.]+\s+(?P<name>\S+)\s+\S*->\S*")


def choose_audio_track(info: VideoInfo, requested: int | None) -> AudioTrack | None:
    """Pick the audio track the mezzanine keeps, or None when the input has none.

    Order: the caller's explicit ``requested`` index (a:N; a missing one is a
    ValueError listing what exists), else the track flagged default, else the
    track with the most channels (first wins ties), else track 0. FFmpeg's own
    default pick is not always the one with the voice in it: OBS files
    routinely carry mic and desktop audio as separate tracks, and the mezzanine
    keeps exactly one, so the choice is made explicitly and logged.
    """
    tracks = info.audio_tracks
    if requested is not None:
        for track in tracks:
            if track.index == requested:
                return track
        available = ", ".join(t.label() for t in tracks) or "none"
        raise ValueError(
            f"{info.path} has no audio track a:{requested}; available tracks: {available}"
        )
    if not tracks:
        return None
    for track in tracks:
        if track.is_default:
            return track
    # max() returns the first maximal element, so ties go to the lower index,
    # and with every track at 0 channels this degrades to track 0.
    return max(tracks, key=lambda t: t.channels)


def build_mezzanine_cmd(
    src: Path, out: Path, info: VideoInfo, audio_track: int | None
) -> list[str]:
    """The ffmpeg argument list for one mezzanine encode (after the
    ``ffmpeg -hide_banner -nostdin`` prefix that run_ffmpeg adds).

    Pure apart from logging: no filesystem, no subprocess. Raises ValueError
    for a requested audio track that does not exist or an input with no video.
    """
    if info.video_codec is None:
        raise ValueError(
            f"{src} has no video stream; the mezzanine needs one "
            "(audio-only ingest is not supported yet)"
        )
    track = choose_audio_track(info, audio_track)

    args: list[str] = ["-i", str(src)]
    if track is None:
        log.warning(
            "audio: %s has no audio stream; synthesizing a silent stereo track "
            "so audio.wav exists like every other mezzanine",
            src,
        )
        args += ["-f", "lavfi", "-i", SILENT_AUDIO_SOURCE]
        args += ["-map", "0:v:0", "-map", "1:a:0"]
        # anullsrc is endless; stop when the video does.
        args += ["-shortest"]
    else:
        log.info("audio: using track %s of %d", track.label(), info.audio_track_count)
        args += ["-map", "0:v:0", "-map", f"0:a:{track.index}"]

    # No -noautorotate here, on purpose. ffmpeg reads the container's display
    # matrix and inserts the transpose itself, so this re-encode bakes the
    # rotation into the pixels: the phone clip's 1920x1080 + rotate=90 comes
    # out as a real 1080x1920. Rotation is handled HERE and never again;
    # everything downstream sees an upright frame with no rotation metadata.
    # Never mix -c copy with a re-encoded rotation (copying the original's
    # matrix onto already-rotated pixels) or you get a double-rotated frame.
    args += ["-vf", HDR_TONEMAP_CHAIN if info.is_hdr else SDR_CHAIN]

    # Force constant frame rate. -vsync is the deprecated spelling, -fps_mode
    # is current, and this single flag is what makes timestamp math reliable
    # for the next eight weeks: after it, frame = round(time * target_fps)
    # holds at minute 90 exactly as it holds at second 1.
    args += ["-fps_mode", "cfr", "-r", str(settings.target_fps)]

    # Fixed 2-second GOP so a keyframe sits at every even second and seeks
    # (clip cuts, thumbnails) land predictably later. -g alone only caps the
    # GOP: x264's scene-change detection (scenecut=40) inserts extra IDR
    # frames and restarts the count from there, which put keyframes at 1.1 s,
    # 2.17 s, 2.97 s ... on the phone clip. -sc_threshold 0 turns that off so
    # the grid is exactly 0, 2, 4, ... seconds.
    args += ["-g", str(2 * settings.target_fps), "-sc_threshold", "0"]

    # aresample=async=1 stretches or squeezes audio to match the timestamps,
    # squashing the slow clock drift that puts captions 300 ms behind the
    # mouth by minute 90 of a long recording. Then one canonical layout:
    # stereo 48 kHz AAC, whatever the input had.
    args += ["-af", "aresample=async=1"]
    args += ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-ac", "2", "-ar", "48000"]

    args += ["-c:v", "libx264", "-crf", str(settings.crf), "-preset", settings.x264_preset]
    args += ["-pix_fmt", "yuv420p"]
    # moov atom up front so browsers and streaming probes can start before the
    # last byte lands. -f mp4 is explicit because the output is written to a
    # .part path whose extension says nothing about the container.
    args += ["-movflags", "+faststart", "-f", "mp4", "-y", str(out)]
    return args


def build_extract_audio_cmd(mezz: Path, out: Path) -> list[str]:
    """The ffmpeg argument list that turns a mezzanine into a mono 16 kHz WAV."""
    return [
        "-i", str(mezz),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-y", str(out),
    ]  # fmt: skip


def _parse_filter_names(filters_output: str) -> frozenset[str]:
    """Filter names from the text of ``ffmpeg -filters``."""
    names: set[str] = set()
    for line in filters_output.splitlines():
        match = _FILTER_LINE.match(line)
        if match:
            names.add(match.group("name"))
    return frozenset(names)


@functools.lru_cache(maxsize=1)
def _ffmpeg_filters() -> frozenset[str]:
    """Every filter this ffmpeg build knows. Runs ``ffmpeg -filters`` once."""
    return _parse_filter_names(run_ffmpeg(["-filters"]).stdout)


def ffmpeg_has_filter(name: str) -> bool:
    """True when the ffmpeg on PATH was built with the named filter."""
    return name in _ffmpeg_filters()


def _encode_atomically(
    args: list[str], part: Path, out: Path, *, timeout: int = ENCODE_TIMEOUT_FLOOR_SECONDS
) -> None:
    """Run ffmpeg writing to ``part``, then rename onto ``out``; clean up on failure."""
    log.debug("running: ffmpeg %s", shlex.join(args))
    try:
        run_ffmpeg(args, timeout=timeout)
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def make_mezzanine(
    src: Path,
    out: Path,
    *,
    audio_track: int | None = None,
    info: VideoInfo | None = None,
) -> Path:
    """Re-encode ``src`` into the mezzanine at ``out`` and return ``out``.

    Cache-first: returns immediately when ``out`` exists. ``info`` skips the
    probe when the caller already has one. ``audio_track`` is the a:N index to
    keep; see :func:`choose_audio_track` for the default. HDR input on an
    ffmpeg without the zscale filter raises :class:`ToolchainError` rather
    than silently producing a washed-out file.
    """
    src = Path(src)
    out = Path(out)
    if out.exists():
        if audio_track is not None:
            # The cache is keyed by path alone, so an explicit track cannot be
            # honoured without a re-encode. Still reject an index that does
            # not exist when the info is at hand, and say what was ignored.
            if info is not None:
                choose_audio_track(info, audio_track)
            log.warning(
                "mezzanine: %s exists, reusing it as is; audio_track=%d is not applied "
                "to a cached mezzanine (delete it to re-encode with that track)",
                out,
                audio_track,
            )
        else:
            log.info("mezzanine: %s exists, reusing", out)
        return out
    info = info or probe(src)
    if info.is_hdr and not ffmpeg_has_filter("zscale"):
        raise ToolchainError(
            f"{src} is HDR (color_transfer={info.color_transfer}) and tone mapping it "
            "needs the zscale filter, but this ffmpeg was built without libzimg. "
            "Install an ffmpeg build that includes zscale (Homebrew's stock formula "
            "does not; a static build from BtbN or johnvansickle does) and retry. "
            "Refusing to convert without tone mapping: the result would be grey and washed."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg takes the output as its last positional argument, so the .part
    # path is anchored to the cwd: a bare relative "-o.mp4.part" would be
    # parsed as an option. ``out`` itself is returned as the caller gave it.
    part = out.absolute().with_name(out.name + PART_SUFFIX)
    args = build_mezzanine_cmd(src, part, info, audio_track)
    timeout = max(
        ENCODE_TIMEOUT_FLOOR_SECONDS, int(info.duration * ENCODE_TIMEOUT_PER_INPUT_SECOND)
    )
    _encode_atomically(args, part, out, timeout=timeout)
    log.info("mezzanine: wrote %s", out)
    return out


def extract_audio(mezz: Path, out: Path) -> Path:
    """Write the mono 16 kHz PCM WAV that transcription consumes; return ``out``.

    Extracted from the MEZZANINE, never from the original. WhisperX timestamps
    are measured against whatever audio it is handed, so ASR and video math
    have to share one clock: the mezzanine's, after aresample=async=1 and the
    constant-frame-rate encode. Feed it the original and the captions are
    right at minute 1 and visibly wrong at minute 90. Cache-first and atomic
    like :func:`make_mezzanine`.
    """
    mezz = Path(mezz)
    out = Path(out)
    if out.exists():
        log.info("audio: %s exists, reusing", out)
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.absolute().with_name(out.name + PART_SUFFIX)  # see make_mezzanine
    _encode_atomically(build_extract_audio_cmd(mezz, part), part, out)
    log.info("audio: wrote %s", out)
    return out
