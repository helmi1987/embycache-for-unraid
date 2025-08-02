#!/usr/bin/env python3
"""
embycache_collect.py – Resume/On‑Deck → Pfadlisten (Unraid)
==========================================================
Sammelt pro Emby‑User alle *Fortsetzen*‑Einträge (Resume) inkl. Folgeepisoden,
filtert strikt auf **freigegebene Bibliotheken** (aus `valid_libraries`) und
mappen Docker‑Pfade → Unraid‑Host. Erzeugt/aktualisiert TXT‑Listen in
`datafiles/` und pflegt einen schnellen Dateiname‑Index (`emby_fileindex.db`).

**Wichtig**
- `embycache_mover_to_exclude.txt` wird **nur** geschrieben, wenn das Script mit
  `--run` gestartet wird. Im Debug/Dry‑Run bleibt die bestehende Datei unverändert.
- `array2cache`/`cache2array` werden immer geschrieben (für Preview/Planung ok).
"""
from __future__ import annotations

# Stdlib
import argparse, json, logging, sqlite3, sys, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Tuple, Set

# Third‑party
import requests

# ───────────────────────── Pfade/Struktur ─────────────────────────────
ROOT_DIR   = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config";    CONFIG_DIR.mkdir(exist_ok=True)
DATA_DIR   = ROOT_DIR / "datafiles"; DATA_DIR.mkdir(exist_ok=True)
LOG_DIR    = ROOT_DIR / "logs";      LOG_DIR.mkdir(exist_ok=True)

SETTINGS_FN = CONFIG_DIR / "embycache_settings.json"
DB_FILE     = DATA_DIR / "emby_fileindex.db"
EXCLUDE_TXT = DATA_DIR / "embycache_mover_to_exclude.txt"
A2C_TXT     = DATA_DIR / "embycache_array2cache.txt"
C2A_TXT     = DATA_DIR / "embycache_cache2array.txt"
INDEX_TTL_S = 55 * 60  # 55 Minuten

# ───────────────────────── CLI / Logging ──────────────────────────────
parser = argparse.ArgumentParser(description="Collect Resume/On‑Deck and prepare mover TXT files")
parser.add_argument("--debug", action="store_true", help="Verbose/Debug‑Ausgabe")
parser.add_argument("--run",   action="store_true", help="EXCLUDE‑Datei wirklich überschreiben")
ARGS = parser.parse_args()

log_file = LOG_DIR / f"collect_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("collect")

# ───────────────────────── Settings laden ─────────────────────────────
if not SETTINGS_FN.exists():
    log.critical("Settings‑Datei fehlt – bitte embycache_setup.py ausführen!")
    sys.exit(1)

cfg: Dict[str, Any] = json.loads(SETTINGS_FN.read_text("utf-8"))

# Basis‑Konfig
EMBY_URL         = cfg["EMBY_URL"].rstrip("/")
EMBY_API_KEY     = cfg["EMBY_API_KEY"]
USERS_TOGGLE     = cfg.get("users_toggle", False)
NUMBER_EPISODES  = max(1, int(cfg.get("number_episodes", 1)))
DAYS_TO_MONITOR  = int(cfg.get("days_to_monitor", 0))
VALID_LIBRARIES  = set(cfg.get("valid_libraries", []))  # z. B. {"Filme","Serien"}

# Pfad‑Mapping Docker → Host
EMBY_SOURCE      = Path(cfg["emby_source"]).resolve()                    # z. B. /data
REAL_SOURCE      = Path(cfg["real_source"]).resolve()                    # z. B. /mnt/user/PlexMedia/video
CACHE_DIR        = Path(cfg["cache_dir"]).resolve()                      # z. B. /mnt/cache/PlexMedia/video
ARRAY_ROOT       = Path("/mnt/user0")                                    # Array‑Root (read‑only Klassifizierung)
EMBY_LIBS        = [str(x).strip("/\\") for x in cfg.get("emby_library_folders", [])]
NAS_LIBS         = [str(x).strip("/\\") for x in cfg.get("nas_library_folders",  [])]
LIB_MAP          = dict(zip(EMBY_LIBS, NAS_LIBS))                         # z. B. {"Serien":"TV","Filme":"Movies",…}

if ARGS.debug:
    log.debug("DEBUG‑Modus aktiv – keine Dateioperationen.")
    log.debug("Library‑Mapping: %s", LIB_MAP)

# ───────────────────────── Emby‑API Helper ────────────────────────────

def emby_get(endpoint: str, *, params: Dict[str, Any] | None = None):
    params = dict(params or {}, api_key=EMBY_API_KEY)
    r = requests.get(EMBY_URL + endpoint, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

# ───────────────────────── Bibliotheks‑Filter ─────────────────────────

def in_allowed_library(container_path: str) -> bool:
    """Erlaubt nur Pfade, deren **erstes Segment nach `emby_source`** in
    `valid_libraries` enthalten ist (z. B. `/data/Serien/...` oder `/data/Filme/...`).
    """
    try:
        rel = Path(container_path).resolve().relative_to(EMBY_SOURCE)
    except Exception:
        return False
    first = rel.parts[0] if rel.parts else ""
    return first in VALID_LIBRARIES

# ───────────────────────── Pfad‑Übersetzung ───────────────────────────

def container_to_host(container_path: str) -> str:
    """Docker‑Pfad → Host‑Pfad.
    Beispiel: `/data/Serien/Show/Ep.mkv` → `/mnt/user/PlexMedia/video/TV/Show/Ep.mkv`.
    """
    p = Path(container_path)
    if not str(p).startswith(str(EMBY_SOURCE)):
        return str(p)  # unbekannt, unverändert
    rel = p.resolve().relative_to(EMBY_SOURCE)          # z. B. Serien/Show/Ep.mkv
    parts = list(rel.parts)
    if not parts:
        return str((REAL_SOURCE / rel).resolve())
    lib = parts[0]
    mapped = LIB_MAP.get(lib, lib)                      # Serien→TV, Filme→Movies, …
    inside = Path(*parts[1:]) if len(parts) > 1 else Path()
    return str((REAL_SOURCE / mapped / inside).resolve())

# ───────────────────────── Zusatz‑Episoden ────────────────────────────

def fetch_extra_episodes(item: Dict[str, Any], uid: str) -> List[str]:
    if NUMBER_EPISODES <= 1 or item.get("Type") != "Episode":
        return []
    show_id, season_id, idx = item.get("SeriesId"), item.get("SeasonId"), item.get("IndexNumber")
    if not (show_id and season_id and isinstance(idx, int)):
        return []
    eps = emby_get(
        f"/Shows/{show_id}/Episodes",
        params={"UserId": uid, "Fields": "Path,MediaSources,ParentIndexNumber,IndexNumber", "SortBy": "ParentIndexNumber,IndexNumber"},
    ).get("Items", [])
    # Startposition suchen
    start = -1
    for i, e in enumerate(eps):
        if e.get("SeasonId") == season_id and e.get("IndexNumber") == idx:
            start = i; break
    extra: List[str] = []
    for e in eps[start+1:]:
        m = e.get("MediaSources")
        if not m:
            continue
        path = m[0].get("Path")
        if path and in_allowed_library(path):
            extra.append(path)
            if len(extra) >= NUMBER_EPISODES - 1:
                break
    return extra

# ───────────────────────── Erste ungesehene Episode ───────────────────

def first_episode(series_id: str, uid: str) -> str | None:
    eps = emby_get(
        f"/Shows/{series_id}/Episodes",
        params={"UserId": uid, "Fields": "Path,MediaSources,UserData,ParentIndexNumber,IndexNumber", "SortBy": "ParentIndexNumber,IndexNumber"},
    ).get("Items", [])
    for e in eps:
        if not e.get("UserData", {}).get("Played") and e.get("MediaSources"):
            p = e["MediaSources"][0]["Path"]
            if in_allowed_library(p):
                return p
    for e in eps:
        if e.get("MediaSources"):
            p = e["MediaSources"][0]["Path"]
            if in_allowed_library(p):
                return p
    return None

# ───────────────────────── Resume / Fallback Playlist ─────────────────

def fetch_ondeck(user: Dict[str, Any]) -> List[str]:
    uid = user["Id"]
    name = user.get("Name", uid)
    out: List[str] = []

    def add_item(it: Dict[str, Any]):
        if it.get("Type") == "Series" and not it.get("MediaSources"):
            ep = first_episode(it["Id"], uid)
            if ep and ep not in out:
                out.append(ep)
            return
        m = it.get("MediaSources")
        if m:
            p = m[0].get("Path")
            if p and in_allowed_library(p) and p not in out:
                out.append(p)
                out.extend(fetch_extra_episodes(it, uid))

    # 1) Resume
    try:
        params = {
            "Limit": 1000, "Recursive": True, "UserId": uid,
            "MediaTypes": "Video",
            "Fields": "Path,MediaSources,SeriesId,SeasonId,IndexNumber,Type,UserData",
        }
        if DAYS_TO_MONITOR:
            params["MinDateLastPlayed"] = (datetime.utcnow() - timedelta(days=DAYS_TO_MONITOR)).isoformat() + "Z"
        res = emby_get(f"/Users/{uid}/Items/Resume", params=params)
        items = res.get("Items", res) if isinstance(res, dict) else res
        for it in items:
            # Bibliotheks‑Filter **VOR** Aufnahme prüfen
            m = it.get("MediaSources")
            test_path = (m[0].get("Path") if m else None)
            if it.get("Type") == "Series" and not test_path:
                # Serien ohne Datei zulassen – first_episode übernimmt Filter
                add_item(it)
            elif test_path and in_allowed_library(test_path):
                add_item(it)
    except Exception as e:
        log.debug("Resume‑Fetch‑Fehler %s: %s", name, e)

    # 2) Fallback: UserPlaylist
    if not out:
        try:
            pl = emby_get("/user_usage_stats/UserPlaylist", params={"UserId": uid, "Limit": 1000, "Fields": "Path,MediaSources,SeriesId,SeasonId,IndexNumber,Type,UserData"})
            items = pl.get("Items", []) if isinstance(pl, dict) else (pl or [])
            for it in items:
                m = it.get("MediaSources")
                p = m[0].get("Path") if m else None
                if p and in_allowed_library(p):
                    add_item(it)
        except Exception as e:
            log.debug("Playlist‑Fetch‑Fehler %s: %s", name, e)

    log.info("%s – Resume‑Einträge: %d", name, len(out))
    return out

# ───────────────────────── Benutzerliste ──────────────────────────────
users = emby_get("/Users")
main_user = next((u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
user_list = [main_user] + [u for u in users if USERS_TOGGLE and u != main_user]
SKIP_ONDECK = set(cfg.get("skip_ondeck", []))

# ───────────────────────── Index (SQLite) ─────────────────────────────

def rebuild_index(db: Path):
    log.info("Baue Datei‑Index …")
    db.unlink(missing_ok=True)
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE files(loc TEXT, name TEXT)")
        cur.executemany("INSERT INTO files VALUES('cache',?)", ((p.name,) for p in CACHE_DIR.rglob('*') if p.is_file()))
        cur.executemany("INSERT INTO files VALUES('array',?)", ((p.name,) for p in ARRAY_ROOT.rglob('*') if p.is_file()))
        conn.commit()
    log.info("Index fertig.")

if not DB_FILE.exists() or time.time() - DB_FILE.stat().st_mtime > INDEX_TTL_S:
    rebuild_index(DB_FILE)
else:
    log.info("Nutze vorhandenen Index (jünger als 55 Min).")

conn = sqlite3.connect(DB_FILE); cur = conn.cursor()
cache_names = {n for (n,) in cur.execute("SELECT name FROM files WHERE loc='cache'")}
array_names = {n for (n,) in cur.execute("SELECT name FROM files WHERE loc='array'")}

# ───────────────────────── Sammeln / Klassifizieren ───────────────────
raw: List[Tuple[str,str]] = []  # (host_path, loc)
for u in user_list:
    if u["Id"] in SKIP_ONDECK:
        continue
    for src in fetch_ondeck(u):
        host = Path(container_to_host(src)).resolve()
        try:
            rel = host.relative_to(REAL_SOURCE)
        except Exception:
            # Falls der Host‑Pfad ausserhalb REAL_SOURCE landet, auslassen
            if ARGS.debug:
                log.debug("Übersprungen (out of REAL_SOURCE): %s", host)
            continue
        loc = (
            'cache'   if (CACHE_DIR/rel).exists() else
            'array'   if (ARRAY_ROOT/rel).exists() else
            'cache'   if host.name in cache_names else
            'array'   if host.name in array_names else
            'missing'
        )
        raw.append((str(host), loc))
        if ARGS.debug:
            print(f"{loc:<7} {host}")

# Dedup mit Priorität cache > array > missing
prio = {'cache': 2, 'array': 1, 'missing': 0}
uniq: Dict[str,str] = {}
for p, l in raw:
    if (p not in uniq) or (prio[l] > prio[uniq[p]]):
        uniq[p] = l
entries = sorted(uniq.items())

# ───────────────────────── Sidecars sammeln ───────────────────────────
SIDECAR_EXTS = {'.nfo', '.jpg', '.jpeg', '.png', '.tbn', '.ico', '.bif'}
sidecars: Set[str] = set()
for media, _ in entries:
    mp = Path(media)
    base = mp.stem.lower()
    parent = mp.parent
    if not parent.is_dir():
        continue
    for f in parent.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in SIDECAR_EXTS:
            continue
        st = f.stem.lower()
        if st == base or st.startswith(base):
            sidecars.add(str(f))
            if ARGS.debug:
                print(f"sidecar {f}")

# ───────────────────────── Exclude/A2C/C2A berechnen ─────────────────
# Vorherigen Cache‑Stand (nur tatsächlich im Cache befindliche Pfade)
prev_cache_paths: Set[str] = set()
if EXCLUDE_TXT.exists():
    for line in EXCLUDE_TXT.read_text("utf-8").splitlines():
        p = line.strip()
        if not p:
            continue
        if Path(p.replace('/mnt/user/', '/mnt/cache/', 1)).exists():
            prev_cache_paths.add(p)

exclude_paths: Set[str] = set()
array2cache: Set[str]   = set()
for p, loc in entries:
    exclude_paths.add(p)  # immer /mnt/user/…
    if loc == 'array':
        array2cache.add(p.replace('/mnt/user/', '/mnt/user0/', 1))
for sc in sidecars:
    exclude_paths.add(sc)
    if not Path(sc.replace('/mnt/user/', '/mnt/cache/', 1)).exists():
        array2cache.add(sc.replace('/mnt/user/', '/mnt/user0/', 1))

# Exclude nur in --run schreiben
if ARGS.run:
    EXCLUDE_TXT.write_text("\n".join(sorted(exclude_paths)) + "\n", encoding="utf-8")
else:
    if ARGS.debug:
        log.debug("EXCLUDE nicht überschrieben (Dry‑Run). Vorhanden: %s", EXCLUDE_TXT.exists())

A2C_TXT.write_text("\n".join(sorted(array2cache)) + "\n", encoding="utf-8")

# Cache→Array aus Diff (nur echte Cache‑Dateien)
new_cache_paths = {p for p in exclude_paths if Path(p.replace('/mnt/user/', '/mnt/cache/', 1)).exists()}
removed = prev_cache_paths - new_cache_paths
c2a = {p.replace('/mnt/user/', '/mnt/user0/', 1) for p in removed}
C2A_TXT.write_text("\n".join(sorted(c2a)) + "\n", encoding="utf-8")

# ───────────────────────── Summary ────────────────────────────────────
count_cache   = sum(1 for _, l in entries if l == 'cache')
count_array   = sum(1 for _, l in entries if l == 'array')
count_missing = len(entries) - count_cache - count_array
summary = (
    f"Gesamt Media: {len(entries)}  |  cache: {count_cache}  |  array: {count_array}  |  missing: {count_missing}\n"
    f"Sidecars: {len(sidecars)}  |  exclude.txt: {len(exclude_paths)}{' (nicht geschrieben)' if not ARGS.run else ''}  |  array2cache.txt: {len(array2cache)}  |  cache2array.txt: {len(c2a)}"
)
log.info(summary)
if ARGS.debug:
    print("*** Debug‑Durchlauf Ende – keine Dateioperationen. ***\n" + summary)
