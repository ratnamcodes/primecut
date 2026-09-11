"""ffprobe wrapper (Task 2).

Shells out to ffprobe and returns a typed description of a media file:
container, duration, video and audio stream codecs, resolution, frame rate,
sample rate, and channel count. Everything downstream (mezzanine
normalization, clip rendering, credit accounting) asks this module instead of
parsing ffprobe output itself. Filled in by Task 2.
"""
