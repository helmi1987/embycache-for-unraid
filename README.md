# EmbyCache

Helper scripts to **collect** “continue watching / next episodes” from Emby
and to **move** media files between an Unraid **array** and **cache** safely.

> Default is **dry‑run** — nothing is copied or deleted unless you pass `--run`.

## Features
- Collects *NextUp / In‑Progress / Resumable* entries per Emby user and builds mover lists.
- Safe mover that checks whether a file is currently playing in Emby before moving.
- Parallel jobs (configurable), rsync based copying, and clear logs.
- `--debug` for extra details (free space checks, rsync progress).
- Single entry point via `start.py`.

## Requirements
- Python **3.9+**
- Access to your Emby server (URL + API key)
- `rsync` available on the host
- Unraid‑style paths (defaults): `/mnt/user/…` and `/mnt/cache/…`

## Quick start
1. **Unpack / clone** this project.
2. **Run setup** (creates the config under the project root):
   ```bash
   python3 scripts/embycache_setup.py
   ```
   The settings file will be written to **`./config/embycache_settings.json`**.
3. **Dry‑run (no changes)**:
   ```bash
   python3 start.py --debug
   ```
4. **Do the real moves**:
   ```bash
   python3 start.py --run
   ```

## Command reference

### `start.py`
Wrapper that runs *collect* and then *mover* sequentially.
```
usage: start.py [--run] [--debug] [--skip-cache]
--run         Run mover in run mode (otherwise dry‑run)
--debug       Run both scripts in debug mode
--skip-cache  Forwarded to collect
```

### `scripts/embycache_setup.py`
Interactive setup/update for the configuration file. It always reads/writes:
```
./config/embycache_settings.json
```
You can re‑run it any time to update existing settings.

### `scripts/embycache_collect.py`
Read‑only analyzer that:
- queries Emby for on‑deck / continue‑watching items,
- resolves up to `number_episodes` next episodes,
- looks for sidecar files,
- writes **TXT lists** under `./datafiles/` for the mover.

Key outputs (text files):
- `embycache_mover_to_exclude.txt` – items that must never be moved back
- `embycache_array2cache.txt` – files that still need to go *array → cache*
- `embycache_cache2array.txt` – placeholder (empty by default)

Logs go to `./logs/` (latest: `ondeck_latest.log`).

### `scripts/embycache_mover.py`
Moves files according to the two mover lists. **Dry‑run by default**.
- `--run` performs real moves (rsync copy, then delete source).
- `--debug` prints additional details.

The mover **skips** any file that is currently playing in Emby.

## Project layout
```
.
├── config/                # generated: embycache_settings.json
├── datafiles/             # generated: *.txt lists, file index DB
├── logs/                  # generated: analysis & mover logs
├── scripts/
│   ├── embycache_collect.py
│   ├── embycache_mover.py
│   └── embycache_setup.py
└── start.py               # entry point
```

## Notes
- Nothing is moved without `--run`.
- Path assumptions (/mnt/user and /mnt/cache) can be adjusted in settings.
- If you see “Settings file missing — please run embycache_setup.py first!”, run the setup again.

---
*Generated 2025-08-01 20:50:50*
