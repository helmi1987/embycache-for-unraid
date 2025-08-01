#!/usr/bin/env python3
"""
emby_ondeck_debug.py – On-deck analysis / TXT generator
=====================================================
Analyzes all *Continue watching* entries (NextUp • In‑Progress • Resumable) je
Emby‑User, ermittelt bis zu `number_episodes` next episodes, sucht passende
Sidecar files and creates three path lists for the Unraid mover:

* **embycache_mover_to_exclude.txt** – everything that must never be moved back
* **embycache_array2cache.txt**       – files that still need to go from array → cache
* **embycache_cache2array.txt**       – placeholder (empty)

Read‑only / debug: No files are copied or deleted.
"""
from __future__ import annotations

# ── Standard‑Libs ──────────────────────────────────────────────────────
import json, logging, sqlite3, sys, time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

# ── 3rd‑Party ──────────────────────────────────────────────────────────
import requests

# ─────────────────────  Paths / constants  ────────────────────────────
ROOT_DIR    = Path(__file__).resolve().parent.parent
SCRIPT_DIR  = ROOT_DIR / "scripts"; SCRIPT_DIR.mkdir(exist_ok=True)
CONFIG_DIR  = ROOT_DIR / "config"; CONFIG_DIR.mkdir(exist_ok=True)
DATA_DIR    = ROOT_DIR / "datafiles"; DATA_DIR.mkdir(exist_ok=True)
LOG_DIR     = ROOT_DIR / "logs"; LOG_DIR.mkdir(exist_ok=True)
SETTINGS_FN = CONFIG_DIR / "embycache_settings.json"
DB_FILE     = DATA_DIR / "emby_fileindex.db"
EXCLUDE_TXT = DATA_DIR / "embycache_mover_to_exclude.txt"
A2C_TXT     = DATA_DIR / "embycache_array2cache.txt"
C2A_TXT     = DATA_DIR / "embycache_cache2array.txt"
INDEX_TTL   = 55 * 60  # Sekunden

# ─────────────────────  Vorherige Mover‑Exclude laden (Cache)  ────────
prev_cache_paths: set[str] = set()
if EXCLUDE_TXT.exists():
    for line in EXCLUDE_TXT.read_text("utf-8").splitlines():
        p = line.strip()
        if not p:
            continue
        cache_ver = p.replace('/mnt/user/', '/mnt/cache/', 1)
        if Path(cache_ver).exists():
            prev_cache_paths.add(p)

# ─────────────────────  Logging  ───────────────────────────────────────
log_file = LOG_DIR / f"ondeck_{datetime.now():%Y%m%d_%H%M%S}.log"
latest_log = LOG_DIR / "ondeck_latest.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ondeck")
latest_log.unlink(missing_ok=True)
latest_log.symlink_to(log_file.name)
for old in sorted(LOG_DIR.glob("ondeck_*.log"))[:-5]:
    old.unlink(missing_ok=True)

DEBUG_MODE = "--debug" in sys.argv
if DEBUG_MODE:
    log.setLevel(logging.DEBUG)
    log.debug("DEBUG mode active — no file operations.")

# ─────────────────────  Settings  ──────────────────────────────────────
if not SETTINGS_FN.exists():
    log.critical("Settings file missing — please run embycache_setup.py first!")
    sys.exit(1)

cfg: Dict[str, Any] = json.loads(SETTINGS_FN.read_text("utf-8"))
EMBY_URL         = cfg["EMBY_URL"].rstrip("/")
EMBY_API_KEY     = cfg["EMBY_API_KEY"]
USERS_TOGGLE     = cfg.get("users_toggle", False)
NUMBER_EPISODES  = max(1, cfg.get("number_episodes", 1))
SKIP_ONDECK      = set(cfg.get("skip_ondeck", []))
EMBY_SOURCE      = Path(cfg["emby_source"])
REAL_SOURCE      = Path(cfg["real_source"])
CACHE_DIR        = Path(cfg["cache_dir"])
ARRAY_ROOT       = Path("/mnt/user0")
EMBY_FOLDERS     = cfg["emby_library_folders"]
NAS_FOLDERS      = cfg["nas_library_folders"]

# ─────────────────────  Emby‑API Helper  ───────────────────────────────

def emby_get(endpoint: str, *, params: Dict[str, Any] | None = None):
    params = dict(params or {}, api_key=EMBY_API_KEY)
    r = requests.get(f"{EMBY_URL}{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

# ─────────────────────  Users  ──────────────────────────────────────
users_all = emby_get("/Users")
main_user = next((u for u in users_all if u.get("Policy", {}).get("IsAdministrator")), users_all[0])
user_list = [main_user] + [u for u in users_all if USERS_TOGGLE and u != main_user]

# ─────────────────────  Path mapping  ──────────────────────────────────

def container_to_host(p: str) -> str:
    if not p.startswith(str(EMBY_SOURCE)):
        return p
    rel = Path(p).relative_to(EMBY_SOURCE)
    parts = rel.parts
    for cont, nas in zip(EMBY_FOLDERS, NAS_FOLDERS):
        if cont in parts:
            inside = Path(*parts[parts.index(cont)+1:])
            return str(REAL_SOURCE / nas / inside)
    return str(REAL_SOURCE / rel)

# ─────────────────────  Next‑Episode Helper  ───────────────────────────

def fetch_extra_episodes(item: Dict[str, Any], uid: str) -> List[str]:
    """Returns up to NUMBER_EPISODES‑1 additional episodes (serienweit)."""
    if NUMBER_EPISODES <= 1 or item.get('Type') != 'Episode':
        return []
    show_id, season_id, idx = item.get('SeriesId'), item.get('SeasonId'), item.get('IndexNumber')
    if not (show_id and season_id and idx):
        return []
    eps = emby_get(
        f"/Shows/{show_id}/Episodes",
        params={
            "UserId": uid,
            "Fields": "Path,MediaSources,ParentIndexNumber,IndexNumber",
            "SortBy": "ParentIndexNumber,IndexNumber",
        },
    ).get("Items", [])
    try:
        pos = next(i for i, e in enumerate(eps) if e.get('SeasonId') == season_id and e.get('IndexNumber') == idx)
    except StopIteration:
        pos = -1
    extra = []
    for e in eps[pos+1:]:
        if e.get('MediaSources'):
            extra.append(e['MediaSources'][0]['Path'])
            if len(extra) >= NUMBER_EPISODES - 1:
                break
    return extra

# ─────────────────────  First episode of a series  ─────────────────────

def first_episode(series_id: str, uid: str) -> str | None:
    eps = emby_get(
        f"/Shows/{series_id}/Episodes",
        params={
            "UserId": uid,
            "Fields": "Path,MediaSources,UserData,ParentIndexNumber,IndexNumber",
            "SortBy": "ParentIndexNumber,IndexNumber",
        },
    ).get("Items", [])
    # Ungesehene bevorzugen
    for e in eps:
        if not e.get('UserData', {}).get('Played', False) and e.get('MediaSources'):
            return e['MediaSources'][0]['Path']
    # Fallback
    for e in eps:
        if e.get('MediaSources'):
            return e['MediaSources'][0]['Path']
    return None

# ─────────────────────  On‑Deck / Resume  ──────────────────────────────

def fetch_ondeck(user: Dict[str, Any]) -> List[str]:
    """Determines *Resume* entries.

    1. **Primary**: `/Users/{id}/Items/Resume` (standard UI‑Endpoint)
    2. **Fallback**: `/user_usage_stats/UserPlaylist` – falls leer oder Server <4.8.

    Series objects without `MediaSources` → first unseen episode; after that
    adds up to `NUMBER_EPISODES-1` subsequent episodes.
    """

    uid = user["Id"]
    name = user.get("Name", uid)
    out: list[str] = []

    def add_path(item: Dict[str, Any]):
        # Series without a file → first episode
        if item.get("Type") == "Series" and not item.get("MediaSources"):
            ep = first_episode(item["Id"], uid)
            if ep and ep not in out:
                out.append(ep)
            return
        # Regular entry
        msrc = item.get("MediaSources")
        if msrc:
            p = msrc[0]["Path"]
            if p not in out:
                out.append(p)
                out.extend(fetch_extra_episodes(item, uid))

    # ---- 1) /Users/{id}/Items/Resume ---------------------------------
    try:
        params = {
            "Limit": 1000,
            "Recursive": True,
            "UserId": uid,
            "MediaTypes": "Video",
            "Fields": "Path,MediaSources,SeriesId,SeasonId,IndexNumber,Type,UserData",
        }
        days = cfg.get("days_to_monitor")
        if days:
            params["MinDateLastPlayed"] = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        resume_items = emby_get(f"/Users/{uid}/Items/Resume", params=params)
        for it in resume_items.get("Items", resume_items):
            add_path(it)
    except Exception as e:
        log.debug("Resume‑Fetch‑Error %s: %s", name, e)

    # ---- 2) Fallback Playlist (wenn leer) -----------------------------
    if not out:
        try:
            params = {
                "UserId": uid,
                "Limit": 1000,
                "Fields": "Path,MediaSources,SeriesId,SeasonId,IndexNumber,Type,UserData",
            }
            pl = emby_get("/user_usage_stats/UserPlaylist", params=params)
            items = pl.get("Items", []) if isinstance(pl, dict) else pl
            for it in items:
                add_path(it)
        except Exception as e:
            log.debug("Playlist‑Fetch‑Error %s: %s", name, e)

    log.info("%s – Resume/Playlist entries: %d", name, len(out))
    return out

# ─────────────────────  SQLite‑Index  ─────────────────────────────────

def rebuild_index(db: Path):
    log.info("Building file index …")
    db.unlink(missing_ok=True)
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE files(loc TEXT, name TEXT)")
        cur.executemany("INSERT INTO files VALUES('cache',?)", ((p.name,) for p in CACHE_DIR.rglob('*') if p.is_file()))
        cur.executemany("INSERT INTO files VALUES('array',?)", ((p.name,) for p in ARRAY_ROOT.rglob('*') if p.is_file()))
        conn.commit()
    log.info("Index fertig.")

if not DB_FILE.exists() or time.time() - DB_FILE.stat().st_mtime > INDEX_TTL:
    rebuild_index(DB_FILE)
else:
    log.info("Using existing index (younger than 55 minutes).")

conn = sqlite3.connect(DB_FILE)
cur  = conn.cursor()
cache_names = {n for (n,) in cur.execute("SELECT name FROM files WHERE loc='cache'")}
array_names = {n for (n,) in cur.execute("SELECT name FROM files WHERE loc='array'")}

# ─────────────────────  Analysis  ───────────────────────────────────────
raw: List[Tuple[str,str]] = []
for u in user_list:
    if u['Id'] in SKIP_ONDECK:
        continue
    for src in fetch_ondeck(u):
        host = Path(container_to_host(src)).resolve()
        rel  = host.relative_to(REAL_SOURCE)
        loc  = 'cache' if (CACHE_DIR/rel).exists() or host.name in cache_names else (
               'array' if (ARRAY_ROOT/rel).exists() or host.name in array_names else 'missing')
        raw.append((str(host), loc))
        if DEBUG_MODE:
            print(f"{loc:<7} {host}")

# Dedup (Bevorzugt cache > array > missing) ---------------------------
prio = {'cache':2, 'array':1, 'missing':0}
uniq: Dict[str,str] = {}
for p, l in raw:
    if p not in uniq or prio[l] > prio[uniq[p]]:
        uniq[p] = l
entries = sorted(uniq.items())

# ─────────────────────  Sidecar‑Suche  ────────────────────────────────
SIDECAR_EXTS = {'.nfo', '.jpg', '.jpeg', '.bif', '.png', '.tbn', '.ico'}
sidecars: set[str] = set()

for media, _ in entries:
    media_path = Path(media)
    base = media_path.stem.lower()
    for f in media_path.parent.iterdir():
        if f.is_file() and f.suffix.lower() in SIDECAR_EXTS:
            stem = f.stem.lower()
            # exact video (or additional qualifiers like -320-10)
            if stem == base or stem.startswith(base):
                sidecars.add(str(f))
                if DEBUG_MODE:
                    print(f"sidecar {f}")

# ─────────────────────  TXT‑Export  ───────────────────────────────────  ───────────────────────────────────
exclude: set[str] = set()
array2cache: set[str] = set()

for p, loc in entries:
    exclude.add(p)
    if loc == 'array':
        array2cache.add(p.replace('/mnt/user/', '/mnt/user0/', 1))
for sc in sidecars:
    exclude.add(sc)
    if not Path(sc.replace('/mnt/user/', '/mnt/cache/', 1)).exists():
        array2cache.add(sc.replace('/mnt/user/', '/mnt/user0/', 1))

EXCLUDE_TXT.write_text("\n".join(sorted(exclude)) + "\n", encoding="utf-8")
A2C_TXT.write_text("\n".join(sorted(array2cache)) + "\n", encoding="utf-8")
# ----- Cache→Array: Alles, was vorher im Cache war, jetzt aber NICHT mehr
new_cache_paths = {p for p in exclude if Path(p.replace('/mnt/user/', '/mnt/cache/', 1)).exists()}
removed = prev_cache_paths - new_cache_paths
cache2array_paths = {
    p.replace('/mnt/user/', '/mnt/user0/', 1)
    for p in removed
}
C2A_TXT.write_text("\n".join(sorted(cache2array_paths)) + "\n", encoding="utf-8")

# ─────────────────────  Summary  ───────────────────────────────────────
count_cache   = sum(1 for _, l in entries if l == 'cache')
count_array   = sum(1 for _, l in entries if l == 'array')
count_missing = len(entries) - count_cache - count_array
summary = (
    f"Gesamt Media: {len(entries)}  |  cache: {count_cache}  |  array: {count_array}  |  missing: {count_missing}\n"
    f"Sidecars: {len(sidecars)}  |  exclude.txt: {len(exclude)}  |  array2cache.txt: {len(array2cache)}"
)
log.info(summary)
if DEBUG_MODE:
    print("*** Debug run finished — no file operations. ***\n" + summary)

conn.close()

