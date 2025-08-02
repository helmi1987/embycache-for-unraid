#!/usr/bin/env python3
"""embycache_mover.py – sicherer Datei‑Mover für EmbyCache‑Listen

• Liest `datafiles/embycache_array2cache.txt` und `datafiles/embycache_cache2array.txt`.
• Prüft vor jeder Operation, ob die Datei gerade in Emby läuft (Sessions API).
• Parallele Jobs gemäß Settings (`max_concurrent_moves_*`).
• Kopiert via rsync (Rechte/Eigentümer bleiben erhalten) → löscht Quelle **nur** bei `--run`.
• Standard = Dry‑Run. Mit `--run` werden die Operationen real ausgeführt.
• `--debug` zeigt zusätzlichen Platzcheck und rsync‑Progress.
• Im **Dry‑Run** wird die **Gesamtdatenmenge (geplant)** ausgewiesen.
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

# ───────────────────── Projektpfade ──────────────────────────────────
ROOT_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT_DIR / "datafiles"
CONFIG_DIR = ROOT_DIR / "config"
LOG_DIR    = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

A2C_TXT     = DATA_DIR / "embycache_array2cache.txt"
C2A_TXT     = DATA_DIR / "embycache_cache2array.txt"
SETTINGS_FN = CONFIG_DIR / "embycache_settings.json"

# ───────────────────── CLI ───────────────────────────────────────────
parser = argparse.ArgumentParser(description="EmbyCache Mover")
parser.add_argument("--run",   action="store_true", help="Dateien wirklich verschieben (Standard = Dry‑Run)")
parser.add_argument("--debug", action="store_true", help="Mehr Log‑Ausgabe inkl. Platzcheck und rsync‑Progress")
ARGS = parser.parse_args()

# ───────────────────── Logging ───────────────────────────────────────
log_file = LOG_DIR / f"mover_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mover")

# ───────────────────── Settings laden ────────────────────────────────
if not SETTINGS_FN.exists():
    log.critical("Settings fehlen – zuerst embycache_setup.py ausführen!")
    sys.exit(1)

cfg: Dict[str, Any] = json.loads(SETTINGS_FN.read_text("utf-8"))
EMBY_URL       = cfg["EMBY_URL"].rstrip("/")
EMBY_API_KEY   = cfg["EMBY_API_KEY"]
MAX_CACHE      = cfg.get("max_concurrent_moves_cache", 5)
MAX_ARRAY      = cfg.get("max_concurrent_moves_array", 2)
REAL_SOURCE    = Path(cfg.get("real_source", "/mnt/user"))
CACHE_DIR      = Path(cfg.get("cache_dir", "/mnt/cache"))
EMBY_SOURCE    = Path(cfg.get("emby_source", "/data"))
LIB_FOLDERS    = [str(x).strip("/\\") for x in cfg.get("emby_library_folders", [])]
NAS_FOLDERS    = [str(x).strip("/\\") for x in cfg.get("nas_library_folders", [])]

# Prefixe
CACHE_PREFIX = str(CACHE_DIR.as_posix() + "/")
ARRAY_PREFIX = (REAL_SOURCE.as_posix() + "/").replace("/mnt/user/", "/mnt/user0/", 1) if REAL_SOURCE.as_posix().startswith("/mnt/user/") else "/mnt/user0/"
USER_PREFIX  = "/mnt/user/"

# Mapping emby_library_folders → nas_library_folders
LIB_MAP: Dict[str, str] = {}
for lib, nas in zip(LIB_FOLDERS, NAS_FOLDERS):
    LIB_MAP[lib] = nas

# ───────────────────── Helfer ────────────────────────────────────────

def human(bytes_: int) -> str:
    unit = "B"
    size = float(bytes_)
    for u in ("KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0:
            return f"{size:0.1f} {unit}"
        size /= 1024.0
        unit = u
    return f"{size:0.1f} {unit}"


def free_space(path: str) -> int:
    p = Path(path)
    # Suche existierenden Parent
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        try:
            return shutil.disk_usage("/").free
        except OSError:
            return 0

# Emby‑Sessions prüfen (skip wenn läuft)

def session_paths() -> List[str]:
    paths: List[str] = []
    try:
        r = requests.get(f"{EMBY_URL}/Sessions", params={"api_key": EMBY_API_KEY}, timeout=8)
        r.raise_for_status()
        for s in r.json():
            p = s.get("NowPlayingItem", {}).get("MediaSources", [{}])[0].get("Path")
            if p:
                paths.append(p)
    except requests.RequestException as e:
        log.debug("Session‑Check fehlgeschlagen: %s", e)
    return paths

SESSION_PATHS = session_paths()

# Host↔Container Pfad‑Vergleich (heuristisch über Dateiname + Relativpfad)

def is_same_media(src_host: str, playing_path: str) -> bool:
    """Vergleicht Quelle auf Host mit einem Emby‑NowPlaying‑Pfad.
    Wir gleichen zunächst den Dateinamen ab und prüfen dann, ob der
    relative Pfad ab dem Library‑Ordner übereinstimmt (falls ermittelbar)."""
    try:
        if os.path.basename(src_host) != os.path.basename(playing_path):
            return False
        # Versuche relative Pfade ab NAS‑Folder zu vergleichen
        host = Path(src_host)
        # Ersetze Cache→User, damit Vergleich auf Array‑Baum erfolgt
        host_user = Path(str(host).replace(CACHE_PREFIX, USER_PREFIX, 1).replace("/mnt/user0/", USER_PREFIX, 1))
        # Finde NAS‑Folder im Host‑Pfad
        rel_host = None
        for nas in NAS_FOLDERS:
            idx = str(host_user).find("/" + nas + "/")
            if idx != -1:
                rel_host = str(host_user)[idx + len(nas) + 2:]  # nach "/nas/"
                break
        if rel_host is None:
            return True  # Dateiname identisch, akzeptiere
        # Mappe playing_path ggf. aus /data/<lib>/... nach /mnt/user/<nas>/...
        playing = playing_path
        if str(EMBY_SOURCE) in playing:
            parts = Path(playing).parts
            # Suche Library Segment (direkt nach EMBY_SOURCE)
            if len(parts) >= 3 and parts[0] == "/" and parts[1] == EMBY_SOURCE.as_posix().strip("/"):
                lib = parts[2]
            else:
                # robustere Variante
                try:
                    rel = Path(playing).resolve().relative_to(EMBY_SOURCE)
                    lib = rel.parts[0]
                except Exception:
                    lib = None
            if lib and lib in LIB_MAP:
                mapped = LIB_MAP[lib]
                playing = playing.replace(str(EMBY_SOURCE / lib), str(REAL_SOURCE / mapped), 1)
        # Relativteil ermitteln
        rel_play = None
        for nas in NAS_FOLDERS:
            idx = playing.find("/" + nas + "/")
            if idx != -1:
                rel_play = playing[idx + len(nas) + 2:]
                break
        if rel_play is None:
            return True
        return rel_play == rel_host
    except Exception:
        return False


def is_playing(src_host: str) -> bool:
    for p in SESSION_PATHS:
        if is_same_media(src_host, p):
            return True
    return False

# TXT‑Listen laden

def read_list(fn: Path) -> List[str]:
    if not fn.exists():
        return []
    return [l.strip() for l in fn.read_text("utf-8").splitlines() if l.strip()]

a2c_raw = read_list(A2C_TXT)
c2a_raw = read_list(C2A_TXT)

# Pfad‑Mapper

def map_array_to_cache(p: str) -> Tuple[str, str]:
    """Array→Cache: *Quelle* ist der Array‑Pfad (idR /mnt/user0/...).
    *Zielverzeichnis* ist derselbe Pfad mit Cache‑Prefix (Elternordner)."""
    src = p
    if p.startswith(ARRAY_PREFIX):
        dst = p.replace(ARRAY_PREFIX, CACHE_PREFIX, 1)
    elif p.startswith(USER_PREFIX):
        dst = p.replace(USER_PREFIX, CACHE_PREFIX, 1)
    else:
        dst = p
    return src, str(Path(dst).parent)


def map_cache_to_array(p: str) -> Tuple[str, str]:
    """Cache→Array: *Zielverzeichnis* ist der Array‑Pfad (Elternordner des Eintrages),
    *Quelle* ist derselbe Pfad mit Cache‑Prefix."""
    dst_dir = str(Path(p).parent)
    if p.startswith(ARRAY_PREFIX):
        src = p.replace(ARRAY_PREFIX, CACHE_PREFIX, 1)
    elif p.startswith(USER_PREFIX):
        src = p.replace(USER_PREFIX, CACHE_PREFIX, 1)
    else:
        # Falls der Eintrag schon Cache‑Pfad wäre, bleibt er so
        src = p
    return src, dst_dir

# Move‑Routine (rsync + safe delete)

def safe_move(src: str, dst_dir: str) -> int:
    try:
        size = os.path.getsize(src)
        free = free_space(dst_dir)
        if free < size:
            log.error("Zu wenig Platz: frei %s < benötigt %s – %s", human(free), human(size), dst_dir)
            return 0
        os.makedirs(dst_dir, exist_ok=True)
        # rsync‑Befehl
        rsync_cmd = ["rsync", "-a"]
        if ARGS.debug:
            rsync_cmd.append("--progress")
        rsync_cmd += [src, str(dst_dir) + "/"]
        if ARGS.run:
            if not shutil.which("rsync"):
                log.error("rsync nicht gefunden – Abbruch.")
                return 0
            ret = subprocess.call(rsync_cmd)
            if ret != 0:
                log.error("rsync‑Fehler %d für %s", ret, src)
                return 0
            # Sicherheits‑Check: Zieldatei existiert und Größe gleich?
            dst_file = os.path.join(dst_dir, os.path.basename(src))
            try:
                if not os.path.isfile(dst_file):
                    log.error("Kopie unvollständig – %s", dst_file)
                    return 0
                if os.path.getsize(dst_file) != size:
                    log.error("Kopie unvollständig (Size mismatch) – %s", dst_file)
                    return 0
            except Exception as e:
                log.error("Kopie‑Check fehlgeschlagen: %s", e)
                return 0
            # Quelle erst jetzt löschen (Delete für echte Move‑Semantik)
            try:
                os.remove(src)
            except Exception as e:
                log.error("Quelle konnte nicht gelöscht werden: %s – %s", src, e)
                return 0
        log.info("SYNC %s -> %s", src, dst_dir)
        return size
    except Exception as e:
        log.error("Fehler bei Sync %s -> %s : %s", src, dst_dir, e)
        return 0

# Executor

def run_jobs(jobs: List[Tuple[str, str]], max_workers: int) -> Tuple[int, int]:
    bytes_total = 0
    if not jobs:
        return 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(safe_move, s, d): (s, d) for s, d in jobs}
        for f in as_completed(futs):
            bytes_total += f.result() or 0
    return len(jobs), bytes_total

# ───────────────────── Job‑Listen aufbauen ───────────────────────────
array2cache_jobs: List[Tuple[str, str]] = []
for p in a2c_raw:
    src, dst = map_array_to_cache(p)
    if not Path(src).is_file():
        continue
    if is_playing(src):
        log.info("Überspringe (läuft): %s", src)
        continue
    array2cache_jobs.append((src, dst))
    if ARGS.debug:
        size = os.path.getsize(src)
        log.debug("PLAN A2C %s -> %s | frei %s | benötigt %s", src, dst, human(free_space(dst)), human(size))

cache2array_jobs: List[Tuple[str, str]] = []
for p in c2a_raw:
    src, dst = map_cache_to_array(p)
    if not Path(src).is_file():
        continue
    if is_playing(src):
        log.info("Überspringe (läuft): %s", src)
        continue
    cache2array_jobs.append((src, dst))
    if ARGS.debug:
        size = os.path.getsize(src)
        log.debug("PLAN C2A %s -> %s | frei %s | benötigt %s", src, dst, human(free_space(dst)), human(size))

# Geplante Gesamtmengen (auch im Dry‑Run zeigen)
cnt_a2c = len(array2cache_jobs)
sz_a2c  = sum(os.path.getsize(s) for s, _ in array2cache_jobs) if array2cache_jobs else 0
cnt_c2a = len(cache2array_jobs)
sz_c2a  = sum(os.path.getsize(s) for s, _ in cache2array_jobs) if cache2array_jobs else 0

log.info("Jobs Array->Cache: %d | Cache->Array: %d", cnt_a2c, cnt_c2a)
log.info("Geplante Datenmenge: A2C %s in %d Dateien | C2A %s in %d Dateien | Gesamt %s",
         human(sz_a2c), cnt_a2c, human(sz_c2a), cnt_c2a, human(sz_a2c + sz_c2a))

if not ARGS.run:
    log.info("Dry‑Run abgeschlossen – verwende --run für echte Moves.")
    sys.exit(0)

# Optional: alte DB der Collect‑Phase entfernen, damit neu aufgebaut wird
try:
    db_file = DATA_DIR / "emby_fileindex.db"
    if db_file.exists():
        db_file.unlink()
        log.info("Alte DB gelöscht: %s", db_file)
except Exception as e:
    log.error("Konnte DB nicht löschen: %s", e)

# ───────────────────── Ausführen ─────────────────────────────────────
cnt1, sz1 = run_jobs(array2cache_jobs, MAX_CACHE)
cnt2, sz2 = run_jobs(cache2array_jobs, MAX_ARRAY)

# TXT‑Dateien nach Erfolg bereinigen (nur verarbeitete Zeilen entfernen)

def update_txt(fn: Path, processed_sources: List[str]):
    if not fn.exists():
        return
    try:
        lines = [l for l in fn.read_text("utf-8").splitlines() if l.strip()]
        keep  = [l for l in lines if l.strip() not in processed_sources]
        fn.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        log.info("TXT %s aktualisiert: entfernt %d Einträge", fn.name, len(lines) - len(keep))
    except Exception as e:
        log.error("Fehler beim Aktualisieren %s: %s", fn.name, e)

update_txt(A2C_TXT, [s for s, _ in array2cache_jobs])
update_txt(C2A_TXT, [s for s, _ in cache2array_jobs])

# Zusammenfassung
log.info("===== Zusammenfassung =====")
log.info("Array -> Cache : %d Dateien | %s", cnt1, human(sz1))
log.info("Cache -> Array : %d Dateien | %s", cnt2, human(sz2))
log.info("Total bewegt  : %s", human(sz1 + sz2))
