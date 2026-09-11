"""The transcript contract (Task 4).

Pydantic models for words, segments, speakers, and the full transcript that
every stage after transcription reads and writes, plus loaders and savers for
the on-disk JSON form. Ranking, captioning, and composition all depend on this
shape, so it is defined once here. Filled in by Task 4.
"""
