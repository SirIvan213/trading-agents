# ui/ — read-only local web UI

A local window onto what the pipeline records: the decision journal, run_day's state files
and the config. It runs alongside the trading phases and never blocks them. The rules are in
CLAUDE.md ("UI track"). In short, it:

- reads files only, never takes the journal's or the budget's lock, and never writes;
- is GET only, serves 127.0.0.1 only and makes no network calls;
- never imports order, broker or agent code;
- shows money values exactly as recorded and computes nothing that decides money.

## Run it

```
.venv\Scripts\python.exe -m pip install -r ui\requirements.txt
.venv\Scripts\python.exe -m ui.api            # http://127.0.0.1:8765/api/health
.venv\Scripts\python.exe -m ui.api --port 8800
```

There is no `--host` option.

## Layout

```
ui/api/            FastAPI app (app.py) and launcher (__main__.py)
ui/api/readers/    standard-library readers: shared_open, journal_snapshot, state, config
ui/tests/          pytest; journal fixtures are written by the journal's own code
ui/web/            React frontend (UI-2, #215)
```

## Tests

```
.venv\Scripts\python.exe -m pytest -q ui/tests tests/test_ui_readonly.py
```

`tests/test_ui_readonly.py` also runs in the required CI job, using the standard library only.
The rest runs in the non-required `ui` workflow (`.github/workflows/ui.yml`) on Ubuntu and
Windows.

## Why files are read the way they are

- **The journal** is read without its lock: `Journal.entries()` takes the lock that approvals
  write under, so a polling reader could make a real approval time out. An incomplete last
  line is a write in progress. If it stays unchanged for 5 s it is reported as cut off, and
  the journal itself then refuses every read. A bad line is reported, and nothing after it is
  trusted.
- **Day totals** come from `SnapshotJournal`: the journal's own `orders()` and `day_state()`
  over the snapshot. They are the same numbers `risk_check` sees.
- **Every file** is opened with `CreateFileW` sharing read, write and delete (`shared_open.py`).
  The UI therefore never blocks the journal's appends, `journal.repair`'s truncate, or a delete.
- **The budget file is never opened yet.** On Windows, `os.replace` cannot replace a file that
  any process holds open, even with delete sharing, and a failed budget write stops run_day's
  day. #213 makes that write rename with POSIX semantics, which succeeds over this reader, and
  retry. Until it lands, the UI only checks from the directory listing that the file exists.
