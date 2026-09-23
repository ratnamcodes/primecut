# fixtures

The adversarial fixture pack. Each file here breaks something specific later,
on purpose. This table is the reason a future you can reproduce a caption
drift bug in thirty seconds: find the row, fetch the file, run the test.

Add a row every time a bug finds you.

## The pack

| file | what it lies about | what breaks if you skip the mezzanine |
|---|---|---|
| `interview.mp4` | The control. Nothing exotic, but not what the pipeline assumes either: 25 fps (PAL) not 30, 44.1 kHz audio not 48 kHz, and yt-dlp's merger leaves the `moov` index at the *end* of the file (it is not within the first 500 KB). | Rendering at `target_fps=30` from a 25 fps source duplicates every fifth frame (judder on the verticals). 44.1 to 48 kHz resampling on every cut if not done once up front. A browser or a streaming `ffprobe` cannot play or inspect the file until the last byte lands. |
| `phone_portrait.mov` | Its orientation. Stored as **1920x1080 landscape** HEVC; a `rotation=-90` display matrix in the container is the only thing that makes it portrait (1080x1920). Five streams: video, audio, and three `mebx` "Core Media Metadata" data tracks that ffmpeg's default mapping silently drops (`-map 0` needs `-copy_unknown`). iPhone SE 2nd gen, so 8-bit SDR BT.709; newer iPhones add 10-bit HDR (HLG / Dolby Vision) on top. 29.97 fps, 34 s. | Anything that reads width/height and ignores the display matrix crops, letterboxes, or renders sideways. HEVC will not play in Chrome or Firefox `<video>`, so the dashboard preview is blank until transcoded. |
| `screenrec.mov` | The frame rate. Header says **120 fps**; 1,563 frames in 33.9 s is **46 fps**, and the gaps between frames take 21 distinct values from 8 ms to 858 ms (nothing on screen changed for almost a second). Trust the header and use frame index as time, and you are **20.75 s wrong by the end of a 34 s clip**. Also: no audio stream at all, 3024x1964 (Retina 2x, not a broadcast size). | Word timestamps and frame indices disagree more and more as the clip goes on, so captions drift and cuts land on the wrong frame. Frame extraction by index returns the wrong moment. Anything that assumes an audio stream exists crashes at probe. |
| `musicbed.mp4` | "Silence." Narration over a continuous music bed: the floor never drops below about -40 dB while the narrator is pausing. A -50 dB / 0.3 s gate finds exactly **one** silence in 60 s (the 0.9 s title-card transition at 13.7 s); a -35 dB gate finds four. The narrator pauses dozens of times. Integrated loudness -24.3 LUFS, true peak -7.9 dBTP. | dB-threshold silence detection reports "100% speech" and offers no cut points, or cuts on the one gap that is not a sentence boundary. Whisper hallucinates lyrics and phantom phrases over the bed (Task 3 confirms). |
| `sample.mkv` | The container. Same H.264 video and AAC audio as `interview.mp4`, first 120 s, remuxed into Matroska. Nothing about the *streams* changed. | An HTML `<video>` element will not play it at all, so the dashboard preview is blank. Any check that trusts the extension over `ffprobe` gets the codec wrong. |
| `corrupt.mp4` | Everything. The first 500,000 bytes of `interview.mp4`: the extension says video, the size says "a small clip", and there is no `moov` index because it lived at the end of the original. `ffprobe` says `error reading header` / `Invalid data found when processing input`. | The pipeline must fail at probe with a clear error instead of crashing minutes later in transcription or render, and must not charge credits for it. |
| `rtl.mp4` | The text, not the container. Urdu in Perso-Arabic script: right-to-left with contextual letter joining. YouTube's own `language` metadata field says `hi` (Hindi), which is wrong; only the audio knows. | Captions render left-to-right, letters come out disjoint, or a font without Arabic-script shaping falls back to boxes (Task 6). Language detection from metadata picks the wrong model. |

## Committed vs fetched

The root `.gitignore` ignores media everywhere and then un-ignores
`fixtures/**`, so every media file in this directory is **committed unless
`fixtures/.gitignore` says otherwise**. Anything over about 50 MB, or
rebuildable in one command from a file that is, stays out and gets an exact
rebuild command below.

| file | in git? | size | why |
|---|---|---|---|
| `interview.mp4` | no | 399 MB | over the line; fetch with yt-dlp |
| `sample.mkv` | no | 33 MB | one-second remux of `interview.mp4` |
| `musicbed.mp4` | yes | 21 MB | under the line; tests should run on a fresh clone |
| `rtl.mp4` | yes | 12 MB | |
| `corrupt.mp4` | yes | 488 KB | |
| `phone_portrait.mov` | no | 56 MB | personal recording of the repo owner; record your own |
| `screenrec.mov` | no | 41 MB | personal recording of the repo owner; record your own |

## Sources and attribution

Only Creative Commons or public-domain material, or recordings we own. This
repo is public; someone else's podcast is not ours to redistribute.

| file | source | license |
|---|---|---|
| `interview.mp4`, `sample.mkv`, `corrupt.mp4` | ANU TV, *An improbable dream: Andy Thomas in conversation with Professor Brian Schmidt* (2019), https://www.youtube.com/watch?v=CmBL1o9fH8M | CC BY (as set by the uploader on YouTube) |
| `musicbed.mp4` | NASA, *ScienceCasts: The Space We Travel Through* (2019), first 60 s, https://www.youtube.com/watch?v=EaW6VcOzLT4 | CC BY on NASA's channel; US government work, public domain |
| `rtl.mp4` | Wikitongues, *Seema speaking Urdu*, first 30 s, https://www.youtube.com/watch?v=pru-95YczT4 | CC BY (as set by the uploader on YouTube) |
| `phone_portrait.mov`, `screenrec.mov` | recorded by the repo owner | ours |

Licenses were read from YouTube's metadata (`yt-dlp -j --skip-download <url>`,
field `license`), not assumed from the search filter. Re-check before
swapping a source.

## Rebuilding the pack

Run from the repo root. Needs `yt-dlp` (`uv tool install yt-dlp`) and `ffmpeg`.

### interview.mp4 (not in git)

```bash
yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/mp4" --merge-output-format mp4 \
  -o "fixtures/interview.mp4" "https://www.youtube.com/watch?v=CmBL1o9fH8M"
```

Expect 1920x1080, 25 fps, H.264 + AAC 44.1 kHz stereo, 1507 s, about 399 MB.

### sample.mkv (not in git; needs interview.mp4)

```bash
ffmpeg -i fixtures/interview.mp4 -t 120 -c copy fixtures/sample.mkv
```

### corrupt.mp4 (in git; rebuild needs interview.mp4)

```bash
head -c 500000 fixtures/interview.mp4 > fixtures/corrupt.mp4
```

PowerShell:

```powershell
$b=[IO.File]::ReadAllBytes("fixtures/interview.mp4")[0..499999]; [IO.File]::WriteAllBytes("fixtures/corrupt.mp4",$b)
```

### musicbed.mp4 (in git)

```bash
yt-dlp -f "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/mp4" \
  --merge-output-format mp4 --download-sections "*00:00-01:20" --force-keyframes-at-cuts \
  -o "fixtures/musicbed_raw.mp4" "https://www.youtube.com/watch?v=EaW6VcOzLT4"
ffmpeg -ss 0 -t 60 -i fixtures/musicbed_raw.mp4 -c copy -movflags +faststart fixtures/musicbed.mp4
rm fixtures/musicbed_raw.mp4
```

### rtl.mp4 (in git)

```bash
yt-dlp -f "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/mp4" \
  --merge-output-format mp4 --download-sections "*00:00-00:30" --force-keyframes-at-cuts \
  -o "fixtures/rtl.mp4" "https://www.youtube.com/watch?v=pru-95YczT4"
```

### phone_portrait.mov (not in git; record your own)

About 20 s, phone held upright, talking to camera. Transfer **without**
conversion: AirDrop with "Keep Originals", or Settings > Photos > Transfer to
Mac or PC > Keep Originals. The reference file (kept locally, not committed) is an untouched iPhone SE (2nd
generation) camera original: HEVC 1920x1080 with `rotation=-90`, AAC 44.1 kHz,
three `mebx` metadata tracks, 34 s, 56 MB. Confirm the lie on any replacement:

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,pix_fmt:stream_side_data=rotation -of default=nw=1 fixtures/phone_portrait.mov
```

Expect `hevc`, a landscape width/height, and a non-zero `rotation`. A 10-bit
`pix_fmt` such as `yuv420p10le` means the phone also recorded HDR; add that to
the table if so.

### screenrec.mov (not in git; record your own)

QuickTime Player > File > New Screen Recording, about 30 s, moving things
around so the frame rate actually varies. The reference file (kept locally, not committed): H.264
3024x1964, 33.9 s, header 120 fps, 1,563 frames (~46 fps effective), no
audio stream. Confirm VFR on any replacement with
`ffprobe -show_streams fixtures/screenrec.mov | grep -E 'r_frame_rate|avg_frame_rate'`
(two different numbers) or, more directly, by listing frame timestamps:

```bash
ffprobe -v error -select_streams v:0 -show_entries frame=pts_time -of csv=p=0 fixtures/screenrec.mov | tr -d ',' | awk 'NR>1{d=$1-p; printf "%.0f\n", d*1000} {p=$1}' | sort -n | uniq -c | sort -rn | head
```

More than a handful of distinct gap values means variable frame rate.

## Quick check

```bash
for f in fixtures/*.mp4 fixtures/*.mkv fixtures/*.mov; do
  printf '%-28s ' "$f"; ffprobe -v error -show_entries format=duration:stream=codec_name -of csv=p=0 "$f" 2>&1 | tr '\n' ' '; echo
done
```

`corrupt.mp4` is *supposed* to print an error here.
