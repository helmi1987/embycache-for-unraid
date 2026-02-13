#!/usr/bin/env python3
import os, sys, json, logging, subprocess, argparse
from pathlib import Path

# Logging Setup
LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "embycache_cleaner.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("EmbyCleaner")

class EmbyCacheCleaner:
    def __init__(self, mode):
        self.mode = mode  # 'dry', 'run', 'add'
        self.config = self.load_config()
        self.exclude_file = Path("embycache_exclude.txt")
        self.exclude_list = self.load_exclude_list()
        self.mover_bin = self.detect_mover_bin()

    def load_config(self):
        p = Path("embycache_settings.json")
        if not p.exists():
            log.error("Config fehlt! Bitte erst embycache_setup.py ausführen.")
            sys.exit(1)
        return json.loads(p.read_text(encoding="utf-8"))

    def load_exclude_list(self):
        if not self.exclude_file.exists(): return set()
        try:
            lines = self.exclude_file.read_text(encoding="utf-8").splitlines()
            return set(line.strip() for line in lines if line.strip())
        except: return set()

    def detect_mover_bin(self):
        # Unraid 7 vs 6 Check
        if os.path.exists("/usr/libexec/unraid/move"):
            return "/usr/libexec/unraid/move"
        return "/usr/local/bin/move"

    def scan_cache_for_orphans(self):
        """Durchsucht den Cache nach Dateien, die NICHT in der Exclude-Liste stehen."""
        cache_root = Path(self.config["cache_path"])
        user_root = Path(self.config.get("user_path", "/mnt/user")) # Fallback wenn nicht in Config
        
        orphans = []
        
        # Wir scannen nur die Ordner, die auch in den Path-Mappings definiert sind
        # um nicht versehentlich System-Shares (appdata, domains) zu scannen.
        scan_targets = set()
        for _, host_path in self.config.get("path_mappings", {}).items():
            if str(user_root) in host_path:
                # Aus /mnt/user/Serien wird /mnt/cache/Serien
                rel_path = host_path.replace(str(user_root), "").strip("/")
                target = cache_root / rel_path
                scan_targets.add(target)

        log.info(f"Scanne {len(scan_targets)} Ordner auf dem Cache...")

        for folder in scan_targets:
            if not folder.exists(): continue
            
            for root, dirs, files in os.walk(folder):
                for file in files:
                    file_path = Path(root) / file
                    
                    # Ignoriere unsere eigenen Config-Dateien, falls sie auf dem Cache liegen
                    if "embycache_" in file: continue

                    # Check: Ist die Datei in der Exclude Liste?
                    if str(file_path) not in self.exclude_list:
                        orphans.append(str(file_path))

        return orphans

    def execute_unraid_mover(self, file_list):
        """Pusht die Liste an den Unraid Mover."""
        if not file_list: return
        
        paths_str = "\n".join(file_list) + "\n"
        try:
            log.info(f"Starte Mover für {len(file_list)} Dateien...")
            process = subprocess.Popen(
                [self.mover_bin, "-d", "1"],  # Logging aktiviert
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(input=paths_str)
            
            if process.returncode != 0:
                log.error(f"Mover Fehler: {stderr}")
            else:
                log.info("Mover erfolgreich ausgeführt.")
        except Exception as e:
            log.error(f"Fehler beim Mover-Aufruf: {e}")

    def update_exclude_list(self, new_files):
        """Fügt die gefundenen Dateien zur Exclude-Liste hinzu."""
        try:
            with open(self.exclude_file, "a", encoding="utf-8") as f:
                for path in new_files:
                    f.write(f"{path}\n")
            log.info(f"{len(new_files)} Dateien zur Exclude-Liste hinzugefügt.")
        except Exception as e:
            log.error(f"Fehler beim Schreiben der Exclude-Liste: {e}")

    def run(self):
        orphans = self.scan_cache_for_orphans()
        
        if not orphans:
            log.info("Keine unbekannten Dateien auf dem Cache gefunden. Alles sauber.")
            return

        total_size = sum(os.path.getsize(f) for f in orphans) / (1024*1024*1024)
        log.info(f"Gefunden: {len(orphans)} Dateien ({total_size:.2f} GB) die NICHT in der Exclude-Liste sind.")

        if self.mode == 'dry':
            print("\n--- GEFUNDENE DATEIEN (DRY RUN) ---")
            for f in orphans:
                print(f"[UNBEKANNT] {f}")
            print("-" * 60)
            print("Benutze --run um diese Dateien auf das Array zu verschieben.")
            print("Benutze --add-to-list um diese Dateien in der Exclude-Liste zu behalten.")

        elif self.mode == 'run':
            log.info("Verschiebe Dateien auf das Array (Mover)...")
            self.execute_unraid_mover(orphans)

        elif self.mode == 'add':
            log.info("Füge Dateien zur Exclude-Liste hinzu...")
            self.update_exclude_list(orphans)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EmbyCache Cleaner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--run", action="store_true", help="Verschiebt gefundene Dateien aufs Array")
    group.add_argument("--add-to-list", action="store_true", help="Fügt gefundene Dateien zur Exclude Liste hinzu")
    
    args = parser.parse_args()
    
    mode = 'dry'
    if args.run: mode = 'run'
    if args.add_to_list: mode = 'add'

    EmbyCacheCleaner(mode).run()