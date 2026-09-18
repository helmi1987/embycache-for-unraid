#!/usr/bin/env python3
"""
EmbyCache Cleaner – findet Dateien auf dem Cache in den gecachten Bibliotheksordnern, die NICHT in der
Exclude-Liste stehen (Waisen: z.B. nach einem abgebrochenen Lauf oder manuell kopiert), und schiebt sie
auf Wunsch per Unraid move-Binary aufs Array oder nimmt sie in die Exclude-Liste auf.

Aufruf:
  python3 embycache_cleaner.py                Dry-Run: listet die Waisen
  python3 embycache_cleaner.py --run          Waisen aufs Array verschieben (move-Binary)
  python3 embycache_cleaner.py --add-to-list  Waisen in die Exclude-Liste aufnehmen (bleiben auf dem Cache)

Umgebungsvariablen: EMBYCACHE_DIR, EMBYCACHE_CONFIG, EMBYCACHE_LOG_LEVEL, EMBYCACHE_MOVER_DEBUG (siehe embycache_lib.py)
Gerade abgespielte Dateien werden nie verschoben.
"""
import argparse
import sys
from pathlib import Path

from embycache_lib import (
    ConfigError, Locations, acquire_lock, collect_sessions, detect_mover_bin, human, is_playing, load_config,
    read_exclude, remove_empty_parents, run_mover, setup_logging, write_exclude,
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
            mover = detect_mover_bin(cfg)
            if not mover:
                log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen)")
                return 1
            run_mover(mover, [str(f) for f in orphans], cfg["mover_debug_level"], log)
            left = [f for f in orphans if f.exists()]
            for f in left:
                log.warning(f"Noch auf dem Cache: {f}")
            for f in orphans:
                if not f.exists():
                    remove_empty_parents(f.parent, Path(cfg["cache_path"]), log)
            log.info(f"Verschoben: {len(orphans) - len(left)} von {len(orphans)} Dateien")
        return 0
    except Exception:
        log.exception("Unerwarteter Fehler")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
