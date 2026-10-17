# Mindustry Launcher

[简体中文](README.zh_CN.md) · **English**

Run several Mindustry versions side by side, keep each one's saves separate, and back
them up automatically. tkinter GUI, **Java bundled** — no Python, no separate Java install.

> This is the English translation. The original is the Chinese
> [README.zh_CN.md](README.zh_CN.md) — if the two ever disagree, the Chinese one wins.

---

## Download and run

Get a package from [Releases](../../releases/latest), extract it somewhere you have
**write permission** (not `C:\Program Files`, not the Desktop), and double-click
`Mindustry启动器.exe`.

There is no game version on first run: click **Check for updates** to download one,
then select it in the list and click **Launch**.

`使用说明.txt` inside the package covers the same ground, and is bilingual:
Chinese in the first half, English in the second.

## What it does

- **Several versions at once** — identical files are stored only once, so a second
  version does not cost you a full extra copy.
- **Version management** — import a local jar someone gave you (pick the `desktop.jar`);
  rename or delete versions you no longer play. All of it lives under
  **"管理版本" (Manage versions)**.
- **Save profiles** — each profile gets a completely separate game data directory.
  Switch profile, and the game reads a different set of saves. Each profile has its
  own data directory, minimum play time, and backup count.
- **Automatic backups** — runs after you quit the game, under the conditions you set
  (by default: at least 20 minutes played, keep the newest 20).
- **Update checking** — downloads game versions for you. If GitHub is slow where you
  are, set a mirror in Settings. A download that dies halfway resumes from where it
  stopped instead of starting over.
- **Launch pre-warming** — the selected version's files are assembled in the background
  while the window is open, so launching is quicker.
- **Launch from the command line** — `--launch "Mindustry v146"` in a console goes
  straight into that version with **no launcher window at all**; `--list` shows your
  versions and `--help` shows the usage (see below).
- **Export from the command line** — `--export` packs a version into a jar you can run
  with `java -jar`, and `--export-backup` packs a save backup into a zip;
  `--list-backups` shows what backups you have.
- **Log viewer** — captures the game's output so you can read it after it exits.

## Launching a version from the command line

Pass an argument from a console (cmd, PowerShell, Windows Terminal) and it goes straight
into that version. **The launcher window never appears** — all you see is the game:

```
"Mindustry启动器.exe" --launch "Mindustry v146"
```

- The version name is the line shown in the main window's list (`Mindustry v146`,
  `BE v146`, ...). You can also pass just the version number (`v146`) — as long as only
  one type has it; if several do, the launcher asks you to spell out the full name
  instead of guessing.
- Can't remember the name? List them first:

  ```
  "Mindustry启动器.exe" --list
  ```

  It prints the versions on this machine, one per line (and launches nothing). Forgot
  how the arguments work? Run `--help`: usage, exit codes and the few gotchas are all
  in there. Neither of them creates a window — they don't even load the UI module.
- **One thing at a time**: if `--help`, `--list` and `--launch` are all given, only the
  first one in that order is done (a "how do I use this" question is always answered
  first; `--list` is read-only and comes before launching).
- Progress and results are written to **the console you typed the command in**: which
  version is starting, that it came up (with the time it took), that the game exited
  (with how long you played), and whether this session was backed up.
- When the game exits, the launcher **exits on its own** — the "close the launcher after
  the game exits" setting does not apply here, because this form has neither a window nor
  a tray icon, so there would be no way to close it. The automatic backup still runs,
  under the rules of the profile you were playing in.
- When it finishes, the **last line** in the console is `Launcher exited (exit code N)` —
  **that line is the "finished" signal** (cmd does **not wait** for windowed programs: the
  prompt comes back the moment you press Enter, *before* the program's output; so no new
  prompt appears when it finishes — trust that line). Prefer the
  "prompt-returns-only-when-done" feel? Put the command in a `.bat` (cmd waits in
  scripts), or prefix it with `start /wait`.
- A wrong name — or a `--launch` with no name after it — launches **nothing**: the
  console explains why (listing your versions when the name was wrong) and no extra
  window appears.
- Exit codes are meaningful, so scripts can branch on them: `0` success (quitting the
  game after playing counts as success), `1` the launcher crashed, `2` bad command line,
  `3` no such version or backup, `4` the version was found but would not start,
  `5` export failed (while packing the jar / zip).
  `--launch=<name>` works too, and the console you pressed Enter in is the one used.

★ A shortcut can carry the argument as well (double-click and play). There is no console
to write to in that case, so problems fall back to a dialog box — run it from a console
and every message stays in that console.

## Exporting a version / a save backup

Both run from a console and **never launch the game or open the launcher window**:

```
:: pack a game version into a jar -- runs with java -jar on any machine
"Mindustry启动器.exe" --export "Mindustry v146"

:: pack a save backup into a zip -- leaving the timestamp out takes the newest one
"Mindustry启动器.exe" --export-backup 默认
"Mindustry启动器.exe" --export-backup 默认 2026-09-20_16-35-32
```

- Not sure which backups you have? List them first:
  ```
  "Mindustry启动器.exe" --list-backups
  ```
  It groups them by profile, one line per "timestamp + note". That timestamp is exactly
  what `--export-backup` wants (leaving it out means the newest one in that profile).
- The file goes into the **current directory** by default, named
  `type_version.jar` / `profile_timestamp.zip`. To put it elsewhere add `--out` (a file
  path, or an **existing directory**):
  ```
  "Mindustry启动器.exe" --export "Mindustry v146" --out D:\share\
  ```
- **Nothing is ever overwritten**: if the name is already taken it becomes
  `..._2.jar`, `..._3.jar`, and so on.
- The exported jar keeps the original `Main-Class`, so `java -jar` just works; the
  exported zip unzips straight into the contents of a save folder (copy it in and play).
- The exported jar **is compressed**: roughly 120 MB → 87 MB for a typical version,
  at the cost of a few extra seconds; `java -jar` and the game are unaffected.
- Exporting is **read-only**: it never touches your current saves or your settings —
  writing to the save folder is what "restore backup" does, not this.

## Where your data lives

Everything sits next to the exe — copy the whole folder and it all comes with you.

```
versions\       downloaded game versions (deduplicated)
Backups\        save backups
logs\           game output logs (one per launch; can be turned off)
extensions\     your own extensions (optional, see below; a missing folder is fine)
lang\           interface text you override yourself (optional, see below)
config.json     your settings
launcher.log    the launcher's own log — look here first when something goes wrong
```

## Settings worth knowing

- **Interface language** — `Follow system` (default), `简体中文`, `English`. The whole
  interface switches immediately, no restart.
- **Launcher update** — `auto` (default) quietly downloads a new launcher and swaps it
  in when you close the launcher; `check` only tells you; `off` disables it.
- **Java path** — point it at a JRE or JDK root, or just drop a `jre\` folder next to the
  exe. A wrong path is safe: it falls back to the bundled Java, and if even that is
  missing it looks in `JAVA_HOME` / `PATH`.
- **GitHub mirror** — leave blank to talk to GitHub directly. The **Speed test**
  button next to it downloads a small sample through every candidate (direct
  included) and sorts them by speed (**a progress bar shows which candidate is
  being tested**, so it never looks stuck); **Use fastest** fills the winner back
  into the setting. Line quality varies by ISP and region, so the order of the
  dropdown means nothing.
- **Test rounds** — 1 to 3 (default 3). Every round tests each candidate once
  (total samples = candidates × rounds), so more rounds = steadier ranking but
  longer waits. Turn it down to 2 or 1 if speed matters more. It takes effect
  the moment you hit "Test" — no need to save first; out-of-range values are
  rejected with a prompt instead of being silently adjusted.
- **Use GitHub CLI authentication** — off by default. If GitHub CLI (`gh`) is
  installed and logged in, tick it and GitHub requests get authenticated: the API
  quota rises from 60/hour (shared per IP) to 5000/hour. The token stays in memory
  and is never written to disk; mirrors and custom sources never see it.
- **Delete permanently** — off by default, so deletions go to the **recycle bin** and can
  be restored. Turn it on and they are gone for good.

⚠️ Click **Save and return** at the bottom right, or your changes are not applied.

## Tweaking it without rebuilding

Everything below is either "drop a file next to the exe" or "hand-edit `config.json`".
Restart the launcher and it takes effect — no repackaging, no programming required.

- **Interface text / another language** — create a `lang\` folder next to the exe and drop
  in a JSON file **named like the built-in one** (`zh_CN.json` / `en_US.json`). **The
  outside copy wins.** This is how you reword things or add a language. Worst case if you
  break it: a few labels show up as setting names (e.g. `settings.save_return`); the
  launcher still opens. Delete your copy and it goes back to normal.
- **More places to download game versions from** — the sources are a table
  (the `version_sources` array in `config.json`). Two are built in; you can add more, and
  a `type` that collides with a built-in **overrides** it (e.g. point the built-in
  Mindustry entry at your own mirror). No code change, no repackaging. Field meanings are
  in `launcher/sources.py`. The two built-ins look like this — copy the shape:

  ```json
  "version_sources": [
    { "type": "MindustryX",  "api_url": "https://api.github.com/repos/TinyLake/MindustryX/releases",
      "asset_pattern": "Desktop\\.jar$", "prerelease": false, "sort_rank": 0 },
    { "type": "Mindustry",   "api_url": "https://api.github.com/repos/Anuken/Mindustry/releases",
      "asset_pattern": "Mindustry.jar", "prerelease": null, "sort_rank": 1 }
  ]
  ```

  Only `type` and `api_url` are required. Optional: `asset_pattern` (regex picking which
  file to take), `prerelease` (`true` / `false` / `null` = both stable and pre-releases),
  `use_mirror` (`false` = this source skips the mirror), `sort_rank` (lower sorts first).
  A broken entry does not invalidate your config — it is dropped and logged.
- **The mirror dropdown itself** — the `github_mirror_presets` array in `config.json`.
  Whatever you put there shows up in the **GitHub mirror** dropdown in Settings.
- **Your own feature** — create an `extensions\` folder in the data root and drop `.py`
  files in it (each needs a `register(api)`); they load at startup. Four hooks:
  `on_config_loaded` / `on_versions_refreshed` / `on_before_launch` / `on_game_exited`.

  ```python
  # extensions\my_stats.py
  def register(api):
      api.on("on_game_exited", lambda **kw: print(kw["version_name"], kw["playtime_minutes"]))
  ```

  ⚠️ Extension code runs with the **same privileges** as the launcher — only use files you
  wrote or trust. If you suspect an extension is causing trouble, set the environment
  variable `MDT_NO_EXTENSIONS=1` to disable all of them.

⚠️ **Close the launcher before hand-editing `config.json`** — it rewrites the whole file on
exit and will overwrite what you typed. Breaking it is not fatal: unknown keys are kept
as-is, bad values fall back to their default, and a line is written to `launcher.log`. Your
config is never thrown away as "corrupt".

## Notes

- **Your antivirus may flag it** — common for PyInstaller-built executables. Add an
  exception.
- Updating the launcher replaces **program files only** (the exe and `_internal\`). It
  never touches your game versions, save backups, settings, extensions, or `jre\`.
- If update checks keep failing, you probably cannot reach GitHub. Set the
  `MDT_SELFUPDATE_API` environment variable to a proxy that works for you.
- When reporting a problem, include `launcher.log` — it records what the launcher did.

## Mindustry itself

This program does not modify or redistribute the game — it only downloads the executable
from the official release page and launches it as a separate process.

## For developers

Building from source, the project layout, the verification workflow and the release
process live in **[DEVELOPING.md](DEVELOPING.md)**.
