# Developing Mindustry Launcher

[简体中文](DEVELOPING.zh_CN.md) · **English**

Developer documentation: how to run from source, the project layout, the verification
workflow, the release process, and the internals behind a few design decisions.

For **what the launcher does and how to use it**, see [README.md](README.md).

---

## Running from source

| Requirement | Notes |
|---|---|
| Python 3.10+ | The code uses `X \| None`, `list[dict]` and similar syntax |
| tkinter | Must be able to **actually create a Tk window**, not merely be importable |
| PyInstaller | Only needed to build the exe: `python -m pip install pyinstaller` |

```bash
python MindustryLauncher.py
```

⚠️ tkinter is a hard requirement — the build needs to derive the location of the
tcl/tk runtime from the interpreter. If `python -c "import tkinter; tkinter.Tk()"`
errors out, use a different interpreter (Anaconda ships one; the official Windows
installer does too).

## Project layout

```
MindustryLauncher.py    Entry point (~20-line wrapper; the real code lives in launcher/)
Launcher.spec           PyInstaller build config
mindustry.ico           Window icon
lang/                   UI strings (zh_CN.json / en_US.json; drop a copy next to the exe to override)
launcher/               All source code
    version.py          ★ The single source of version numbers + compatibility constants
    sources.py          ★ Version-source registry (where game versions come from; add sources without code changes)
    extensions.py       ★ Extension points (add features without repackaging)
    i18n.py             ★ UI string lookup (t / language packs / system-language detection)
    utils.py            Paths, logging, atomic writes, recycle bin
    config.py           Config read/write (save profiles + jvm launch config + migration chain)
    storage.py          CAS store, backups, runtime jar assembly
    updates.py          Update checking and downloading (the game itself)
    selfupdate.py       ★ Launcher self-update (check → download slim package → swap files on exit)
    gamecmd.py          Launch-argument parsing + command-line assembly
    gamelog.py          Game output capture (to disk + in-memory buffer + pipe-encoding fallback)
    gui_core.py         GUI core: thread queue, status bar, window skeleton
    gui_*.py            Feature pages (main / launch / versions / profiles / backups / log / settings)
_tools/                 Developer scripts (not packaged, not part of the app)
    _paths.py           ★ Shared path source for scripts
    build.py            One-shot build + deploy
    make_source_zip.py  Build the source zip
    make_release_zip.py Build the release zip
    recycle.py          Safe delete: move to the recycle bin, never hard-delete
    verify/             Regression and verification scripts
LICENSE                 Full GNU GPL-3.0 text
```

## Development workflow

```bash
python _tools/verify/code_regression.py          # 1. Run after any code change (344 checks)
python _tools/verify/i18n_check.py               # 1b. After touching strings/features: language-pack and "no hardcoded strings" gate (35 checks)
python _tools/verify/gui_smoke.py                # 2. After touching the GUI: really build windows and click through (116 checks, no game launched)
python _tools/verify/i18n_switch_smoke.py        # 2b. After touching strings/languages: really build windows and switch language (29 checks)
python _tools/verify/selfupdate_check.py         # 3. After touching self-update: offline check/swap/rollback run (92 checks)
python _tools/recycle.py dist/Mindustry启动器     # 4. ★ Clear build output before packaging
python _tools/build.py --deploy                  # 5. Build + sync to the deploy directory
python _tools/verify/packed_code_check.py        # 6. Confirm the new code really made it into the exe
python _tools/verify/exe_edge_check.py           # 7. After touching startup/config/log paths: packaged-build edge-case smoke test
```

- **Step 4 is not optional.** Before COLLECT, PyInstaller empties `dist/Mindustry启动器`
  (1000+ files), which trips the tooling's "bulk delete confirmation gate" (≥ 50 files in a
  single round requires manual confirmation) and the build **fails outright**. `recycle.py`
  goes through the native `SHFileOperationW` API, so it bypasses the gate — and files really
  do land in the recycle bin.
- **Step 6 is not optional.** If source changed but did not make it into the package, or the
  spec missed a module, the exe still starts, the window still appears, and nothing is
  reported — it just runs the old logic. "It opens" proves nothing.
- Deployment is **incremental**: `_internal/` holds close to a thousand files, so recopying
  the whole directory is slow and trips the delete gate. Only genuinely differing files are
  copied. `--deploy-to <dir>` picks a target; `--prune` additionally removes stale files.
- For what each script does and when to run it, see `_tools/README.md`.

## Building and delivering

| Artifact | Command | Size | For whom |
|---|---|---|---|
| **Source zip** | `python _tools/make_source_zip.py` | ~360 KB | People who want to build it or read the code |
| **Release zip** | `python _tools/make_release_zip.py` | ~31 MB | People who just want to double-click (no Python / Java needed) |
| **Update zip** | produced by the command above | ~2 MB | **People on an older version** — the launcher downloads it during self-update |

A release zip = exe + `_internal/` + bundled `jre/` + `使用说明.txt` + `LICENSE` + a program
file manifest (`manifest.json`). Extract and run.

Publishing a release means **two assets**: the full package for new users, the update package
for existing ones. The update package carries only the files that **actually changed** relative
to the previous version (the exe changes almost every time; `_internal/` changes only when
dependencies, Python, or PyInstaller change). The diff is computed from the `manifest.json`
shipped inside the previous release — which is why **every full package carries one**, with a
local copy kept at `_history/releases/manifest-<version>.json`.

```bash
# Normal release: the baseline is auto-picked as the highest recorded version below the current one
python _tools/make_release_zip.py

# Previous version shipped no manifest.json (1.0.0 did not — self-update did not exist yet)
# → measure its release package instead
python _tools/make_release_zip.py --baseline-from-zip previous-release.zip --baseline-version 1.0.0

python _tools/make_release_zip.py --no-update     # full package only
```

⚠️ With no baseline it **degrades to a full update package** (tens of MB) — bigger, but it can
never miss a file. Run the acceptance check before shipping:

```bash
python _tools/verify/release_check.py
```

It extracts into a **pristine empty directory**, launches from a **completely unrelated cwd**,
and asserts that key dependencies are present and that data lands next to the exe
(⚠️ it pops a GUI window for about 10 seconds). Reading the zip's file list cannot prove
"this really runs on someone else's machine" — only an actual launch can.

## Configuration internals

**`config.json` is the only config file**, auto-created next to the exe on first run. The full
structure, defaults, and fault-tolerance rules live in `launcher/config.py`. Three conventions
that decide whether a future release can still read an old config:

- **`config_version` is the format version of the config**, not the app version. Merely
  **adding** a key with a default → do nothing. **Renaming / changing meaning / deleting a key /
  changing a type** → bump `CONFIG_VERSION` in `version.py` and add a migration function from
  the old version to the new one in `config.py`'s `MIGRATIONS` (step by step, no guessing, no
  skipping levels).
- **Unknown keys are preserved verbatim** (forward compatibility): keys this version does not
  understand are written back on save, not dropped just because "I don't understand them".
- **Boolean keys accept only `true`/`false`** (and the numbers `0`/`1`): `bool("false")` is
  `True` in Python, so a string would **invert** the switch. Any other value falls back to the
  default plus a WARNING.
- **Enum keys** (e.g. `launcher_update` = `auto`/`check`/`off`, `language` = `auto`/`zh_CN`/`en_US`)
  likewise fall back to the default plus a WARNING, and the WARNING **names the key** — the user
  needs that to find it in `config.json`.

Two hatches for extending without touching the app itself:

| Goal | Where | Notes |
|---|---|---|
| Add a **game version source** | the `version_sources` array in `config.json` | Bundled: `Anuken/Mindustry`, `TinyLake/MindustryX`. Same name overrides a built-in — no code change, no repackaging. Field meanings in `launcher/sources.py` |
| Add **your own feature** | `extensions/*.py` under the deploy directory | Four hooks: `on_config_loaded` / `on_versions_refreshed` / `on_before_launch` / `on_game_exited`. Hook names are **append-only** after release; see `launcher/extensions.py` |

⚠️ Extension code runs with the **same privileges** as the launcher, and is loaded in
`gui_core._start_post_window_tasks` (not in `_start_background_gc`). Set `MDT_NO_EXTENSIONS=1`
to disable extensions entirely.

## UI language internals

UI strings live in `lang/*.json`; the code only contains keys
(`t("settings.save_return")`). As of 2026-10-05 **every module (21 source files) has been
migrated — 355 keys** — and the gate is a hard line: hardcoding one more UI string fails
`i18n_check.py` on the spot.

- **`zh_CN.json` is the reference pack**: add new strings there first, then fill in other
  languages. The key sets and `{placeholders}` of the two files must match **exactly** — a
  missing placeholder only shows up at runtime, so the gate watches it statically.
- **Switching language rebuilds the two pages** rather than relabelling widget by widget:
  strings are resolved when the widget is built, not referenced later. Chasing every widget
  with `configure(text=...)` is guaranteed to miss something, and a miss triggers no alarm.
  Rebuilding (tens of milliseconds) is the only approach that cannot miss; the only moment it
  happens is the settings page's single exit, "Save and return".
- **A missing string never crashes**: the fallback chain is
  `current language → zh_CN → show the key itself`, plus a WARNING. Seeing something like
  `settings.save_return` in the UI means someone missed a key (far easier to locate than a
  blank).
- **Language packs can be overridden externally**: lookup goes through `resource_path()`, and
  a `lang/` folder **next to the exe takes priority over the one inside the package**
  (`_internal/lang/`). So to fix a translation or add a language, drop a JSON of the same name
  next to the exe — **no repackaging needed** (same mechanism as `jre/`).
- **`MDT_LANG` pins the language** (e.g. `MDT_LANG=en_US`). This is a decoupling switch for
  verification scripts — otherwise "the user switched the UI to English" turns a pile of
  assertions that compare Chinese strings red, and that **is not a regression**. Once pinned,
  the value in `config.json` no longer applies. (Scripts that verify the *switching itself*
  must conversely **clear** it, or they cannot exercise a user changing the language.
  `code_regression` / `gui_smoke` / `exe_edge_check` / `e2e_launcher` pin it;
  only `i18n_switch_smoke` clears it.)
- **What stays untranslated**: logs are always Chinese (`logger.*`, and content passed to
  `_add` is left as-is — logs must be greppable and pasteable into an issue under any
  language). Same for the language names themselves (`简体中文` / `English`), internal
  identifiers (`APP_NAME`), **names written into `config.json` as data** (the default profile
  name), and `what=` diagnostic labels (users need them to locate a key in config.json). These
  are marked in the code with **`# i18n: keep`** (at end of line, or in an immediately
  preceding pure-comment block), with the reason written next to it. The single test is:
  **could a user ever read this in the UI?**

To add a language: add one line `(code, native_name)` to `LANGUAGES` in `launcher/utils.py`,
then supply a `lang/<code>.json`.

## Self-update internals

The launcher checks for its own new version and swaps the program files on exit. A few
decisions that go against the grain:

- **Only program files are replaced**: the exe and `_internal/`. `jre/`, `config.json`,
  `versions/`, `Backups/`, `logs/`, and `extensions/` are never touched — an extension of
  "upward compatibility": upgrading the launcher must not disturb any user data.
- **Windows will not let you overwrite a running exe**, so the swap happens **after the launcher
  exits**: before exiting, the exe is copied to `%TEMP%` and relaunched with
  `--apply-update <plan file>`; once the old process is truly gone, it replaces the files from
  outside. (Deliberately not a `.cmd` batch file: non-ASCII paths in batch files are far too
  easy to get wrong. An onedir exe cannot start without its sibling `_internal/`, so the copy in
  `%TEMP%` is linked back to the program directory with `mklink /J`.)
- **`_internal/` is replaced before the exe**: if it dies halfway, the worst case is
  "old exe + new runtime", which is easier to recover from than "new exe + old runtime". Any
  failed step rolls back to the original and records a line in `launcher.log`.
- **Strictly greater, or nothing happens**: a remote version equal to the local one → do
  nothing; lower → never downgrade (locally built versions on a dev machine are often newer
  than a Release — this is the last gate).
- **The update package arrives over the network**, so it only accepts the `_internal/` prefix
  and the exe itself, and compares sha256 per file; an incomplete package, a format mismatch,
  or a path escape causes the **whole thing to be discarded** — half an update plan is far more
  dangerous than none.
- **Three modes** (`launcher_update`): `auto` = download in the background and swap on exit;
  `check` = only notify; `off` = disabled. Running from source, and `MDT_NO_SELFUPDATE=1`,
  always disable it.
- **The update endpoint can be overridden**: the `MDT_SELFUPDATE_API` environment variable
  (`http(s)://` only). Two uses: ① point it at a local fake endpoint during end-to-end
  verification to exercise the entire "check → download → swap files → restart" chain for
  real; ② point it at your own proxy when `api.github.com` is unreachable. The download URL
  comes from the endpoint's response, so it is redirected too.
- The update package is computed against the **previous release's `manifest.json`**. If no
  baseline is available the build **degrades to a full package — it never guesses**.

## Design notes

- **Data root vs. resource root**: `BASE_DIR` (the directory holding the exe) and
  `RESOURCE_DIR` (`sys._MEIPASS`) are separate; resources always go through `resource_path()`
  — first an external copy next to the exe, then a fallback inside the package. That is why
  swapping `jre/` needs no repackaging.
- **All writes go through `atomic_write_text()`** (on Windows, a WinError 5 from `os.replace`
  usually means the target is locked; handled with a module-level lock plus retries and a
  last-resort overwrite).
- **Deleting data goes to the recycle bin by default**: the single entry point is
  `delete_path()` → `SHFileOperationW` + `FOF_ALLOWUNDO`, never `shutil.rmtree`.
- **The version number exists in exactly one place** (`launcher/version.py`): logs, the window
  title, and the `User-Agent` all reference it, so "the log says 1.0.1 while the UI says 1.0.0"
  cannot happen.
- **Launch pre-warming**: as soon as the window is usable, the game files for the current
  version are assembled in the background, so clicking Launch reuses them.
- **Write the whole config in one place**: `set*()` calls only update memory; a single `save()`
  at the end persists. Writing on every setter is how you get half-written config files.
