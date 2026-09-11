# fixtures

Test media for PrimeCut. Short podcast clips used by the probe, mezzanine, and
transcribe tests arrive in Task 2; this directory is empty except for this file
until then.

How ignoring works: the root `.gitignore` ignores media everywhere
(`*.mp4 *.mov *.mkv *.wav *.m4a`) and then un-ignores `fixtures/**`. That means
media under this directory is not ignored by default, so each fixture file is an
explicit, reviewed choice to commit. Task 2 decides file by file what goes in.

Rules for anything added here:

- Keep it small. A few seconds to a minute of audio or video, low bitrate.
  Fixtures are cloned by everyone and sit in history forever.
- No real secrets. Nothing under `fixtures/` may contain API keys, tokens, or
  credentials in any form (filenames, metadata, sidecar files).
- No private recordings. Only media you have the right to publish; this repo is
  public.
