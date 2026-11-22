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
cd ui\web; npm ci; npm run build; cd ..\..       # Node 24 (ui/web/.nvmrc)
.venv\Scripts\python.exe -m ui.api            # http://127.0.0.1:8765/
.venv\Scripts\python.exe -m ui.api --port 8800
```

There is no `--host` option. The server serves the built page (`ui/web/dist`) and the API on
the same origin. Without a build, `/` says how to build it.

For frontend work, run `npm run dev` in `ui/web` (Vite on 127.0.0.1:5173, with `/api` proxied
to `python -m ui.api`).

Pages: `#/` How it works, `#/live` Live run (UI-5), `#/history` History (UI-3) and
`#/decision/<id>` Decision detail (UI-4). The last three say "coming" until built.

## Layout

```
ui/api/            FastAPI app (app.py) and launcher (__main__.py)
ui/api/readers/    standard-library readers: shared_open, journal_snapshot, state, config
ui/tests/          pytest; journal fixtures are written by the journal's own code
ui/api/content/    static page content (the 8 steps, glossary, phases)
ui/fixtures/api/   golden API responses: the frontend tests render them, the API tests check
                   the real endpoints against them
ui/web/            React + TypeScript frontend (Vite); hash routes, no router library
```

## Tests

```
.venv\Scripts\python.exe -m pytest -q ui/tests tests/test_ui_readonly.py
```

```
cd ui\web; npx tsc --noEmit; npx vitest run; npx vite build
```

On Windows, run Vitest from a path with an upper-case drive letter (`C:\...`). From `c:\...`
it loads its own runner twice and every suite fails with "failed to find the current suite".

After a deliberate change to an API response, regenerate its golden file with
`UPDATE_GOLDEN=1 .venv\Scripts\python.exe -m pytest -q ui/tests/test_explainer.py`.

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
- **The budget file is read only through `open_shared`**, one read at a time, never held
  open.
  - On Windows, `os.replace` cannot replace a file that any process holds open, even with
    delete sharing.
  - run_day's state writes (`tools/atomic_write.py`, #213) rename with POSIX semantics, which
    succeeds over this reader. When that call is unavailable they fall back to `os.replace`,
    which fails over any open handle, this one included. The replace is retried for at most
    0.5 s, and a budget write that still fails stops run_day's day. So never open the budget
    file any other way, and keep each read short.
  - `/api/health` needs only whether the file exists, which it reads from the folder's
    listing. The Live run page (UI-5, #218) will read its contents.
- **The event caches (`journal/cache/`) stay unread** until a UI PR needs them, and then only
  through `open_shared`. A cache write that fails costs another billed, capped fetch.
