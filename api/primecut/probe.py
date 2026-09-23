"""Media inspection: the ffprobe/ffmpeg subprocess helpers and a typed
:class:`VideoInfo` describing one input file.

Every FFmpeg call in this codebase goes through :func:`run_ffmpeg` or
:func:`run_ffprobe`. They take argument LISTS and never a shell string, so a
filename containing spaces, quotes, or semicolons stays a filename instead of
becoming a command. Failures surface as :class:`FFmpegError`, whose message is
the exact command followed by the tail of stderr, and files ffprobe cannot
make sense of surface as :class:`UnreadableMediaError`, whose message starts
with a sentence a user can act on.

:func:`probe` runs ffprobe and hands the JSON to :meth:`VideoInfo.from_ffprobe`,
a pure parser that the tests feed with recorded output. Everything downstream
(mezzanine normalization, clip rendering, credit accounting) asks this module
instead of parsing ffprobe output itself.
"""

import json
import logging
import shlex
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel

__all__ = [
    "AudioTrack",
    "FFmpegError",
    "HDR_TRANSFERS",
    "ToolchainError",
    "UnreadableMediaError",
    "VideoInfo",
    "probe",
    "run_ffmpeg",
    "run_ffprobe",
]

log = logging.getLogger(__name__)

# How much of stderr an FFmpegError keeps. FFmpeg's actual complaint is always
# in the last few lines; the megabytes of per-frame progress above it are not.
STDERR_TAIL_CHARS = 4000

# ffprobe on a local file returns in well under a second. The timeout is only a
# safety net against a hung read from a network mount.
PROBE_TIMEOUT_SECONDS = 600

# color_transfer values that mean HDR: PQ (smpte2084) and HLG (arib-std-b67).
# Everything else, including the "unknown" many encoders leave, is treated as
# SDR BT.709.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})

# How far r_frame_rate and avg_frame_rate may disagree, as a fraction of
# r_frame_rate, before a file counts as variable frame rate. The phone clip
# (30000/1001 vs 610800/20381, off by 0.003%) is well inside; the screen
# recording (120 vs 45.8) is nowhere near.
VFR_TOLERANCE = 0.01


class ToolchainError(RuntimeError):
    """ffmpeg/ffprobe is missing from PATH, or was built without a feature we need."""


class FFmpegError(RuntimeError):
    """An ffmpeg or ffprobe invocation failed.

    Carries the exact ``cmd`` list, the ``returncode``, and the last
    :data:`STDERR_TAIL_CHARS` of ``stderr``. ``str(err)`` is the command on the
    first line (shell-quoted, so it can be pasted back into a terminal), then
    the stderr tail; a ``note`` (the timeout case) goes between the two. The
    exit status is the ``returncode`` attribute. It is -1 when the process was
    killed on timeout, since there is no exit status.
    """

    def __init__(
        self,
        cmd: list[str],
        returncode: int,
        stderr: str,
        *,
        note: str | None = None,
    ) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = (stderr or "")[-STDERR_TAIL_CHARS:]
        self.note = note
        super().__init__(str(self))

    def __str__(self) -> str:
        lines = [shlex.join(self.cmd)]
        if self.note:
            lines.append(self.note)
        tail = self.stderr.strip()
        if tail:
            lines.append(tail)
        return "\n".join(lines)


class UnreadableMediaError(FFmpegError):
    """ffprobe could not make sense of the file.

    The message starts with a plain sentence naming the file, so a pipeline
    can show it to a user as-is; the ffprobe command and stderr tail follow
    underneath for whoever has to debug it. ``cmd`` is empty when the failure
    was found while parsing already-returned JSON rather than by ffprobe.
    """

    def __init__(
        self,
        path: Path | str,
        cmd: list[str] | None = None,
        returncode: int = 0,
        stderr: str = "",
        *,
        note: str | None = None,
    ) -> None:
        self.path = Path(path)
        super().__init__(cmd or [], returncode, stderr, note=note)

    def __str__(self) -> str:
        lines = [f"cannot read {self.path}: not a media file, or the file is truncated"]
        if self.cmd:
            lines.append(super().__str__())
        elif self.note:
            lines.append(self.note)
        return "\n".join(lines)


def _run(cmd: list[str], *, timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one already-prefixed command. List in, never a shell string."""
    log.debug("running: %s", shlex.join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        raise ToolchainError(
            f"{cmd[0]} was not found on PATH. Install FFmpeg (macOS: brew install ffmpeg) and retry."
        ) from None
    except subprocess.TimeoutExpired as exc:
        # The partial stderr on the exception is bytes even in text mode.
        partial = exc.stderr
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        raise FFmpegError(
            cmd, -1, partial or "", note=f"timed out after {timeout} s and was killed"
        ) from None


def run_ffprobe(args: list[str]) -> dict[str, Any]:
    """Run ``ffprobe -v error <args>`` and return its JSON output as a dict.

    Callers pass ``-print_format json`` themselves along with whatever
    ``-show_*`` sections they need; the input path goes last, as ffprobe wants
    it anyway. A non-zero exit raises :class:`FFmpegError`; output that is not
    JSON raises :class:`UnreadableMediaError`.
    """
    cmd = ["ffprobe", "-v", "error", *args]
    proc = _run(cmd, timeout=PROBE_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        data = None
        reason = str(exc)
    else:
        reason = f"got {type(data).__name__}"
    if not isinstance(data, dict):
        raise UnreadableMediaError(
            args[-1] if args else "<no input>",
            cmd,
            proc.returncode,
            proc.stderr,
            note=f"ffprobe printed something that is not a JSON object ({reason})",
        )
    return data


def run_ffmpeg(args: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    """Run ``ffmpeg -hide_banner -nostdin <args>`` and return the completed process.

    ``-nostdin`` matters: without it a background ffmpeg that gets a stray
    keypress on the terminal pauses or quits, and a job worker with no
    terminal at all can hang on the read. A non-zero exit raises
    :class:`FFmpegError`; exceeding ``timeout`` seconds kills the process and
    raises the same error with a note saying so.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *args]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr)
    return proc


class AudioTrack(BaseModel):
    """One audio stream of an input, as later stages need to refer to it."""

    # Position among the AUDIO streams only: the N in ``-map 0:a:N``.
    index: int
    # Absolute position in ffprobe's stream list, for cross-referencing logs.
    stream_index: int
    codec: str | None
    channels: int
    sample_rate: int
    # ``tags.language`` verbatim (ISO 639-2: "eng", or "und" for undetermined,
    # which MOV/MP4 muxers write when nothing was set), or None when absent.
    language: str | None
    # ``tags.title`` (Matroska) or ``tags.name`` (MOV/MP4 store the same thing
    # under that key), or None. OBS labels its tracks "Mic/Aux", "Desktop
    # Audio", and so on; this is how a human tells them apart.
    title: str | None
    # ``disposition.default``: the track a player picks when nobody chooses.
    is_default: bool

    def label(self) -> str:
        """Short human description for logs and error messages."""
        return (
            f"a:{self.index} ({self.codec}, {self.channels} ch, "
            f"lang={self.language}, title={self.title!r})"
        )


class VideoInfo(BaseModel):
    """Everything the pipeline needs to know about an input before touching it."""

    path: Path
    duration: float
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None
    # ffprobe's format_name, verbatim. MOV/MP4 comes back as the demuxer's
    # whole alias list ("mov,mp4,m4a,3gp,3g2,mj2"); take the first token when
    # a single word is wanted.
    container: str
    bitrate: int | None
    # Parsed from r_frame_rate as a fraction: "30000/1001" is 29.97, not 30000.
    fps: float
    r_frame_rate: str
    avg_frame_rate: str
    # True when r_frame_rate and avg_frame_rate disagree by more than
    # VFR_TOLERANCE. On a VFR file the frame timestamps are irregular, so any
    # ``frame = time * fps`` arithmetic drifts further the deeper into the
    # file you go (20 s off by the end of the 34 s screen recording). That is
    # why nothing downstream ever touches the original: the mezzanine is
    # re-encoded at a constant rate first.
    is_vfr: bool
    # Clockwise degrees the stored frame must be turned to display upright,
    # normalized to 0/90/180/270. Only the mezzanine step cares; it bakes the
    # rotation into the pixels and every later stage sees an upright frame.
    rotation: int
    # color_transfer is PQ or HLG. Such footage needs tone mapping before it
    # can be encoded as 8-bit BT.709 without coming out grey and washed.
    is_hdr: bool
    color_transfer: str | None
    color_primaries: str | None
    pix_fmt: str | None
    has_audio: bool
    audio_track_count: int
    audio_tracks: list[AudioTrack]

    @classmethod
    def from_ffprobe(cls, data: dict[str, Any], path: Path) -> "VideoInfo":
        """Build a VideoInfo from ``ffprobe -show_streams -show_format`` JSON.

        Pure: no subprocess, so tests can feed recorded output. Raises
        :class:`UnreadableMediaError` when the JSON has no streams or no
        duration can be found anywhere, which is what a truncated file or a
        non-media file looks like after ffprobe has given up on it.
        """
        path = Path(path)
        streams = data.get("streams") if isinstance(data, dict) else None
        if not streams:
            raise UnreadableMediaError(path, note="ffprobe reported no streams")
        fmt: dict[str, Any] = data.get("format") or {}

        # An MP3 with embedded cover art carries a one-frame "video" stream
        # flagged attached_pic. That is still an audio-only input.
        video = next(
            (
                s
                for s in streams
                if s.get("codec_type") == "video"
                and not _disposition_flag(s, "attached_pic")
            ),
            None,
        )
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        duration = _to_float(fmt.get("duration"))
        if duration is None and video is not None:
            duration = _to_float(video.get("duration"))
        if duration is None and audio_streams:
            duration = _to_float(audio_streams[0].get("duration"))
        if duration is None:
            raise UnreadableMediaError(path, note="no duration in the format or any stream")

        tracks = [
            AudioTrack(
                index=n,
                stream_index=int(s.get("index", n)),
                codec=s.get("codec_name"),
                channels=_to_int(s.get("channels")) or 0,
                sample_rate=_to_int(s.get("sample_rate")) or 0,
                language=_language(s),
                title=_title(s),
                is_default=_disposition_flag(s, "default"),
            )
            for n, s in enumerate(audio_streams)
        ]

        if video is None:
            r_rate, avg_rate = "0/0", "0/0"
        else:
            r_rate = str(video.get("r_frame_rate") or "0/0")
            avg_rate = str(video.get("avg_frame_rate") or "0/0")
        r_frac = _parse_rate(r_rate)
        avg_frac = _parse_rate(avg_rate)
        is_vfr = (
            r_frac is not None
            and avg_frac is not None
            and abs(r_frac - avg_frac) / r_frac > VFR_TOLERANCE
        )

        color_transfer = video.get("color_transfer") if video is not None else None

        return cls(
            path=path,
            duration=duration,
            width=(_to_int(video.get("width")) or 0) if video is not None else 0,
            height=(_to_int(video.get("height")) or 0) if video is not None else 0,
            video_codec=video.get("codec_name") if video is not None else None,
            audio_codec=tracks[0].codec if tracks else None,
            container=str(fmt.get("format_name") or ""),
            bitrate=_to_int(fmt.get("bit_rate")),
            fps=float(r_frac) if r_frac is not None else 0.0,
            r_frame_rate=r_rate,
            avg_frame_rate=avg_rate,
            is_vfr=is_vfr,
            rotation=_rotation(video) if video is not None else 0,
            is_hdr=color_transfer in HDR_TRANSFERS,
            color_transfer=color_transfer,
            color_primaries=video.get("color_primaries") if video is not None else None,
            pix_fmt=video.get("pix_fmt") if video is not None else None,
            has_audio=bool(tracks),
            audio_track_count=len(tracks),
            audio_tracks=tracks,
        )


def probe(path: Path | str) -> VideoInfo:
    """Inspect a media file with ffprobe.

    Raises :class:`FileNotFoundError` when the path does not exist and
    :class:`UnreadableMediaError` when ffprobe fails or returns nothing
    usable, so a truncated upload fails here, in seconds, rather than minutes
    later inside transcription or render.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no such file: {path}")
    # The input is ffprobe's last positional argument, so a bare relative name
    # like "-x.mp4" would be parsed as an option and misreported as unreadable.
    # Anchoring it to the cwd keeps the filename a filename; ``path`` itself
    # (and VideoInfo.path) stays as the caller gave it.
    args = ["-print_format", "json", "-show_streams", "-show_format", str(path.absolute())]
    try:
        data = run_ffprobe(args)
    except FFmpegError as exc:
        # Covers both the non-zero exit and the not-JSON case; either way the
        # user-facing story is the same sentence, with the details underneath.
        raise UnreadableMediaError(path, exc.cmd, exc.returncode, exc.stderr, note=exc.note) from None
    return VideoInfo.from_ffprobe(data, path)


# --- parsing helpers ---------------------------------------------------------


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _parse_rate(text: str | None) -> Fraction | None:
    """Turn ffprobe's "num/den" into a Fraction; "0/0", empty, or junk is None."""
    if not text:
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _disposition_flag(stream: dict[str, Any], name: str) -> bool:
    disposition = stream.get("disposition") or {}
    return _to_int(disposition.get(name)) == 1


def _language(stream: dict[str, Any]) -> str | None:
    # Verbatim, "und" included: it is what the file says, and the log line
    # that shows which track was kept should say the same.
    tags = stream.get("tags") or {}
    return tags.get("language") or None


def _title(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("title") or tags.get("name") or None


def _rotation(stream: dict[str, Any]) -> int:
    """Clockwise display rotation in {0, 90, 180, 270}.

    The display matrix (``side_data_list[].rotation``) is what modern muxers
    write and what ffmpeg's autorotate honours. ffprobe reports it as the
    counter-clockwise angle, so the iPhone's portrait clip shows up as -90;
    negating it gives the clockwise 90 everyone else means by "rotate 90".
    The legacy ``tags.rotate`` string is already clockwise.
    """
    clockwise: float | None = None
    for entry in stream.get("side_data_list") or []:
        if "rotation" in entry:
            value = _to_float(entry.get("rotation"))
            if value is not None:
                clockwise = -value
                break
    if clockwise is None:
        legacy = _to_float((stream.get("tags") or {}).get("rotate"))
        if legacy is not None:
            clockwise = legacy
    if clockwise is None:
        return 0
    # Snap to the nearest quarter turn: real files carry -90.0 or "90", but
    # a hand-edited matrix can produce 89.99.
    return int(round(clockwise / 90.0)) % 4 * 90
