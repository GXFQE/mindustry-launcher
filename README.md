English · [简体中文](README.zh_CN.md)

# Mindustry Launcher — a multi-version manager for Mindustry on Windows

> **Looking for the Android build?** That is a separate project → **[MDT Manager (Android)](https://github.com/GXFQE/mindustry-launcher-android)**.

The official desktop build of Mindustry ships as a plain jar: to keep several versions you build your
own folders, remember which jar is which, and type `java -jar` every time. Worse, **every version
shares one data set** — saves / maps / blueprints / mods / settings under `%APPDATA%\Mindustry`.

This launcher separates the two: versions sit side by side and launch with one click; each **save
profile** is a whole data directory of its own; backups run on your terms after you quit. **Java is
bundled** — no Python, no separate Java install.

> This is the English translation. The original is the Chinese
> [README.zh_CN.md](README.zh_CN.md) — if the two ever disagree, the Chinese one wins.

## What it solves

| Managing jars and the official data directory yourself | With this launcher |
|---|---|
| Several versions means folders you build, jars you have to tell apart, and `java -jar` by hand | One row per version; select it and press **Launch**. Files are assembled in the background, so the click does not wait |
| Every version shares `%APPDATA%\Mindustry`, so switching versions pushes the old progress aside | **Save profiles**: one profile is one full data directory, switched before launching — they never touch each other |
| A second version costs a full extra copy (about 110 MB) | Version files are split by content and **identical pieces are stored once** |
| Backing up means copying the whole data directory, and a week later you cannot tell the copies apart | **Automatic backups** after you quit (default: 20 minutes played, keep 20), with notes and one-click restore |
| Updating means browsing GitHub releases, and a flaky connection kills the download | **Check for updates** works off a **source table**, with mirrors, a speed test and **resumable downloads** |
| You must install a matching Java first, or the game will not start | **JRE bundled**; a JDK of your own works too, with layered fallback |
| All you get is the game's own log, written under `%APPDATA%` | Game output is captured and kept per profile, readable in the log window |
| Launching from a script, a batch file or a shortcut means opening the launcher first | `--launch "version"` goes straight in with **no window at all** |

**How it works**: each version is split into small file pieces (identical pieces stored once) and
assembled into a runtime jar before launch; save isolation works by pointing the game's **data
directory** at that profile. The game itself, the save format and the mod ecosystem are untouched.

## Getting started

1. **Download** — grab a package from [Releases](../../releases/latest), extract it somewhere you have
   **write permission** (not `C:\Program Files`, not the Desktop), double-click `Mindustry启动器.exe`.
2. **Get one version in** — press **Check for updates** to download one (set a mirror if GitHub is
   slow), or **Manage versions → Import version** and pick a `desktop.jar` someone gave you.
3. **Play** — select it and press **Launch**. Want a second, independent save set? **Manage
   profiles** → create one → switch to it → launch again.

The interface **follows your system language** (Chinese / English); settings can override that.
`使用说明.txt` inside the package is a step-by-step version of the same story, bilingual.

## What it can do

### Versions

- One row per version (`Mindustry v146` …); select and press **Launch**; **Manage versions** imports,
  renames and deletes
- **Identical files are stored once**, so a second version does not cost a full extra copy
- **Launch pre-warming**: the runtime jar is assembled in the background as soon as the window opens

### Save profiles

- One profile = one full data directory (saves / maps / blueprints / mods / settings), switched from
  the main window
- Per-profile: data directory, automatic backup on/off, minimum play time, maximum backup count
- ⚠️ Changing the data directory only changes which one is used from now on — existing saves are not
  moved; switching is refused while the game runs

### Backups and restore

- Automatic backups after you quit, plus **Back up now**; each backup can carry a note and be restored
  in one click
- Backups deduplicate by content, so keeping more does not multiply the space; past the limit the
  oldest go first
- Deletions go to the **Recycle Bin** (settings can make them permanent)

### Update checks and mirrors

- "Where versions come from" is one table: Mindustry / MindustryX built in, `version_sources` adds or
  overrides
- An empty mirror means a direct connection; **Speed test** measures every candidate and fills in the
  fastest; a broken download resumes where it stopped
- Finished files are checked for size / checksum before entering the version store

### Command line

- Twelve switches: launch, list versions, list backups, read / write settings, export a jar, export a
  backup zip, import a jar, delete a version or a backup — no window, real exit codes, friendly to
  scripts and shortcuts. Deleting always requires `--yes` and a closed launcher
- Full switch table, exit codes and the limits of `--set`: see **[detailed usage](docs/USAGE.md#command-line)**

### Logs

- Game output shown live and kept readable afterwards, one file per launch under `logs\`; the
  launcher's own log is `launcher.log`

## More detail

Short reads, when you need them:

- **[FEATURE.md](FEATURE.md)** — 22 capabilities set against the five other desktop launchers of the
  period (judged from source, not from their READMEs); what is unique here, where the gaps are, and
  what is worth building next
- **[docs/USAGE.md](docs/USAGE.md)** — every command-line switch, each setting, the data layout, the
  Java fallback order, language packs / version sources / extensions, and troubleshooting
- **[DEVELOPING.md](DEVELOPING.md)** — running from source, project layout, verification and release

## Not there yet

- **macOS / Linux** — Windows only for now (an exe plus a bundled Windows JRE)
- **No `--version`**, and `--launch` cannot pick a different save profile for that one run
- **Deleting a save profile is GUI-only on purpose** — it takes three confirmations and moves the data
  directory to the recycle bin. Versions and single backups can be deleted from the console
  (`--delete-version`, `--delete-backup`, both with `--yes`)
- **No dedicated mod / map / blueprint pages** — isolation works on the whole data directory, so those
  follow the profile, but listing, importing and dependency checks are left to the game

## Deliberately out of scope

- **Bundling or redistributing the game** — the copyright belongs to its authors; versions always come
  from the official release pages (or a source you configure)
- **Modifying the game** — no injection, no patches, no changes to the game's own files; official
  files are only assembled as they are into a runnable jar and started
- **Multithreaded downloads** — a single stream is enough, and the effort is better spent on resumable
  downloads and switching sources

---

## Licence

Released under the **GNU General Public License v3.0**; the full text is in [LICENSE](LICENSE).
Copyright (C) 2026 GXFQE.
