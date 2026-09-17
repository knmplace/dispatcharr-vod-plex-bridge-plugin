# v2.5.2 Release Notes

Covers everything since the last committed release (v2.5.1, `c660bfe`).

---

## Callout: orphaned STRM folders are now reviewable, never auto-removed

Investigating a report of stale playback behavior turned up 23 leftover movie
folders on disk under `strm_output_dir` that were no longer tracked as
activated (likely left behind by a past cleanup gap, not this release). The
user was explicit: they don't trust automatic removal of "stale" folders and
want to review and choose what gets deleted themselves. This release adds
exactly that — a scan, never a sweep.

## Features

- **Orphaned STRM Folders panel (Health tab)** — new panel lists every movie
  and series-show folder on disk under `strm_output_dir` that isn't
  referenced by any currently-tracked activation (`bridge.py`:
  `scan_orphan_strm_folders()`). Movies are compared against
  `self._activated[...]["strm_folder"]`; series are compared per-category
  against `self._episodes_activated[...]["strm_folder"]` (matched at the
  show-folder level, since episodes for the same show share one folder).
  Scanning is read-only and safe to run any time — it never touches disk.
- **Manual "Remove Selected" action** — a checkbox-selectable table
  (select-all supported) with a `remove_orphan_strm_folders()` backend call
  that deletes **only** the folder names explicitly passed in from the
  dashboard selection. No automatic, scheduled, or reconcile-triggered
  deletion exists anywhere in this feature — removal only ever happens from
  an explicit user click, and the browser confirms the file list before
  sending the request.
- **Defense in depth on removal** — before deleting anything,
  `remove_orphan_strm_folders()` re-scans and re-validates that each
  requested folder is *still* an orphan (rejects anything reactivated or
  already removed since the last scan) and resolves its real path to confirm
  it stays inside the configured `strm_output_dir` (rejects path traversal).
  New routes: `GET /api/maintenance/orphan-strm`, `POST
  /api/maintenance/orphan-strm/remove`.

## Not changed

- **No change to activation, deactivation, or the existing
  `_reconcile_removed_movies()` cleanup path.** This release only adds
  visibility and a manual cleanup tool for folders that path may have missed
  in the past — it doesn't alter when or how folders are normally created or
  removed during activation/deactivation.
- **Series folder cleanup remains untriggered today** — the live catalog's
  series folders were confirmed empty at the time this was built, so this
  feature is mainly forward-looking for series; it was still implemented and
  tested since a future cleanup gap could affect series the same way it did
  movies.

## Testing

- New unit test file `tests/test_orphan_strm_scan.py` (7 tests, all passing):
  movie-folder orphan detection, series show-folder orphan detection,
  activated folders correctly excluded, empty-directory handling, removal
  scoped to only the requested folder, refusal to remove a folder no longer
  orphaned, refusal of a path-traversal attempt.
- Run via direct module load (`importlib.util.spec_from_file_location`)
  against a lightweight duck-typed stand-in object, same pattern as the
  existing duplicate-grouping tests — no Django/DB dependency required.
- **Known pre-existing issue, not introduced by this change**: `pytest`
  itself currently fails to collect/run any test in this suite in this dev
  environment (`ImportError: attempted relative import with no known parent
  package` from `__init__.py`, triggered by pytest's own import machinery).
  Confirmed via `git stash` that this reproduces identically on a clean
  checkout of `main` before this change — a separate, pre-existing
  environment issue, not something this release fixes or works around. All
  test runs above were done via direct module execution, bypassing pytest's
  broken collection step.
- Not yet deployed/live-verified on the test bed (192.168.1.245) — pending
  deploy approval.

## Tracking

- Bead: `ycjh` (feature implementation).
