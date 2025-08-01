#!/usr/bin/env python3
"""embycache_mover.py — safe file mover for EmbyCache lists

• Liest `datafiles/embycache_array2cache.txt` und `datafiles/embycache_cache2array.txt`.
• Checks before each operation whether the file is currently playing in Emby.
• Parallel jobs according to settings (`max_concurrent_moves_*`).
• Copies via rsync -> deletes source when `--run` is passed.
• Default = dry-run. With `--run`, operations are actually executed.
• `--debug` shows additional space checks and rsync progress.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests  # Emby API

# ───────────────────── Project paths ─────────────────────────────
ROOT_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT_DIR / "datafiles"
CONFIG_DIR = ROOT_DIR / "config"
LOG_DIR    = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
A2C_TXT     = DATA_DIR / "embycache_array2cache.txt"
C2A_TXT     = DATA_DIR / "embycache_cache2array.txt"
SETTINGS_FN = CONFIG_DIR / "embycache_settings.json"

# ───────────────────── Argumente ─────────────────────────────────
parser = argparse.ArgumentParser(description="EmbyCache Mover")
parser.add_argument("--run",   action="store_true", help="Actually move files (default is dry-run)")
parser.add_argument("--debug", action="store_true", help="More log output including space checks")
ARGS = parser.parse_args()

# ───────────────────── Logging ────────────────────────────────────
log_file = LOG_DIR / f"mover_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ],
)
log = logging.getLogger("mover")

# ───────────────────── Settings laden ─────────────────────────────
if not SETTINGS_FN.exists():
    log.critical("Settings missing — run embycache_setup.py first!")
    sys.exit(1)
cfg: Dict[str, Any] = json.loads(SETTINGS_FN.read_text("utf-8"))
EMBY_URL     = cfg["EMBY_URL"].rstrip("/")
EMBY_API_KEY = cfg["EMBY_API_KEY"]
MAX_CACHE    = cfg.get("max_concurrent_moves_cache", 5)
MAX_ARRAY    = cfg.get("max_concurrent_moves_array", 2)
CACHE_PREFIX = "/mnt/cache/"
ARRAY_PREFIX = "/mnt/user0/"
USER_PREFIX  = "/mnt/user/"

# ───────────────────── Helfer ─────────────────────────────────────

def human(bytes_: int) -> str:
    unit = "B"
    for u in ["KiB","MiB","GiB","TiB"]:
        if bytes_ < 1024:
            break
        bytes_ /= 1024
        unit = u
    return f"{bytes_:0.1f} {unit}"


def free_space(path: str) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        try:
            return shutil.disk_usage("/").free
        except OSError:
            return 0

# ───────────────────── Emby-Check ─────────────────────────────────

def is_playing(p: str) -> bool:
    try:
        r = requests.get(f"{EMBY_URL}/Sessions", params={"api_key": EMBY_API_KEY}, timeout=8)
        r.raise_for_status()
        for s in r.json():
            if s.get("NowPlayingItem", {}).get("MediaSources", [{}])[0].get("Path") == p:
                return True
    except requests.RequestException as e:
        log.debug("Session-Check fehlgeschlagen: %s", e)
    return False

# ───────────────────── Load TXT lists ─────────────────────────────

def read_list(fn: Path) -> List[str]:
    if not fn.exists():
        return []
    return [l.strip() for l in fn.read_text("utf-8").splitlines() if l.strip()]

a2c_raw = read_list(A2C_TXT)
c2a_raw = read_list(C2A_TXT)

# ───────────────────── Path mapper ─────────────────────────────────

def map_array_to_cache(p: str) -> Tuple[str, str]:
    src = p
    if p.startswith(ARRAY_PREFIX) or p.startswith(USER_PREFIX):
        dst = p.replace(ARRAY_PREFIX, CACHE_PREFIX, 1).replace(USER_PREFIX, CACHE_PREFIX, 1)
    else:
        dst = p
    return src, str(Path(dst).parent)


def map_cache_to_array(p: str) -> Tuple[str, str]:
    """p is the array destination path (/mnt/user0/...);
    Source is corresponding cache path (/mnt/cache/...)."""
    # Destination path in array remains p -> dst_dir
    dst_dir = str(Path(p).parent)
    # Source in cache by replacing the prefix
    if p.startswith(ARRAY_PREFIX):
        src = p.replace(ARRAY_PREFIX, CACHE_PREFIX, 1)
    elif p.startswith(USER_PREFIX):
        src = p.replace(USER_PREFIX, CACHE_PREFIX, 1)
    else:
        # Fallback
        src = p
    return src, dst_dir

# ───────────────────── Move-Routine ─────────────────────────────────

def safe_move(src: str, dst_dir: str) -> int:
    try:
        size = os.path.getsize(src)
        free = free_space(dst_dir)
        if free < size:
            log.error("Zu wenig Platz: free %s < required %s – %s", human(free), human(size), dst_dir)
            return 0
        os.makedirs(dst_dir, exist_ok=True)
        # Select rsync options
        if ARGS.debug:
            rsync_cmd = ["rsync", "-a", "--progress", src, str(dst_dir) + "/"]
        else:
            rsync_cmd = ["rsync", "-a", "-q", src, str(dst_dir) + "/"]
        if ARGS.run:
            if not shutil.which("rsync"):
                log.error("rsync nicht gefunden – Abbruch.")
                return 0
            ret = subprocess.call(rsync_cmd)
            if ret != 0:
                log.error("rsync error %d for %s", ret, src)
                return 0
            try:
                os.remove(src)
            except Exception as e:
                log.error("Could not delete source: %s – %s", src, e)
                return 0
        log.info("SYNC %s -> %s", src, dst_dir)
        return size
    except Exception as e:
        log.error("Error bei Sync %s -> %s : %s", src, dst_dir, e)
        return 0

# ───────────────────── Executor ─────────────────────────────────────

def run_jobs(jobs: List[Tuple[str, str]], max_workers: int) -> Tuple[int, int]:
    bytes_total = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(safe_move, s, d): (s, d) for s, d in jobs}
        for f in as_completed(futs):
            bytes_total += f.result() or 0
    return len(jobs), bytes_total

# ───────────────────── Ablauf ───────────────────────────────────────
array2cache_jobs: List[Tuple[str, str]] = []
for p in a2c_raw:
    src, dst = map_array_to_cache(p)
    if not Path(src).is_file():
        continue
    if is_playing(src):
        log.info("Skipping (playing): %s", src)
        continue
    array2cache_jobs.append((src, dst))
    if ARGS.debug:
        size = os.path.getsize(src)
        log.debug("PLAN A2C %s -> %s | free %s | required %s", src, dst, human(free_space(dst)), human(size))

cache2array_jobs: List[Tuple[str, str]] = []
for p in c2a_raw:
    src, dst = map_cache_to_array(p)
    if not Path(src).is_file():
        continue
    if is_playing(src):
        log.info("Skipping (playing): %s", src)
        continue
    cache2array_jobs.append((src, dst))
    if ARGS.debug:
        size = os.path.getsize(src)
        log.debug("PLAN C2A %s -> %s | free %s | required %s", src, dst, human(free_space(dst)), human(size))

log.info("Jobs Array->Cache: %d | Cache->Array: %d", len(array2cache_jobs), len(cache2array_jobs))
if not ARGS.run:
    log.info("Dry-run finished — use --run to perform real moves.")
    sys.exit(0)

# Delete old DB
if ARGS.run:
    db_file = DATA_DIR / "emby_fileindex.db"
    if db_file.exists():
        try:
            db_file.unlink()
            log.info("Old DB removed: %s", db_file)
        except Exception as e:
            log.error("Could not delete DB: %s – %s", db_file, e)

# Execute moves
cnt1, sz1 = run_jobs(array2cache_jobs, MAX_CACHE)
cnt2, sz2 = run_jobs(cache2array_jobs, MAX_ARRAY)

# Update TXT files (remove processed paths)
if ARGS.run:
    def update_txt(fn: Path, paths: List[str]):
        """Removes paths from the corresponding TXT file after a successful run."""
        lines = read_list(fn)
        path_set = set(p.strip() for p in paths)
        removed = [l for l in lines if l.strip() in path_set]
        remaining = [l for l in lines if l.strip() not in path_set]
        try:
            fn.write_text("".join(remaining) + ("" if remaining else ""), encoding="utf-8")
            log.info("TXT %s updated: removed %d entries", fn.name, len(removed))
        except Exception as e:
            log.error("Error beim Aktualisieren %s: %s", fn.name, e)
    # Update for A2C and C2A
    update_txt(A2C_TXT, [s for s, _ in array2cache_jobs])
    update_txt(C2A_TXT, [s for s, _ in cache2array_jobs])

# Zusammenfassung
log.info("===== Zusammenfassung =====")
log.info("Array -> Cache : %d files | %s", cnt1, human(sz1))
log.info("Cache -> Array : %d files | %s", cnt2, human(sz2))
log.info("Total bewegt  : %s", human(sz1 + sz2))
