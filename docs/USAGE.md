English · [简体中文](USAGE.zh_CN.md)

# Detailed usage — Mindustry Launcher

For people who already have it running and want the details. **What it is and how to start** is in
[README.md](../README.md).

## Command line

Most of what the GUI does has a console counterpart — handy in scripts and shortcuts, or when you would
rather not open a window at all. (Renaming a version and adding a note to a backup are GUI-only;
deleting a save profile is deliberately GUI-only, because it takes three confirmations.)

| Switch | What it does |
|---|---|
| (no argument) | Open the launcher window |
| `--launch <version>` | Launch that version with **no launcher window at all**; the launcher exits by itself when the game does |
| `--list` | List the versions on this machine (starts nothing, does not even load the UI modules) |
| `--list-backups` | List save backups per profile (timestamp + note) |
| `--check-updates` | Check for new game versions (check only, no download; exit code `10` when there is one) |
| `--get [key]` | Show settings; without a key, list them all (one per line: `key = value # description`, defaults marked) |
| `--set key=value …` | Change settings (writes `config.json` only, effective on the next start; several at once) |
| `--export <version>` | Pack a version into a jar you can run with `java -jar`; accepts `--out` |
| `--export-backup <profile> [timestamp]` | Pack one backup into a zip (omitting the timestamp means the newest); accepts `--out` |
| `--import <jar> --as <version>` | Add a jar to the version library (`--type` changes the type; `Mindustry` by default). `--as` is required |
| `--restore-backup <profile> [timestamp]` | Restore one backup into the save folder (omitting the timestamp means the newest). **Needs `--yes`** |
| `--delete-version <version>` | Delete a version (manifest plus unreferenced objects). **Needs `--yes`** |
| `--delete-backup <profile> [timestamp]` | Delete one backup (omitting the timestamp means the newest). **Needs `--yes`** |
| `--yes` | Confirm a data-changing action (delete / restore). Without it nothing at all happens |
| `--help` | Usage, exit codes and a few caveats |

```bat
:: Straight into the game (only the game on screen, no launcher)
"Mindustry启动器.exe" --launch "Mindustry v146"

:: Pack a version into a portable jar — without --out it goes to the current directory
"Mindustry启动器.exe" --export "Mindustry v146" --out D:\share\

:: Change two settings, effective on the next start
"Mindustry启动器.exe" --set language=en_US auto_update=false
```

### Must-knows

- **One task per run**, priority: `--help` → `--list` → `--list-backups` → `--check-updates` →
  `--get` → `--set` → `--export` → `--export-backup` → `--import` → `--restore-backup` →
  `--delete-version` / `--delete-backup` → `--launch` (read-only first, the online one after the
  local ones; export before launch — export changes no state, while a launch holds the save
  directory; writing in before destroying; delete after export, so `--export-backup X --delete-backup X`
  copies before it removes).
- **Data-changing targets are mutually exclusive**: giving two of `--restore-backup` /
  `--delete-version` / `--delete-backup` is bad usage (exit code `2`) and nothing runs at all — doing
  only the first would silently drop the other half.
- **A version name** is the row shown in the version list (`Mindustry v146`, `BE v146`); the bare
  number (`v146`) works too as long as only one type has it — an ambiguous name tells you to write it
  out. A mistyped name, or `--launch` with nothing after it, **starts nothing** and lists what is
  available.
- **No window**: progress and results go to the console you typed in — which version is starting, when
  it came up (with the time it took), when the game exits (with how long you played), and whether a
  backup was made.
- ⚠️ **cmd does not wait for a windowed program**: the prompt returns immediately, even before the
  output appears. Treat the **last line**, `启动器已退出（退出码 N）`, as the signal. For a "prompt
  after it finishes" feel, put the command in a `.bat` (scripts make cmd wait) or prefix it with
  `start /wait`.
- The same argument works on a shortcut (double-click to play). There is no console to write to in that
  case, so problems fall back to a dialog.
- **Exports never overwrite**: an existing name becomes `..._2.jar`, `..._3.jar`. The exported jar is
  compressed (about 120 MB → 87 MB, a few extra seconds while packing) and keeps the original
  `Main-Class`, so `java -jar` works straight away. An exported zip unpacks to the contents of the save
  directory.
- **Export is read-only**: it does not touch your saves or any setting — writing into the save
  directory is what **Restore** does.

### Exit codes (readable as `%ERRORLEVEL%` in scripts)

| Code | Meaning |
|---|---|
| `0` | Finished normally (a finished game counts) |
| `1` | The launcher itself failed |
| `2` | Bad arguments (`--launch` with no version, `--set` with no `key=value`, a delete without `--yes`, `--import` without `--as`) |
| `3` | No such version / no such backup |
| `4` | Version found, but the game did not start |
| `5` | Export failed (while packing the jar / zip) |
| `6` | Could not write the config (launcher running / disk refused / read-back mismatch) |
| `7` | Delete failed (manifest locked or still there after read-back; the launcher was running) |
| `8` | Import failed (jar unreadable / same name already exists / manifest not written) |
| `9` | Restore failed (backup not writable / fewer files after read-back; the launcher was running) |
| `10` | Update check found a newer version (not an error — "something to say") |
| `11` | Update check failed (no source could be reached) |

### Importing a jar

Got a `desktop.jar` (the one from an official release, one a friend sent, or one you packed yourself)
and want it in the version list? Use `--import`. It is the same path as **Version manager → Import**
in the GUI.

```bat
:: Import as Mindustry 146
"Mindustry启动器.exe" --import "D:\Downloads\Mindustry-146.jar" --as 146

:: Import as MindustryX 160 (--type switches the type)
"Mindustry启动器.exe" --import "D:\Downloads\Desktop.jar" --as 160 --type MindustryX
```

- **`--as` is required** and never guessed from the file name — the version name becomes the manifest
  filename and the row in the list, and a wrong one has to be deleted first. Without it you get exit
  code `2` and not a single object is written.
- **The same name is never overwritten**: importing 146 again while `Mindustry 146` exists is refused
  (exit code `8`). Delete it first (`--delete-version "Mindustry 146" --yes`) or pick another name.
- **Refused while the launcher is running** (exit code `6`): its background GC would treat the freshly
  written objects (manifest not on disk yet) as unreferenced orphans.
- Importing only reads that jar and **never touches your saves**; afterwards it reclaims unreferenced
  objects once, same as the GUI does.

### Deleting a version or a backup

Deleting is the one irreversible operation here, so it is gated twice: an explicit `--yes`, and the
launcher must be closed.

```bat
:: Delete a version (and reclaim objects nothing references any more)
"Mindustry启动器.exe" --delete-version "Mindustry v146" --yes

:: Delete the newest backup of a profile
"Mindustry启动器.exe" --delete-backup 默认 --yes

:: Delete one specific backup (timestamps come from --list-backups)
"Mindustry启动器.exe" --delete-backup 默认 2026-09-20_16-35-32 --yes
```

- **Without `--yes` nothing happens** — exit code `2`, not a single file touched. A console command
  that scripts run cannot afford an interactive "are you sure".
- **Only the manifest is deleted; your saves are not touched.** Data objects live in a shared pool, so
  they are reclaimed only once nothing else references them (another backup, another profile).
- **Refused while the launcher is open** (exit code `6`): its in-memory version/backup lists still
  point at what you are deleting. Close it and run again.
- The reclamation pass walks the whole object pool (tens of thousands of objects, roughly ten seconds
  on a typical install), so a delete takes noticeably longer than a listing.
- A backup is addressed by a **profile and timestamp that exist on disk**: the profile name is looked
  up in what was just scanned, never joined into a path, so `..` cannot escape the backup directory.

### Restoring a backup

**Manage backups → Restore backup** in the GUI is `--restore-backup` on the console. It takes the same
arguments as `--delete-backup` (profile plus an optional timestamp; omitting it means the newest).

```bat
:: Put the newest backup of the profile 默认 back
"Mindustry启动器.exe" --restore-backup 默认 --yes

:: Restore one specific backup (timestamps come from --list-backups)
"Mindustry启动器.exe" --restore-backup 默认 2026-09-20_16-35-32 --yes
```

- **`--yes` is required**: this is the one command that **overwrites your current save**. Without it
  you get exit code `2` and not a single file is touched.
- It goes through the same code as the GUI, so both safety nets are intact: the current save is packed
  into `rollback_*.zip` first (if that snapshot cannot be made, nothing is deleted), the old contents
  go to the Recycle Bin (unless `permanent_delete` is on), and a failure halfway rolls back.
- **It reads back afterwards**: as many files as the manifest lists have to be in the save folder.
  Fewer means exit code `9` instead of a fake success — do not start the game then; check the folder
  first, what is missing may still be in the Recycle Bin.
- **Only that one profile's save folder is touched** — not the version library, not other profiles. It
  is refused while the launcher is running (exit code `6`). The profile name is looked up in the scan
  results, never joined into a path.

### Checking for updates (check only, no download)

`--check-updates` is the **Check for updates** button without the download: pulling a 100+ MB package
from a console with no progress bar leaves you reading logs when it goes wrong. To download, use the
window, or let the launcher check on startup (it downloads by itself when it finds one).

```bat
"Mindustry启动器.exe" --check-updates
if errorlevel 10 echo An update is available
```

- Exit code `10` means **an update was found** — not an error; scripts use it to decide whether to
  notify. Exit code `11` means **no source could be reached** (offline / rate limited). That is not
  the same as "already up to date", and must not be read as such, or an offline machine would silently
  skip updates.
- It only covers game versions, not the launcher's own update (that is a separate mechanism).

### Changing settings with `--set`

- The output of `--get` can be **pasted straight** into `--set`: `hide_on_launch`, `jvm.jre_path`,
  `profile.Default.max_backups`. Use your own profile names.
- A bad value **raises an error** instead of silently storing a default; **one bad item leaves the
  whole batch unchanged**.
- Ranges match the spin boxes in Settings — minimum play time 1–60, maximum backups 1–50, test rounds
  1–3, log retention 1–200; the language accepts only `auto` / `zh_CN` / `en_US`; a mirror must look
  like an address (empty means a direct connection). Out-of-range values **error, never clamp**.
- **It is refused while the launcher is running** (exit code `6`): on exit the launcher writes its
  in-memory config back over the file and would wipe the change — close it first. **Save and go back**
  in the GUI has no such problem.
- Quote any argument containing a space, or cmd splits it into two:

  ```bat
  "Mindustry启动器.exe" --set "extra_vm_args=-Xmx4G -XX:+UseZGC"
  ```

- **Not everything is open**: `version_sources` and `github_mirror_presets` are arrays,
  `profile.<name>.data_dir` would **not move any files**, and `jvm.main_class` / `jvm.vm_args` can
  stop the game from starting — edit `config.json` or use the GUI for those (VM arguments go through
  `extra_vm_args`).

## Java and launch arguments

- Double-click and go: `jre\` ships with the package.
- **Java path** takes the **root** of a JRE or JDK (the folder containing `bin\java.exe`); write just
  `jre` for the bundled one. The **Detect** button looks for one.
- Fallback order: the configured path → the bundled `jre\` → system `JAVA_HOME` / `PATH` → an error.
  ⚠️ A system Java found that way is used **for that launch only** and is never written into settings.
- **Extra JVM arguments** go before `-cp` (e.g. `-Xmx4G`); **extra game arguments** go after the main
  class and are rarely needed.
- Quote any item containing a space: `-Dfoo="bar baz"`. Backslashes in paths (`C:\...`) are written
  as-is, no escaping.

## Logs

- Game output is shown live and stays readable afterwards; `logs\` keeps one file per launch (can be
  turned off in settings).
- Two sections: **game output** and **launcher log**; copy them or open the folder.
- Retention is 1–200 files (default 20), oldest dropped first, effective on the next game start.
- The launcher's own log is `launcher.log` — the first thing to read when something goes wrong.
- The game's own `last_log.txt` is hard-wired to `%APPDATA%\Mindustry` and does not follow a profile;
  after the game exits the launcher copies it into the current profile's logs.

## Where the data lives

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

## Settings, item by item

**Per save profile** (the upper half of the settings page):

| Setting | Meaning |
|---|---|
| Profile name | The name such as "默认"; renaming does not move any directory |
| Data directory | The game data directory this profile reads and writes. Changing it only changes **which directory is used from now on** — existing saves are not moved |
| Minimum play time | 1–60 minutes: a session shorter than this is not backed up |
| Maximum backups | 1–50: past the limit the oldest is deleted |
| Automatic backup | Off means only **Back up now** |

**Launcher settings** (shared by every profile):

- **Language** — `Follow system` (default), `简体中文`, `English`; the whole interface switches at once.
- **Launcher updates** — `Automatic` (default) downloads quietly and swaps in on exit; `Notify only`
  just tells you; `Disabled` turns it off. Only program files are replaced — never versions, config,
  backups or saves. Disabled automatically when running from source.
- **GitHub mirror** — prefixed to the download address for speed; empty means a direct connection, and
  a source that stalls hands over to the next one.
- **Speed test / test rounds** — 1–3 (default 3). Each round measures every candidate; more rounds rank
  more reliably and take longer. Values outside 1–3 are reported, never silently used.
- **Use GitHub CLI authentication** — off by default. With `gh` installed and logged in, this raises
  the API quota from 60 to 5000 requests per hour (the 60 is shared per exit IP). The token stays in
  memory, is never written to disk, and is not sent to mirrors or custom sources.
- **Extra JVM / game arguments** — see "Java and launch arguments" above.
- **Log retention** — 1–200.
- **Delete files permanently** — off by default, so deletions go to the Recycle Bin; once on, they are
  gone for good. Only affects user files such as the save directory and old logs.
- **Hide the window on launch / close the launcher when the game exits** — turn both on for a "click
  once, then only the game" feel (closing waits for the backup to finish).

⚠️ Settings apply only after **Save and go back** — that is the page's only exit, and a failed
validation keeps you on it.

## Things you can change without rebuilding

Add files next to the exe or edit `config.json`, then restart — no rebuild, no programming.

### Interface text / another language

Create a `lang\` folder and drop in a JSON with the same name as the built-in one (`zh_CN.json` /
`en_US.json`); **the external file wins**. The worst case is a few labels showing their key (such as
`settings.save_return`); delete the file and it is back to normal.

### More version sources

Where versions come from is one table (the `version_sources` array in `config.json`); two are built in
and you can add more. Reusing a built-in `type` **overrides** it (for example, pointing Mindustry at
your own mirror repository). The built-in pair looks like this:

```json
"version_sources": [
  { "type": "MindustryX",  "api_url": "https://api.github.com/repos/TinyLake/MindustryX/releases",
    "asset_pattern": "Desktop\\.jar$", "prerelease": false, "sort_rank": 0 },
  { "type": "Mindustry",   "api_url": "https://api.github.com/repos/Anuken/Mindustry/releases",
    "asset_pattern": "Mindustry.jar", "prerelease": null, "sort_rank": 1 }
]
```

Only `type` and `api_url` are required; `asset_pattern` (a regex picking the asset), `prerelease`
(`true` / `false` / `null` = both), `use_mirror` (`false` = this source skips the mirror) and
`sort_rank` (lower sorts first) are optional. A broken entry is dropped with a log line rather than
invalidating the config. Field meanings: `launcher/sources.py`.

### The mirror dropdown

The `github_mirror_presets` array in `config.json`; whatever you put there shows up in the mirror
dropdown in Settings.

### Add a feature (extensions)

Create `extensions\` under the data root and drop in `.py` files (each needs `register(api)`); they
load at startup. Four hooks: `on_config_loaded` / `on_versions_refreshed` / `on_before_launch` /
`on_game_exited`.

```python
# extensions\my_stats.py
def register(api):
    api.on("on_game_exited", lambda **kw: print(kw["version_name"], kw["playtime_minutes"]))
```

⚠️ Extensions run with the **same privileges** as the launcher — only use ones you wrote or trust. If
you suspect an extension, set `MDT_NO_EXTENSIONS=1` to disable them all.

## When something goes wrong

- **Antivirus false positives happen** (common with PyInstaller builds) — allow the exe.
- Upgrading the launcher **only replaces program files** (the exe and `_internal\`); game versions,
  backups, settings, extensions and `jre\` are untouched.
- If **Check for updates** keeps failing, it is usually GitHub being unreachable: set a mirror, or set
  the `MDT_SELFUPDATE_API` environment variable to a proxy that works for you.
- Close the launcher before editing `config.json` by hand — it rewrites the whole file on exit. A bad
  edit is not fatal: unknown keys are preserved and bad values fall back to their defaults with the key
  named in `launcher.log`.
- When reporting a problem, include `launcher.log` (plus the game output from `logs\` if relevant) —
  every step is recorded there.
