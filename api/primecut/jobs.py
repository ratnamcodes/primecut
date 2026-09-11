"""Job state machine (Phase 3).

Persistent job records and their lifecycle (queued, running, failed, done)
backed by Postgres via ``settings.database_url``, with credit accounting per
source minute and hooks for the web app to poll progress. Filled in by
Phase 3.
"""
