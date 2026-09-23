"""Probe every file in the fixture pack and print one table, or build the
mezzanine for one fixture and show before/after.

    uv run python scripts/ingest_fixtures.py
    uv run python scripts/ingest_fixtures.py --mezz phone_portrait.mov
    uv run python scripts/ingest_fixtures.py --mezz obs.mkv --audio-track 1

Run ``--mezz`` from ``api/`` so the default ``--work-dir`` resolves to
``api/.primecut_work/``, the directory every later task reads from, and so
``primecut.config`` finds ``api/.env``. The probe-only table needs neither
and runs from any directory. A file ffprobe cannot read gets one
``UNREADABLE`` row instead of killing the run.
"""

import argparse
import importlib.util
import logging
import signal
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

# `uv run` installs the package, but a bare `python scripts/ingest_fixtures.py`
# from anywhere should work too.
if importlib.util.find_spec("primecut") is None:
    sys.path.insert(0, str(API_DIR))

# primecut.mezzanine imports primecut.config, which loads .env from the cwd
# and fails when it is not there. Only --mezz needs it, so it is imported
# inside ingest_one() and the probe-only table works from any directory.
from primecut.probe import FFmpegError, UnreadableMediaError, VideoInfo, probe  # noqa: E402

COLUMNS = [
    "file",
    "container",
    "vcodec",
    "acodec",
    "resolution",
    "fps",
    "vfr",
    "rotation",
    "hdr",
    "audio",
    "duration",
]
RIGHT_ALIGNED = {"fps", "rotation", "audio", "duration"}

# A table row is either one string per column, or [name, "UNREADABLE: ..."].
Row = list[str]


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def info_row(label: str, info: VideoInfo) -> Row:
    has_video = info.video_codec is not None
    return [
        label,
        info.container.split(",")[0],
        info.video_codec or "-",
        info.audio_codec or "-",
        f"{info.width}x{info.height}" if has_video else "-",
        f"{info.fps:.2f}" if has_video else "-",
        _yes_no(info.is_vfr),
        str(info.rotation),
        _yes_no(info.is_hdr),
        str(info.audio_track_count),
        f"{info.duration:.1f}",
    ]


def unreadable_row(label: str, exc: Exception) -> Row:
    first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return [label, f"UNREADABLE: {first_line}"]


def render_table(rows: list[Row]) -> str:
    widths = [len(name) for name in COLUMNS]
    for row in rows:
        for i, cell in enumerate(row if len(row) == len(COLUMNS) else row[:1]):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        out = []
        for name, width, cell in zip(COLUMNS, widths, cells):
            out.append(cell.rjust(width) if name in RIGHT_ALIGNED else cell.ljust(width))
        return "  ".join(out).rstrip()

    lines = [fmt(COLUMNS)]
    for row in rows:
        if len(row) == len(COLUMNS):
            lines.append(fmt(row))
        else:
            lines.append(f"{row[0].ljust(widths[0])}  {row[1]}")
    return "\n".join(lines)


def probe_row(label: str, path: Path) -> Row:
    try:
        return info_row(label, probe(path))
    except (UnreadableMediaError, FFmpegError) as exc:
        return unreadable_row(label, exc)


def probe_all(fixtures: Path) -> int:
    files = sorted(
        p for p in fixtures.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix != ".md"
    )
    if not files:
        print(f"no fixtures found in {fixtures}", file=sys.stderr)
        return 1
    print(render_table([probe_row(p.name, p) for p in files]))
    return 0


def ingest_one(fixtures: Path, name: str, work_dir: Path, audio_track: int | None) -> int:
    from primecut.mezzanine import ToolchainError, extract_audio, make_mezzanine

    src = fixtures / name
    if not src.is_file():
        available = ", ".join(sorted(p.name for p in fixtures.iterdir() if p.is_file()))
        print(f"{src} does not exist. Fixtures available: {available}", file=sys.stderr)
        return 2
    work = work_dir / src.stem
    # Task 3 and the Phase 1 pipeline address these two by exact name.
    mezz_path = work / "mezzanine.mp4"
    wav_path = work / "audio.wav"
    try:
        info = probe(src)
        mezz = make_mezzanine(src, mezz_path, audio_track=audio_track, info=info)
        wav = extract_audio(mezz, wav_path)
    except (UnreadableMediaError, FFmpegError, ToolchainError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rows = [
        info_row(f"{name} (original)", info),
        probe_row("mezzanine.mp4", mezz),
        probe_row("audio.wav", wav),
    ]
    print(render_table(rows))
    print()
    print(f"mezzanine: {mezz}")
    print(f"audio:     {wav}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=REPO_ROOT / "fixtures",
        help="directory of input files (default: <repo>/fixtures)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".primecut_work"),
        help="where <stem>/mezzanine.mp4 and <stem>/audio.wav go (default: ./.primecut_work)",
    )
    parser.add_argument(
        "--mezz",
        metavar="NAME",
        help="build the mezzanine and audio.wav for this one fixture instead of probing all",
    )
    parser.add_argument(
        "--audio-track",
        type=int,
        default=None,
        metavar="N",
        help="with --mezz: keep audio track a:N instead of the automatic choice",
    )
    args = parser.parse_args(argv)

    # stderr, so the audio-track choice and the silent-track warning are
    # visible without polluting the table on stdout.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.mezz:
        return ingest_one(args.fixtures, args.mezz, args.work_dir, args.audio_track)
    return probe_all(args.fixtures)


def _exit_on_sigterm(signum: int, frame: object) -> None:
    # docker/systemd/k8s stop a worker with SIGTERM. Python's default
    # disposition dies without unwinding, which orphans a running ffmpeg and
    # leaves it writing the .part file. Raising SystemExit instead lets
    # subprocess.run kill the child and _encode_atomically remove the .part,
    # exactly as happens on Ctrl-C.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    sys.exit(main())
