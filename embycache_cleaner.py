#!/usr/bin/env python3
"""
EmbyCache Cleaner – findet Dateien auf dem Cache in den gecachten Bibliotheksordnern, die NICHT in der
Exclude-Liste stehen (Waisen: z.B. nach einem abgebrochenen Lauf oder manuell kopiert), und schiebt sie
auf Wunsch aufs Array oder nimmt sie in die Exclude-Liste auf. Werkzeug wie im Hauptscript per cleanup_tool:
mover (Default, Unraid move-Binary – Shares mit Mover-Richtung Array -> Cache werden übersprungen) oder rsync.

Aufruf:
  python3 embycache_cleaner.py                Dry-Run: listet die Waisen
  python3 embycache_cleaner.py --run          Waisen aufs Array verschieben (cleanup_tool: mover oder rsync)
  python3 embycache_cleaner.py --add-to-list  Waisen in die Exclude-Liste aufnehmen (bleiben auf dem Cache)

Umgebungsvariablen: EMBYCACHE_DIR, EMBYCACHE_CONFIG, EMBYCACHE_LOG_LEVEL, EMBYCACHE_MOVER_DEBUG, EMBYCACHE_CLEANUP_TOOL,
EMBYCACHE_RSYNC_ARGS (siehe embycache_lib.py bzw. embycache_run.py)
Gerade abgespielte Dateien werden nie verschoben.
"""
import argparse
import sys
from pathlib import Path

from embycache_lib import (
    ConfigError, Locations, acquire_lock, collect_sessions, detect_mover_bin, human, is_playing, load_config,
    read_exclude, remove_empty_parents, run_mover, run_rsync, setup_logging, share_mode_ok, write_exclude,
)

log = setup_logging("EmbyCleaner", "embycache_cleaner.log")


def scan_orphans(cfg, exclude, sessions):
    """Alle Dateien unter <cache>/<Bibliotheks-Wurzel>, die weder in der Exclude-Liste stehen noch laufen."""
    roots = set()
    for inst in cfg["instances"]:
        loc = Locations(cfg, inst["_mappings"])
        roots.update(loc.library_roots)
    cache = Path(cfg["cache_path"])
    orphans, playing = [], []
    log.info(f"Scanne {len(roots)} Bibliotheksordner auf {cache}: {', '.join(map(str, sorted(roots)))}")
    for rel_root in sorted(roots):
        base = cache / rel_root
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.name.startswith("embycache_"):
                continue
            if str(f) in exclude:
                continue
            if is_playing(f.relative_to(cache), sessions):
                playing.append(f)
                continue
            orphans.append(f)
    return orphans, playing


def move_with_rsync(cfg, files):
    """Cache -> Array per rsync nach array_path (shfs wählt die Disk), Quelle erst nach Grössenvergleich löschen."""
    cache, array = Path(cfg["cache_path"]), Path(cfg["array_path"])
    moved = failures = 0
    for i, src in enumerate(files, 1):
        if failures >= 3:
            log.error("Drei rsync-Fehler in Folge – abgebrochen, Ursache im Log prüfen")
            break
        rel = src.relative_to(cache)
        dst = array / rel
        if dst.exists():
            log.warning(f"Ziel existiert schon auf dem Array (Duplikat?), Datei bleibt auf dem Cache: {rel}")
            continue
        log.info(f"[{i}/{len(files)}] -> ARRAY {human(src.stat().st_size):>10}  {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", *cfg["rsync_args"], str(src), str(dst)]
        log.debug("rsync-Aufruf: " + " ".join(cmd))
        rc, out = run_rsync(cmd)
        if rc != 0:
            failures += 1
            log.error(f"rsync-Fehler (Code {rc}) bei {rel}: {out.strip()}")
            continue
        failures = 0
        try:
            if dst.stat().st_size != src.stat().st_size:
                log.error(f"Grösse stimmt nicht überein nach rsync, Datei bleibt auf dem Cache: {rel}")
                continue
            src.unlink()
        except OSError as e:
            log.error(f"Quelle konnte nicht gelöscht werden (Datei liegt jetzt doppelt!): {src} ({e})")
            continue
        moved += 1
        remove_empty_parents(src.parent, cache, log)
    return moved


def main():
    parser = argparse.ArgumentParser(description="EmbyCache Cleaner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run", action="store_true", help="Waisen aufs Array verschieben")
    group.add_argument("--add-to-list", action="store_true", help="Waisen in die Exclude-Liste aufnehmen")
    args = parser.parse_args()
    mode = "run" if args.run else "add" if args.add_to_list else "dry"

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error(str(e))
        return 1
    lock = acquire_lock(log)
    if lock is None:
        return 1
    try:
        exclude = read_exclude()
        sessions, sessions_ok = collect_sessions(cfg, log)
        if not sessions_ok and mode == "run":
            log.error("Sessions nicht von allen Instanzen abrufbar – ohne Wissen über laufende Streams wird nichts verschoben")
            return 1
        orphans, playing = scan_orphans(cfg, exclude, sessions)
        for f in playing:
            log.info(f"[BLEIBT: läuft gerade] {f}")
        if not orphans:
            log.info("Keine unbekannten Dateien auf dem Cache. Alles sauber.")
            return 0
        total = sum(f.stat().st_size for f in orphans)
        log.info(f"Gefunden: {len(orphans)} Dateien ({human(total)}), die nicht in der Exclude-Liste stehen")

        if mode == "dry":
            for f in orphans:
                print(f"[UNBEKANNT] {human(f.stat().st_size):>10}  {f}")
            print("\n--run           verschiebt diese Dateien aufs Array")
            print("--add-to-list   behält sie auf dem Cache und schützt sie in der Exclude-Liste")
        elif mode == "add":
            write_exclude(exclude | {str(f) for f in orphans})
            log.info(f"{len(orphans)} Dateien zur Exclude-Liste hinzugefügt")
        else:
            cache = Path(cfg["cache_path"])
            checked = {}
            moved = 0
            todo = [f for f in orphans if share_mode_ok(cfg, f.relative_to(cache).parts[0], checked, log)]
            if cfg["cleanup_tool"] == "rsync":
                log.info(f"Cleanup-Werkzeug: rsync nach {cfg['array_path']}")
                moved = move_with_rsync(cfg, todo)
            elif todo:
                mover = detect_mover_bin(cfg)
                if not mover:
                    log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen)")
                    return 1
                run_mover(mover, [str(f) for f in todo], cfg["mover_debug_level"], log)
                for f in todo:
                    if not f.exists():
                        moved += 1
                        remove_empty_parents(f.parent, cache, log)
            for f in orphans:
                if f.exists():
                    log.warning(f"Noch auf dem Cache: {f}")
            log.info(f"Verschoben: {moved} von {len(orphans)} Dateien")
        return 0
    except Exception:
        log.exception("Unerwarteter Fehler")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
