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

See `使用说明.txt` inside the package for the same information in Chinese.

## What it does

- **Several versions at once** — identical files are stored only once, so a second
  version does not cost you a full extra copy.
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
- **GitHub mirror** — leave blank to talk to GitHub directly.
- **Delete permanently** — off by default, so deletions go to the **recycle bin** and can
  be restored. Turn it on and they are gone for good.

⚠️ Click **Save and return** at the bottom right, or your changes are not applied.

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
