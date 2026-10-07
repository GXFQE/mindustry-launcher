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
  are, set a mirror in Settings.
- **Launch pre-warming** — the selected version's files are assembled in the background
  while the window is open, so launching is quicker.
- **Log viewer** — captures the game's output so you can read it after it exits.

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
