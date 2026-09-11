"""Gemini highlight ranking (Task 5).

Sends the transcript to the ranking model (``settings.ranking_model``) with the
user's rubric, gets back scored candidate moments with start and end times,
and enforces the clip length bounds from settings. The single ranking produced
here feeds both the best-of cut and the vertical shorts. Filled in by Task 5.
"""
