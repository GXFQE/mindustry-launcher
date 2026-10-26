English · [简体中文](README.zh_CN.md)

# Mindustry Launcher — a multi-version manager for Mindustry on Windows

The official desktop build of Mindustry ships as a plain jar: to keep several versions you build your
own folders, remember which jar is which, and type `java -jar` every time. Worse, **every version
shares one data set** — saves / maps / blueprints / mods / settings under `%APPDATA%\Mindustry`. Play
in any version and the progress lands in the same place; two save sets mean moving directories by hand.

This launcher separates the two: versions sit side by side and launch with one click; each **save
profile** is a whole data directory of its own; backups run on your terms after you quit. tkinter GUI,
**Java bundled** — no Python, no separate Java install.

## What it solves

| Managing jars and the official data directory yourself | With this launcher |
|---|---|
| Several versions means folders you build, jars you have to tell apart, and `java -jar` by hand — with quotes when the path has spaces | One row per version; select it and press **Launch**. Files are assembled in the background while the window is open, so the click does not wait |
| Every version shares `%APPDATA%\Mindustry`, so switching versions pushes the old progress aside | **Save profiles**: one profile is one full data directory (saves / maps / blueprints / mods / settings). Switch before launching and they never touch each other |
| A second version costs a full extra copy (about 110 MB each) | Version files are split by content and **identical pieces are stored once**; a new version only adds what actually differs |
| Backing up means copying the whole data directory, and a week later you cannot tell the copies apart | **Automatic backups** after you quit (default: 20 minutes played, keep 20), with notes and one-click restore; backups deduplicate too, so keeping more does not multiply the space |
| Updating means browsing GitHub releases and picking a file, and a flaky connection kills the download | **Check for updates** downloads from a **source table** (Mindustry / MindustryX built in, you can add your own), with mirrors, a speed test and **resumable downloads** |
| You must install a matching Java first, or the game will not start | **JRE bundled**; a JDK of your own works too — configured path → `jre\` next to the exe → system `JAVA_HOME` / `PATH`, in that order |
| All you get is the game's own log, written under `%APPDATA%` — gone once you switch profiles | Game output is captured and kept per profile; the log window separates **launcher log** and **game output** |
| Launching from a script, a batch file or a shortcut still means opening the launcher first | `--launch "version"` goes straight in with **no window at all**; `--export` packs a version into a portable jar |
| A deleted save or an old log is gone for good | Deletions go to the **Recycle Bin** (settings can make them permanent) |
| A new launcher build means downloading and reinstalling by hand | Downloaded in the background and swapped in when you close it; **only program files are replaced** — never versions, backups or settings |

**How it works**: each version is split into many small file pieces (identical pieces stored once) and
assembled into a runtime jar before launch; save isolation works by pointing the game's **data
directory** at that profile's folder. The game itself, the save format and the mod ecosystem are
untouched — the launcher only assembles files, starts the process and manages backups.

## Getting started

1. **Download** — grab a package from [Releases](../../releases/latest), extract it somewhere you have
   **write permission** (not `C:\Program Files`, not the Desktop), and double-click
   `Mindustry启动器.exe`. `使用说明.txt` inside the package is a step-by-step version of the same
   story, bilingual: Chinese in the first half, English in the second.

2. **Get one version in** — there is no version on first run; pick one of the two:
   - press **Check for updates** to download one (it goes through GitHub; set a mirror in Settings if
     that is slow);
   - press **Manage versions → Import version** and pick a `desktop.jar` someone gave you, then fill
     in the version type and number.

3. **Play** — select it in the list and press **Launch**. Want a second, independent save set?
   **Manage profiles** on the main window → create one → switch to it → launch again, and the game
   reads the other data directory.

The interface **follows your system language**: 简体中文 on a Chinese system, English otherwise.
Settings can override that, and you can drop a `lang\zh_CN.json` of your own to change strings (see
"Things you can change without rebuilding").

## What it can do

### Versions

- One row per version (`Mindustry v146`, `BE v146` …); select one and press **Launch**
- **Manage versions**: import a local jar (pick `desktop.jar`), rename the version number, delete
- **Identical files are stored once**: a second version normally adds only what actually differs
- **Launch pre-warming**: the selected version is assembled into a runtime jar in the background as
  soon as the window opens, so pressing Launch does not wait
- While the game runs, changing versions / switching profiles / opening settings is refused — nothing
  may touch files the game is reading

### Save profiles

- One profile = one full data directory: saves / maps / blueprints / mods / settings
- Switch from the dropdown at the top of the main window; **Manage profiles** creates, renames,
  deletes and opens directories
- Per-profile settings: data directory, automatic backup on/off, minimum play time (1–60 minutes),
  maximum backup count (1–50)
- ⚠️ Changing the data directory only changes **which directory is used from now on** — existing saves
  are not moved for you
- ⚠️ Switching profiles only affects the **next launch**; it is refused while the game runs

### Backups and restore

- **Back up now** for a manual copy; after you quit, a backup runs under the current profile's policy
  (default: 20 minutes played, keep 20)
- Backups are isolated per profile, and each one can carry a note so you know which session it was
- Restoring asks first (it overwrites that profile's current saves) and is refused while the game runs
- Backups deduplicate by content, so keeping more does not multiply the space; past the limit the
  oldest go first
- Over-limit automatic cleanup and the file pieces no longer referenced by any version delete for
  real — "delete to the Recycle Bin" only protects user files such as the save directory and old logs

### Update checks and mirrors

- "Where versions come from" is **one table**: Mindustry and MindustryX are built in; the
  `version_sources` array in `config.json` adds more, and reusing a built-in `type` **overrides** it
  (for example, your own mirror repository)
- Only the matching asset is picked (`asset_pattern`, a regex); stable and pre-release can be filtered
- An empty mirror means a direct GitHub connection; the dropdown lists a few common mirrors and you can
  type your own; a source that stalls hands over to the next one automatically
- **Speed test**: really downloads a small range from every candidate (direct connection included) and
  ranks them (1–3 rounds); **Use the fastest** fills the setting back in
- **Resumable downloads**: a download that dies (flaky network, launcher closed, mirror switched)
  continues from what is already on disk instead of starting over
- Finished files are checked for size / checksum before entering the version store

### Command line

Everything the GUI does has a console counterpart — handy in scripts, or when you would rather not
open a window at all:

| Switch | What it does |
|---|---|
| (no argument) | Open the launcher window |
| `--launch <version>` | Launch that version with **no launcher window at all**; the launcher exits by itself when the game does |
| `--list` | List the versions on this machine (starts nothing, does not even load the UI modules) |
| `--list-backups` | List save backups per profile (timestamp + note) |
| `--get [key]` | Show settings; without a key, list them all — the output can be pasted straight into `--set` |
| `--set key=value …` | Change settings (writes `config.json` only, effective on the next start; several at once) |
| `--export <version>` | Pack a version into a jar you can run with `java -jar`; accepts `--out` |
| `--export-backup <profile> [timestamp]` | Pack one backup into a zip (omitting the timestamp means the newest); accepts `--out` |
| `--help` | Usage, exit codes and a few caveats |

```bat
"Mindustry启动器.exe" --launch "Mindustry v146"
"Mindustry启动器.exe" --export "Mindustry v146" --out D:\share\
"Mindustry启动器.exe" --set language=en_US auto_update=false
```

- **One task per run**, priority: `--help` → `--list` → `--list-backups` → `--get` → `--set` →
  `--export` → `--export-backup` → `--launch` (read-only first; export before launch)
- **No window**: nothing appears on screen; progress and results go to the console you typed in. A
  mistyped name or a half-written argument **starts nothing** and says why
- ⚠️ cmd does **not wait** for a windowed program — the prompt returns immediately, even before the
  output. Treat the **last line**, `启动器已退出（退出码 N）`, as the "finished" signal. To get the
  "prompt after it finishes" feel, put the command in a `.bat` or prefix it with `start /wait`
- Exit codes: `0` OK (a finished game counts) · `1` the launcher crashed · `2` bad arguments ·
  `3` no such version or backup · `4` version found but the game did not start · `5` export failed ·
  `6` could not write the config (launcher is running / disk refused)
- Exports never overwrite: an existing name becomes `..._2.jar`. The exported jar is compressed
  (about 120 MB → 87 MB) and keeps the original `Main-Class`, so it runs as-is

### Java and launch arguments

- Double-click and go: `jre\` ships with the package
- **Java path** takes the **root** of a JRE or JDK (the folder containing `bin\java.exe`); write just
  `jre` for the bundled one. The **Detect** button looks for one
- Fallback order: the configured path → the bundled `jre\` → system `JAVA_HOME` / `PATH` → an error.
  A system Java found that way is used **for that launch only** and is never written into your settings
- **Extra JVM arguments** go before `-cp` (e.g. `-Xmx4G`); **extra game arguments** go after the main
  class and are rarely needed
- Quote any argument containing a space: `-Dfoo="bar baz"`

### Logs

- Game output is shown live and stays readable afterwards; `logs\` keeps one file per launch (can be
  turned off in settings)
- Two sections: **game output** and **launcher log**; copy them or open the folder
- Retention is 1–200 files, oldest dropped first
- The launcher's own log is `launcher.log` — the first thing to read when something goes wrong

### Where the data lives

Everything sits next to the exe; copy the folder and it all comes along:

```
versions\       game versions (identical files stored once)
Backups\        save backups (per profile, deduplicated)
logs\           game output, one file per launch
runtime\        the game process's working directory (the game writes its cache here; leave it alone)
extensions\     your own extensions (optional; the launcher runs fine without it)
lang\           your own interface strings (optional)
config.json     your settings
launcher.log    the launcher's own log
```

### Settings worth knowing

- **Language** — `Follow system` (default), `简体中文`, `English`; the whole interface switches at once
- **Launcher updates** — `Automatic` (default) downloads quietly and swaps in when you close it;
  `Notify only` just tells you; `Disabled` turns it off
- **GitHub mirror** — empty means a direct connection. **Speed test** really downloads a small range
  from each candidate and ranks them; **Use the fastest** fills it back in. The dropdown order is not
  your speed — measure once
- **Test rounds** — 1–3 (default 3): more rounds rank more reliably and take longer. Out-of-range
  values are reported, never clamped
- **Use GitHub CLI authentication** — off by default. If `gh` is installed and logged in, this raises
  the API quota from 60 to 5000 requests per hour. The token stays in memory, is never written to disk,
  and is not sent to mirrors or custom sources
- **Delete files permanently** — off by default, so deletions go to the Recycle Bin; once on, they are
  gone for good
- **Hide the window on launch / close the launcher when the game exits** — turn both on for a
  "click once, then only the game" feel

⚠️ Settings apply only after **Save and go back**.

### Things you can change without rebuilding

Add files next to the exe or edit `config.json`, then restart — no rebuild, no programming:

- **Interface text / another language** — create a `lang\` folder and drop in a JSON with the same
  name as the built-in one (`zh_CN.json` / `en_US.json`); **the external file wins**. The worst case is
  a few labels showing their key (such as `settings.save_return`); delete the file and it is back to
  normal
- **More version sources** — the `version_sources` array; only `type` and `api_url` are required, and
  `asset_pattern` / `prerelease` / `use_mirror` / `sort_rank` are optional. A broken entry is dropped
  with a log line instead of invalidating the rest. Field meanings: `launcher/sources.py`
- **The mirror dropdown** — the `github_mirror_presets` array; whatever you put there shows up in
  Settings
- **Add a feature** — create `extensions\` under the data root and drop in `.py` files (each needs
  `register(api)`); they load at startup. Four hooks: `on_config_loaded` / `on_versions_refreshed` /
  `on_before_launch` / `on_game_exited`

  ```python
  # extensions\my_stats.py
  def register(api):
      api.on("on_game_exited", lambda **kw: print(kw["version_name"], kw["playtime_minutes"]))
  ```

  ⚠️ Extensions run with the **same privileges** as the launcher — only use ones you wrote or trust.
  If you suspect an extension, set `MDT_NO_EXTENSIONS=1` to disable them all

⚠️ Close the launcher before editing `config.json` by hand — it rewrites the whole file on exit. A bad
edit is not fatal: unknown keys are preserved and bad values fall back to their defaults with the key
named in `launcher.log`.

## Not there yet

- **macOS / Linux** — Windows only for now (an exe plus a bundled Windows JRE)
- **The command line stops at launch / list / export / settings** — there is no `--version`, and
  `--launch` uses the **current** save profile rather than one you name
- **No dedicated mod / map / blueprint pages** — isolation works on the whole data directory, so those
  follow the profile, but listing, importing and dependency checks are left to the game
- **No extension distribution** — `extensions\` holds local `.py` files; write your own or have someone
  send you one

## Deliberately out of scope

- **Bundling or redistributing the game** — the copyright belongs to its authors; versions always come
  from the official release pages (or a source you configure)
- **Modifying the game** — no injection, no patches, no changes to the game's own files; official
  files are only assembled as they are into a runnable jar and started
- **Multithreaded downloads** — a single stream is enough, and the effort is better spent on resumable
  downloads and switching sources

## Notes

- **Antivirus false positives happen** (common with PyInstaller builds) — allow the exe
- Upgrading the launcher **only replaces program files** (the exe and `_internal\`); versions, backups,
  settings, extensions and `jre\` are untouched
- If **Check for updates** keeps failing, it is usually GitHub being unreachable: set a mirror, or set
  the `MDT_SELFUPDATE_API` environment variable to a proxy that works for you
- When reporting a problem, include `launcher.log` (plus the game output from `logs\` if relevant) —
  every step is recorded there

---

To **build it or change the code**, see **[DEVELOPING.md](DEVELOPING.md)** (running from source, project
layout, verification and release process, and the reasoning behind a few design decisions).

> This is the English translation. The original is the Chinese
> [README.zh_CN.md](README.zh_CN.md) — if the two ever disagree, the Chinese one wins.

## Licence

Released under the **GNU General Public License v3.0**; the full text is in [LICENSE](LICENSE).
Copyright (C) 2026 GXFQE.
