#!/usr/bin/env python3
"""
EmbyCache – gemeinsame Funktionen für embycache_run.py, embycache_cleaner.py und embycache_setup.py.

Enthält: Config mit Defaults und Validierung, Pfad-Übersetzung (Docker → Host → relativ zum Share),
Emby-API-Client (nur Standardbibliothek, kein `requests`), Aufruf des Unraid move-Binaries,
Lockfile, Exclude-Liste und Logging.

Umgebungsvariablen (gelten für alle Scripte):
  EMBYCACHE_DIR        Arbeitsverzeichnis für Settings, Exclude-Liste, Lock und logs/
                       Default: das Verzeichnis, in dem die Scripte liegen
  EMBYCACHE_CONFIG     Pfad zur Settings-JSON; Default: $EMBYCACHE_DIR/embycache_settings.json
  EMBYCACHE_LOG_LEVEL  DEBUG | INFO | WARNING; Default INFO
"""
import copy
import datetime
import fcntl
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

__version__ = "7.3.0 (2026-10-09)"

BASE_DIR = Path(os.environ.get("EMBYCACHE_DIR") or Path(__file__).resolve().parent)
CONFIG_FILE = Path(os.environ.get("EMBYCACHE_CONFIG") or (BASE_DIR / "embycache_settings.json"))
EXCLUDE_FILE = BASE_DIR / "embycache_exclude.txt"
LOCK_FILE = BASE_DIR / "embycache.lock"
STATUS_FILE = BASE_DIR / "embycache_status.json"
LOG_DIR = BASE_DIR / "logs"

MOVER_CANDIDATES = ["/usr/libexec/unraid/move", "/usr/local/sbin/move", "/usr/local/bin/move"]
MOVER_PIDFILE = "/var/run/mover.pid"

# Alle Config-Schlüssel mit Default. Unbekannte Schlüssel werden ignoriert, fehlende ergänzt.
DEFAULTS = {
    "cache_path": "/mnt/cache",          # Pool, auf den gecacht wird (z.B. /mnt/cache oder /mnt/master)
    "array_path": "/mnt/user0",          # Array-Sicht ohne Pools (FUSE)
    "user_path": "/mnt/user",            # Zusammengeführte Sicht (Emby sieht diese Pfade)
    "array_disks_glob": "/mnt/disk[0-9]*",  # Echte Array-Disks (Quelle bei array_source = disk, Disk-Zuordnung für Parallelität/Status)
    "array_source": "user0",             # rsync-Quelle: user0 = /mnt/user0/... wie das Original, disk = echte /mnt/diskN-Pfade
    "instances": [],                     # [{"servername": "...", "url": "...", "api_key": "...", "path_mappings": {...}}]
    "path_mappings": {},                 # Globales Mapping Docker-Pfad -> Host-Pfad (/mnt/user/...)
    "libraries": [],                     # Nur informativ (Wizard); Filter passiert über path_mappings
    "valid_users": [],                   # Liste von User-IDs oder Dict {id: {"budget": "300G"}}; leer = alle Benutzer
    "number_episodes": 3,                # Zähl-Modus: wie viele Folgen nach der aktuellen vorgeladen werden
    "cache_budget": "",                  # Budget-Modus: z.B. "2.5T" oder "800G" – leer = Zähl-Modus
    "movie_share_percent": 50,           # Budget-Modus: Anteil Filme am Benutzer-Budget (weich, nach Bytes)
    "max_episodes_per_series": 0,        # Budget-Modus: Folgen pro Serie höchstens (0 = das Budget entscheidet)
    "max_resume_items": 10,              # Wie viele "Weiterschauen"-Einträge pro Benutzer berücksichtigt werden
    "max_favorite_series": 10,           # Wie viele Favoriten-Serien pro Benutzer (0 = keine Favoriten)
    "use_next_up": True,                 # Auch Embys «Als Nächstes» (/Shows/NextUp) als Quelle – hält Serien zwischen zwei Folgen im Cache
    "min_free_percent": 20,              # Unter diesem Freiplatz (Prozent) wird nichts mehr auf den Cache kopiert
    "movie_mode": "folder",              # folder = ganzer Filmordner mitnehmen, file = nur Dateien mit gleichem Namen
    "create_share_root": False,          # Share-Wurzel auf dem Pool automatisch anlegen (ZFS: besser vorher als Dataset!)
    "mover_bin": "",                     # Leer = automatisch erkennen
    "mover_debug_level": 0,              # Parameter -d für das move-Binary (0 = still, 1..3 = ausführlicher)
    "rsync_args": ["-aAX", "--numeric-ids"],  # Vollständige rsync-Optionen (Default = wie das Original)
    "fill_tool": "rsync",                # Array -> Cache: rsync (kopieren + Quelle löschen) oder mover (Unraid move-Binary)
    "cleanup_tool": "mover",             # Cache -> Array: mover (Unraid move-Binary, wie das Original) oder rsync
                                         #   (rsync /mnt/<pool>/<rel> -> /mnt/user0/<rel>, unabhängig von der Mover-Richtung des Shares)
    "parallel_per_disk": 1,              # Befüllen per rsync: gleichzeitige Kopien pro Quell-Disk (1 = wie das Original)
    "parallel_total": 1,                 # Befüllen per rsync: gleichzeitige Kopien insgesamt (0 = ohne Limit, nur parallel_per_disk)
    "copy_progress": True,               # rsync zusätzlich mit --info=progress2 (nur Ausgabe) – Fortschritt pro Datei im Status
    "status_log_interval": 60,           # Sekunden zwischen Fortschrittszeilen im Log während des Kopierens (0 = aus)
    "api_timeout": 10,                   # Sekunden pro API-Aufruf
    "shares_cfg_dir": "/boot/config/shares",  # Unraid Share-Konfigurationen (für die Mover-Richtungs-Prüfung)
}


# --------------------------------------------------------------------------- Logging
def setup_logging(name, filename):
    """Rotierendes Logfile (10 MB x 20) plus Konsole. Level über EMBYCACHE_LOG_LEVEL."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.environ.get("EMBYCACHE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    log = logging.getLogger(name)
    log.setLevel(level)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = RotatingFileHandler(LOG_DIR / filename, maxBytes=10 * 1024 * 1024, backupCount=20, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def human(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} EB"


# --------------------------------------------------------------------------- Config
class ConfigError(Exception):
    pass


_UNITS = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024 ** 2, "MB": 1024 ** 2, "G": 1024 ** 3, "GB": 1024 ** 3,
          "T": 1024 ** 4, "TB": 1024 ** 4, "P": 1024 ** 5, "PB": 1024 ** 5}


def parse_size(value):
    """'2.5T', '800G', '100GB', '1536M' oder eine Zahl (Bytes) -> Bytes; leer/0 -> 0."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper().replace(" ", "")
    if not text:
        return 0
    num = text.rstrip("KMGTPB")
    unit = text[len(num):]
    try:
        return int(float(num) * _UNITS[unit])
    except (ValueError, KeyError):
        raise ConfigError(f"Ungültige Grössenangabe: '{value}' (erlaubt z.B. 2.5T, 800G, 100GB)")


def load_config(require_paths=True):
    """Liest die Settings-JSON, ergänzt Defaults und prüft das Nötigste."""
    if not CONFIG_FILE.exists():
        raise ConfigError(f"Konfiguration nicht gefunden: {CONFIG_FILE} – zuerst embycache_setup.py ausführen")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Konfiguration ist kein gültiges JSON ({CONFIG_FILE}): {e}")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, copy.deepcopy(val))

    # Umgebungsvariablen überschreiben einzelne Werte
    env_min = os.environ.get("EMBYCACHE_MIN_FREE_PERCENT")
    if env_min:
        cfg["min_free_percent"] = env_min
    env_dbg = os.environ.get("EMBYCACHE_MOVER_DEBUG")
    if env_dbg:
        cfg["mover_debug_level"] = env_dbg
    try:
        cfg["min_free_percent"] = float(cfg["min_free_percent"])
        cfg["mover_debug_level"] = int(cfg["mover_debug_level"])
    except (TypeError, ValueError):
        raise ConfigError("min_free_percent muss eine Zahl, mover_debug_level eine ganze Zahl sein "
                          "(auch EMBYCACHE_MIN_FREE_PERCENT / EMBYCACHE_MOVER_DEBUG prüfen)")
    env_rsync = os.environ.get("EMBYCACHE_RSYNC_ARGS")
    if env_rsync is not None:
        cfg["rsync_args"] = env_rsync.split()
    env_fill = os.environ.get("EMBYCACHE_FILL_TOOL")
    if env_fill:
        cfg["fill_tool"] = env_fill.lower()

    for key in ("cache_path", "array_path", "user_path"):
        cfg[key] = str(cfg[key]).rstrip("/") or "/"

    env_budget = os.environ.get("EMBYCACHE_CACHE_BUDGET")
    if env_budget is not None:
        cfg["cache_budget"] = env_budget

    # valid_users: Liste von IDs oder Dict {id: {"budget": "300G", ...}} -> Liste + feste Budgets
    vu = cfg["valid_users"]
    cfg["user_budgets"] = {}
    if isinstance(vu, dict):
        for uid, opts in vu.items():
            b = parse_size((opts or {}).get("budget")) if isinstance(opts, dict) else 0
            if b > 0:
                cfg["user_budgets"][str(uid)] = b
    cfg["valid_users"] = [str(k) for k in (vu.keys() if isinstance(vu, dict) else vu) if str(k).strip()]
    cfg["cache_budget_bytes"] = parse_size(cfg["cache_budget"])
    try:
        cfg["movie_share_percent"] = float(cfg["movie_share_percent"])
        cfg["max_episodes_per_series"] = int(cfg["max_episodes_per_series"])
    except (TypeError, ValueError):
        raise ConfigError("movie_share_percent muss eine Zahl 0–100, max_episodes_per_series eine ganze Zahl sein")
    if not 0 <= cfg["movie_share_percent"] <= 100:
        raise ConfigError("movie_share_percent muss zwischen 0 und 100 liegen")

    if not cfg["instances"]:
        raise ConfigError("Keine Emby-Instanz konfiguriert (instances)")
    for i, inst in enumerate(cfg["instances"], 1):
        inst["url"] = str(inst.get("url", "")).rstrip("/")
        inst.setdefault("servername", f"Server{i}")
        if not inst["url"] or not inst.get("api_key"):
            raise ConfigError(f"Instanz {i} ({inst.get('servername')}): url oder api_key fehlt")
        # Pro-Instanz-Mappings (README v6) und globale Mappings zusammenführen
        merged = dict(cfg["path_mappings"])
        merged.update(inst.get("path_mappings") or {})
        inst["_mappings"] = {k.rstrip("/"): v.rstrip("/") for k, v in merged.items() if k and v}
        if not inst["_mappings"]:
            raise ConfigError(f"Instanz {i} ({inst['servername']}): keine path_mappings – ohne Mapping passiert nichts")

    if require_paths:
        for key in ("cache_path", "array_path"):
            if not Path(cfg[key]).is_dir():
                raise ConfigError(f"{key} existiert nicht: {cfg[key]}")
    if cfg["movie_mode"] not in ("folder", "file"):
        raise ConfigError("movie_mode muss 'folder' oder 'file' sein")
    if cfg["fill_tool"] not in ("rsync", "mover"):
        raise ConfigError("fill_tool muss 'rsync' oder 'mover' sein")
    env_cleanup = os.environ.get("EMBYCACHE_CLEANUP_TOOL")
    if env_cleanup:
        cfg["cleanup_tool"] = env_cleanup.lower()
    if cfg["cleanup_tool"] not in ("rsync", "mover"):
        raise ConfigError("cleanup_tool muss 'rsync' oder 'mover' sein")
    if cfg["array_source"] not in ("user0", "disk"):
        raise ConfigError("array_source muss 'user0' oder 'disk' sein")
    for key, env, minimum in (("parallel_per_disk", "EMBYCACHE_PARALLEL_PER_DISK", 1),
                              ("parallel_total", "EMBYCACHE_PARALLEL_TOTAL", 0),
                              ("status_log_interval", "EMBYCACHE_STATUS_LOG_INTERVAL", 0)):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            raise ConfigError(f"{key} muss eine ganze Zahl sein")
        if cfg[key] < minimum:
            raise ConfigError(f"{key} muss mindestens {minimum} sein")
    cfg["copy_progress"] = bool(cfg["copy_progress"])
    if not cfg["rsync_args"]:
        raise ConfigError("rsync_args darf nicht leer sein (Original: [\"-aAX\", \"--numeric-ids\"])")
    return cfg


def save_config(cfg):
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    for inst in clean.get("instances", []):
        inst.pop("_mappings", None)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(clean, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- Emby API
class EmbyApi:
    """Minimaler Emby-Client auf urllib-Basis. API-Key geht als Header X-Emby-Token, nicht in die URL."""

    def __init__(self, url, api_key, timeout=10):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def get(self, path, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        full = f"{self.url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(full, headers={"X-Emby-Token": self.api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} bei {path}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"{path} nicht erreichbar: {e}") from e

    def items(self, path, **params):
        data = self.get(path, **params)
        if isinstance(data, dict):
            return data.get("Items", [])
        return data or []


# --------------------------------------------------------------------------- Pfade
class Locations:
    """Übersetzt Docker-Pfade in Host-Pfade und findet die Datei auf Cache, Array-Sicht und echter Disk."""

    def __init__(self, cfg, mappings):
        self.cache = Path(cfg["cache_path"])
        self.array = Path(cfg["array_path"])
        self.user = Path(cfg["user_path"])
        self.disks_glob = cfg["array_disks_glob"]
        self.mappings = mappings
        # Bibliotheks-Wurzeln relativ zum Share (z.B. "Filme") – werden nie als Filmordner behandelt
        self.library_roots = set()
        for host in mappings.values():
            rel = self.to_rel(host)
            if rel is not None:
                self.library_roots.add(rel)

    def to_host(self, docker_path):
        """Längstes passendes Mapping, Prefix nur an Pfadgrenzen."""
        best, repl = "", ""
        for d, h in self.mappings.items():
            if (docker_path == d or docker_path.startswith(d + "/")) and len(d) > len(best):
                best, repl = d, h
        if not best:
            return None
        return repl + docker_path[len(best):]

    def to_rel(self, host_path):
        """Host-Pfad -> Pfad relativ zur Share-Ebene (z.B. /mnt/user/Filme/X/x.mkv -> Filme/X/x.mkv)."""
        p = Path(host_path)
        for root in (self.user, self.array, self.cache):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            return rel if rel.parts else None
        return None

    def rel_from_docker(self, docker_path):
        host = self.to_host(docker_path)
        return None if host is None else self.to_rel(host)

    def on_cache(self, rel):
        return self.cache / rel

    def on_array(self, rel):
        return self.array / rel

    def on_disk(self, rel):
        """Echte Disk-Pfade (z.B. /mnt/disk3/Filme/...), auf denen die Datei liegt."""
        return [Path(p) for p in sorted(glob.glob(str(Path(self.disks_glob) / rel))) if Path(p).exists()]

    def is_library_root(self, rel):
        return len(rel.parts) <= 1 or rel in self.library_roots

    def list_files(self, rel_dir, recursive):
        """Dateien unter rel_dir – Union aus Array-Sicht und Cache (ersetzt den FUSE-Blick über /mnt/user)."""
        found = set()
        for root in (self.array, self.cache):
            base = root / rel_dir
            if not base.is_dir():
                continue
            it = base.rglob("*") if recursive else base.iterdir()
            for f in it:
                if f.is_file():
                    found.add(f.relative_to(root))
        return sorted(found)


def remove_empty_parents(path, stop_root, log, min_depth=2):
    """Löscht leere Elternordner von path bis (exklusiv) stop_root; Share-Wurzel (Tiefe 1) bleibt immer stehen."""
    p = Path(path)
    while True:
        try:
            rel = p.relative_to(stop_root)
        except ValueError:
            return
        if len(rel.parts) < min_depth or not p.is_dir():
            return
        try:
            next(p.iterdir())
            return  # nicht leer
        except StopIteration:
            pass
        try:
            p.rmdir()
            log.info(f"Leeren Ordner entfernt: {p}")
        except OSError as e:
            log.warning(f"Leerer Ordner konnte nicht entfernt werden: {p} ({e})")
            return
        p = p.parent


def free_percent_after(path, size, simulated_delta=0):
    """Freiplatz in Prozent, nachdem `size` Bytes geschrieben wären (bei ZFS-Quota zählt die Quota).
    simulated_delta: im Dry-Run der noch nicht ausgeführte Platzgewinn (Cleanup) minus geplante Kopien,
    damit der Dry-Run denselben Füllstand sieht wie der scharfe Lauf an dieser Stelle."""
    u = shutil.disk_usage(path)
    if u.total == 0:
        return 0.0
    return max(0.0, (u.free + simulated_delta - size) / u.total * 100.0)


# --------------------------------------------------------------------------- Mover
def _read_share_cfg(path):
    """Gibt {key: value} für shareUseCache/shareCachePool/shareCachePool2 zurück oder None, wenn nicht lesbar."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        for key in ("shareUseCache", "shareCachePool", "shareCachePool2"):
            if line.startswith(key + "="):
                vals[key] = line.split("=", 1)[1].strip().strip('"')
    return vals


def share_mover_mode(cfg, share):
    """Mover-Einstellung eines Shares: (mode, primary, secondary, quelle).
    mode = yes | prefer | only | no | None (nichts gefunden). Quelle = Share-Cfg, Standardwerte oder ''."""
    cfg_dir = Path(cfg["shares_cfg_dir"])
    share_cfg = cfg_dir / f"{share}.cfg"
    vals = _read_share_cfg(share_cfg)
    source = str(share_cfg)
    if not vals or "shareUseCache" not in vals:
        defaults = _read_share_cfg(cfg_dir.parent / "share.cfg")  # /boot/config/share.cfg = Standardwerte neuer Shares
        if defaults and "shareUseCache" in defaults:
            vals = defaults
            source = f"Standardwerte {cfg_dir.parent / 'share.cfg'} (keine {share}.cfg vorhanden)"
        else:
            return None, None, None, ""
    mode = (vals.get("shareUseCache") or "").lower() or None
    primary = vals.get("shareCachePool") or "cache"
    secondary = vals.get("shareCachePool2") or ("array" if mode in ("yes", "prefer") else "keines")
    return mode, primary, secondary, source


MOVER_MODE_HINT = {
    "yes": "Mover Cache → Array – Cleanup über das move-Binary funktioniert",
    "prefer": "Mover Array → Cache: das move-Binary schiebt Dateien dieses Shares immer Richtung Cache – "
              "Cleanup Cache → Array ist damit unmöglich (Share auf «Cache: Yes» stellen)",
    "only": "Cache only: das move-Binary kennt kein Array-Ziel für diesen Share – Cleanup unmöglich",
    "no": "Array only: der reguläre Mover fasst den Share nicht an; ob das move-Binary Pool-Pfade trotzdem "
          "aufs Array schiebt, ist ungetestet – ersten Lauf mit mover_debug_level 1 prüfen",
    None: "keine Share-Konfiguration gefunden – Mover-Richtung unbekannt, Cleanup wird versucht",
}


def share_mode_ok(cfg, share, checked, log):
    """Prüft einmal pro Share, ob das move-Binary Cache → Array kann, und schreibt das Ergebnis ins Log.
    Bei cleanup_tool=rsync immer True (rsync nach /mnt/user0 ist von der Mover-Richtung unabhängig)."""
    if share in checked:
        return checked[share]
    if cfg["cleanup_tool"] == "rsync":
        checked[share] = True
        return True
    mode, primary, secondary, source = share_mover_mode(cfg, share)
    where = f"Primary {primary}, Secondary {secondary}; aus {source}" if mode else "keine Konfiguration gefunden"
    hint = MOVER_MODE_HINT.get(mode, f"unbekannter Wert «{mode}»")
    if mode == "yes":
        log.info(f"Share «{share}»: shareUseCache=yes ({where}) – {hint}")
        checked[share] = True
    elif mode is None or mode == "no":
        log.warning(f"Share «{share}»: shareUseCache={mode or '?'} ({where}) – {hint}")
        checked[share] = True
    else:
        log.error(f"Share «{share}»: shareUseCache={mode} ({where}) – {hint}. "
                  f"Cleanup für diesen Share übersprungen (Alternative: cleanup_tool=rsync).")
        checked[share] = False
    return checked[share]


def summarize_mover_output(stdout, paths):
    """Ordnet jeder Datei die Mover-Meldung zu (z.B. 'No space left on device'); {path: message}."""
    result = {}
    for line in stdout.splitlines():
        for p in paths:
            if p in line and p not in result:
                msg = line.split(p, 1)[1].strip(" :-")
                result[p] = msg or line.strip()
    return result


def detect_mover_bin(cfg):
    if cfg.get("mover_bin"):
        return cfg["mover_bin"]
    for cand in MOVER_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


def unraid_mover_running():
    """True, wenn der reguläre Unraid-Mover (mover-Script) gerade läuft."""
    try:
        pid = int(Path(MOVER_PIDFILE).read_text().strip())
    except (OSError, ValueError):
        return False
    return Path(f"/proc/{pid}").exists()


def run_mover(mover_bin, paths, debug_level, log):
    """Pipt Dateipfade ins Unraid move-Binary (Pool -> Array). Liefert (returncode, stdout, stderr)."""
    cmd = [mover_bin]
    if int(debug_level) > 0:
        cmd += ["-d", str(int(debug_level))]
    log.debug(f"Mover-Aufruf: {' '.join(cmd)} mit {len(paths)} Pfaden")
    try:
        proc = subprocess.run(cmd, input="\n".join(paths) + "\n", capture_output=True, text=True)
    except OSError as e:
        log.error(f"move-Binary konnte nicht gestartet werden ({mover_bin}): {e}")
        return 1, "", str(e)
    for line in proc.stdout.splitlines():
        log.debug(f"mover: {line}")
    for line in proc.stderr.splitlines():
        log.debug(f"mover: {line}")
    if proc.returncode != 0:
        log.error(f"move-Binary endete mit Code {proc.returncode}")
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- rsync / Status
_PROGRESS_RE = re.compile(r"^\s*([\d,.']+)\s+(\d+)%\s+(\S+/s)\s+(\d+:\d+:\d+)")


def run_rsync(cmd, on_progress=None):
    """Startet rsync, liest stdout+stderr laufend und meldet --info=progress2-Zeilen an on_progress(bytes, speed, eta).
    Liefert (returncode, Meldungen ohne Fortschrittszeilen)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        return 127, str(e)
    messages = []

    def handle(raw):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            return
        m = _PROGRESS_RE.match(line)
        if m:
            if on_progress:
                on_progress(int(re.sub(r"\D", "", m.group(1)) or 0), m.group(3), m.group(4))
        else:
            messages.append(line)

    buf = b""
    while True:
        chunk = proc.stdout.read1(65536)
        if not chunk:
            break
        parts = re.split(rb"[\r\n]", buf + chunk)
        buf = parts.pop()
        for part in parts:
            handle(part)
    handle(buf)
    return proc.wait(), "\n".join(messages)


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Status:
    """Live-Status des Laufs: schreibt embycache_status.json (atomar, höchstens alle 2 s) und auf Wunsch
    periodisch eine Fortschrittszeile ins Log. Thread-sicher (Kopien laufen parallel)."""

    def __init__(self, mode, log, enabled=True, log_interval=60):
        self.log = log
        self.enabled = enabled
        self.log_interval = log_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._dirty = True
        self._last_log = time.time()
        self.data = {"pid": os.getpid(), "version": __version__, "mode": mode, "state": "läuft",
                     "started": time.time(), "updated": time.time(), "phase": "Start", "phase_started": time.time(),
                     "files_total": 0, "bytes_total": 0, "files_done": 0, "files_failed": 0, "bytes_done": 0,
                     "jobs": {}, "recent": []}
        if self.enabled:
            self._write()
            self._thread = threading.Thread(target=self._ticker, name="status", daemon=True)
            self._thread.start()

    def phase(self, name, files=0, size=0):
        with self._lock:
            self.data.update(phase=name, phase_started=time.time(), files_total=files, bytes_total=size,
                             files_done=0, files_failed=0, bytes_done=0, jobs={})
            self._dirty = True
            self._last_log = time.time()

    def job_start(self, key, rel, disk, size, index):
        with self._lock:
            self.data["jobs"][key] = {"rel": str(rel), "disk": disk, "size": size, "done": 0, "speed": "",
                                      "eta": "", "started": time.time(), "index": index}
            self._dirty = True

    def job_progress(self, key, done, speed="", eta=""):
        with self._lock:
            job = self.data["jobs"].get(key)
            if job:
                job.update(done=min(done, job["size"]) if job["size"] else done, speed=speed, eta=eta)
                self._dirty = True

    def job_end(self, key, ok):
        with self._lock:
            job = self.data["jobs"].pop(key, None)
            if not job:
                return
            if ok:
                self.data["files_done"] += 1
                self.data["bytes_done"] += job["size"]
            else:
                self.data["files_failed"] += 1
            self.data["recent"] = ([{"rel": job["rel"], "disk": job["disk"], "size": job["size"], "ok": ok,
                                     "seconds": round(time.time() - job["started"], 1)}] + self.data["recent"])[:10]
            self._dirty = True

    def finish(self, state="fertig"):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self.data.update(state=state, phase="Ende", jobs={}, finished=time.time())
            self._dirty = True
            if self.enabled:
                self._write()

    def summary(self):
        """Eine Zeile für das Log: Phase, Dateien, Bytes, aktive Kopien."""
        d = self.data
        done = d["bytes_done"] + sum(j["done"] for j in d["jobs"].values())
        pct = f" ({done / d['bytes_total'] * 100:.0f} %)" if d["bytes_total"] else ""
        elapsed = time.time() - d["phase_started"]
        rate = done / elapsed if elapsed > 0 else 0
        eta = f", Rest ca. {_fmt_duration((d['bytes_total'] - done) / rate)}" if rate > 0 and d["bytes_total"] > done else ""
        active = ", ".join(f"{j['disk']}: {Path(j['rel']).name} {j['done'] * 100 // j['size'] if j['size'] else 0} %"
                           for j in d["jobs"].values())
        return (f"Fortschritt {d['phase']}: {d['files_done']}/{d['files_total']} Dateien, {human(done)} von "
                f"{human(d['bytes_total'])}{pct}, {human(rate)}/s{eta}"
                + (f", {d['files_failed']} Fehler" if d["files_failed"] else "")
                + (f" | aktiv ({len(d['jobs'])}): {active}" if active else ""))

    def _ticker(self):
        while not self._stop.wait(2):
            with self._lock:
                if self._dirty:
                    self._write()
                if self.log_interval and self.data["jobs"] and time.time() - self._last_log >= self.log_interval:
                    self._last_log = time.time()
                    self.log.info(self.summary())

    def _write(self):
        """Nur unter self._lock bzw. vor dem Start des Tickers aufrufen."""
        self.data["updated"] = time.time()
        self._dirty = False
        try:
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
        except OSError as e:
            self.log.debug(f"Status-Datei nicht schreibbar ({STATUS_FILE}): {e}")


def format_status():
    """Text für --status aus embycache_status.json."""
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"Kein Status vorhanden ({STATUS_FILE}) – noch kein Lauf mit --run"
    ts = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    alive = Path(f"/proc/{d.get('pid')}").exists()
    state = d.get("state", "?")
    if state == "läuft" and not alive:
        state = "abgebrochen (Prozess existiert nicht mehr)"
    lines = [f"EmbyCache {d.get('version', '')} – Status: {state}, PID {d.get('pid')}, Modus {str(d.get('mode', '')).upper()}",
             f"Start {ts(d['started'])}, letzte Aktualisierung {ts(d['updated'])}"
             + (f", beendet {ts(d['finished'])}" if d.get("finished") else "")]
    jobs = d.get("jobs") or {}
    done = d.get("bytes_done", 0) + sum(j.get("done", 0) for j in jobs.values())
    total = d.get("bytes_total", 0)
    lines.append(f"Phase: {d.get('phase')}  –  {d.get('files_done', 0)}/{d.get('files_total', 0)} Dateien, "
                 f"{human(done)} von {human(total)}" + (f" ({done / total * 100:.0f} %)" if total else "")
                 + (f", {d['files_failed']} Fehler" if d.get("files_failed") else ""))
    if jobs:
        lines.append(f"Aktiv ({len(jobs)}):")
        for j in sorted(jobs.values(), key=lambda x: x.get("index", 0)):
            pct = j["done"] * 100 // j["size"] if j.get("size") else 0
            lines.append(f"  [{j.get('index', '?')}/{d.get('files_total', 0)}] {j['disk']:<8} {pct:>3} %  "
                         f"{human(j['done']):>10} / {human(j['size']):<10} {j.get('speed') or '':>12}  "
                         f"Rest {j.get('eta') or '?':<8} {j['rel']}")
    if d.get("recent"):
        lines.append("Zuletzt fertig:")
        for r in d["recent"]:
            lines.append(f"  {'ok    ' if r['ok'] else 'FEHLER'} {r['disk']:<8} {human(r['size']):>10}  "
                         f"{r['seconds']:>7.1f} s  {r['rel']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- Lock / Exclude
def acquire_lock(log):
    """Exklusives flock auf embycache.lock; None, wenn bereits ein Lauf aktiv ist."""
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        log.warning(f"Ein anderer EmbyCache-Lauf ist aktiv ({LOCK_FILE}) – Abbruch")
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def read_exclude():
    if not EXCLUDE_FILE.exists():
        return set()
    return {line.strip() for line in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}


def write_exclude(paths):
    """Atomar schreiben (temp + rename), damit Mover Tuning nie eine halbe Liste liest."""
    tmp = EXCLUDE_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(set(paths))) + ("\n" if paths else ""), encoding="utf-8")
    os.replace(tmp, EXCLUDE_FILE)


def collect_sessions(cfg, log):
    """Gerade abgespielte Dateien aller Instanzen: relative Pfade plus Dateinamen (Fallback ohne Mapping).
    Liefert ((rels, names), ok) – ok=False, wenn eine Instanz nicht geantwortet hat."""
    rels, names = set(), set()
    ok = True
    for inst in cfg["instances"]:
        api = EmbyApi(inst["url"], inst["api_key"], cfg["api_timeout"])
        loc = Locations(cfg, inst["_mappings"])
        try:
            sessions = api.get("/Sessions") or []
        except RuntimeError as e:
            log.warning(f"[{inst['servername']}] Sessions nicht abrufbar: {e}")
            ok = False
            continue
        for s in sessions:
            item = s.get("NowPlayingItem") or {}
            p = item.get("Path")
            if not p:
                continue
            names.add(os.path.basename(p))
            rel = loc.rel_from_docker(p)
            if rel is not None:
                rels.add(str(rel))
                log.info(f"[{inst['servername']}] Läuft gerade ({s.get('UserName', '?')}): {rel}")
    return (rels, names), ok


def is_playing(rel, sessions):
    rels, names = sessions
    return str(rel) in rels or Path(rel).name in names
