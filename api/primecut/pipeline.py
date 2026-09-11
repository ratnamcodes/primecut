"""Resumable orchestrator (Task 7).

Runs the full flow (probe, mezzanine, transcribe, rank, caption, render) as a
sequence of idempotent stages whose outputs are cached in the work directory,
so a crash or a tweak to one stage resumes from the last good artifact rather
than starting over. Filled in by Task 7.
"""
