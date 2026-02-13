#!/usr/bin/env python3
import os, sys, json, logging, subprocess, argparse, shutil, time
from pathlib import Path
from datetime import datetime

# Logging Setup
LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "embycache.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("EmbyCache")

class EmbyCache:
    def __init__(self, run_mode=False):
        self.run_mode = run_mode
        self.config = self.load_config()
        self.exclude_file = Path("embycache_exclude.txt")
        self.origin_file = Path("embycache_origin.json")
        self.stats = {"to_cache_bytes": 0, "to_array_bytes": 0}
        self.previous_exclude_list = self.load_previous_exclude()
        self.origin_data = self.load_origin_data()
        self.mover_bin = self.detect_mover_bin()

    def load_config(self):
        p = Path("embycache_settings.json")
        if not p.exists():
            log.error("Config fehlt!"); sys.exit(1)
        return json.loads(p.read_text(encoding="utf-8"))

    def load_previous_exclude(self):
        if not self.exclude_file.exists(): return set()
        try:
            lines = self.exclude_file.read_text(encoding="utf-8").splitlines()
            return set(line.strip() for line in lines if line.strip())
        except: return set()

    def load_origin_data(self):
        if not self.origin_file.exists(): return {}
        try: return json.loads(self.origin_file.read_text(encoding="utf-8"))
        except: return {}

    def detect_mover_bin(self):
        if os.path.exists("/usr/libexec/unraid/move"):
            return "/usr/libexec/unraid/move"
        return "/usr/local/bin/move"

    def format_size(self, size):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024: return f"{size:.2f} {unit}"
            size /= 1024

    def is_protected_root_folder(self, folder_path, base_path):
        """
        Entspricht der Logik 'find base_path -maxdepth 1'.
        Prüft, ob ein Ordner ein direkter Unterordner des Basis-Pfads ist (z.B. 'Filme').
        Wenn ja, ist er geschützt.
        """
        try:
            # Berechne den relativen Pfad zur Basis
            # Bsp: folder=/mnt/cache/Filme, base=/mnt/cache -> rel=Filme
            rel = Path(folder_path).relative_to(base_path)
            
            # Wenn der relative Pfad 0 Teile hat (es ist der Basis-Ordner selbst)
            # oder nur 1 Teil hat (es ist ein direkter Unterordner wie 'Filme'), ist es geschützt.
            if len(rel.parts) <= 1:
                return True
        except ValueError:
            # Pfad liegt gar nicht im Basis-Ordner (sollte nicht passieren, aber sicher ist sicher)
            pass
        return False

    def clean_empty_dirs(self):
        # Reinigt leere Ordner auf dem ARRAY
        if not self.run_mode: return
        
        base_path = self.config["array_path"] # z.B. /mnt/user0
        
        # Explizite Schutzliste aus Config Mappings
        protected = set()
        for _, host_p in self.config.get("path_mappings", {}).items():
            if host_p.startswith("/mnt/user"):
                suffix = host_p.replace("/mnt/user", "")
                protected.add(str(Path(base_path) / suffix.strip("/")))
        
        if not os.path.exists(base_path): return

        for root, dirs, files in os.walk(base_path, topdown=False):
            if root == base_path: continue
            if root in protected: continue

            # SCHUTZ (Maxdepth 1 Logik)
            if self.is_protected_root_folder(root, base_path):
                continue
            
            if not dirs and not files:
                try: os.rmdir(root)
                except: pass

    def cleanup_moved_source_dirs(self, file_list):
        """Reinigt leere Ordner auf dem CACHE mit striktem Root-Schutz."""
        if not file_list: return
        
        cache_base = self.config["cache_path"] # z.B. /mnt/cache

        # 1. Ermittle alle betroffenen Eltern-Ordner auf dem Cache
        dirs_to_check = set()
        for file_path in file_list:
            parent_dir = os.path.dirname(file_path)
            if os.path.exists(parent_dir):
                dirs_to_check.add(parent_dir)

        # 2. Sortiere nach Länge absteigend (tiefste zuerst löschen)
        sorted_dirs = sorted(list(dirs_to_check), key=len, reverse=True)

        if not sorted_dirs: return
        
        log.info(f"Prüfe {len(sorted_dirs)} Quell-Ordner auf dem Cache auf Leere...")
        
        cleaned_count = 0
        for d in sorted_dirs:
            # SCHUTZ: Entspricht 'find /mnt/cache -maxdepth 1'
            # Wenn es ein Hauptordner ist (z.B. /mnt/cache/Serien), ÜBERSPRINGEN.
            if self.is_protected_root_folder(d, cache_base):
                log.info(f"Behalte geschützten Hauptordner: {d}")
                continue

            try:
                os.rmdir(d)
                log.info(f"Leerordner gelöscht: {d}")
                cleaned_count += 1
                
                # Rekursiv nach oben prüfen
                parent = os.path.dirname(d)
                # Auch beim Parent prüfen: Ist es ein geschützter Root? Wenn nein, versuche löschen.
                if not self.is_protected_root_folder(parent, cache_base):
                    try:
                        os.rmdir(parent)
                        log.info(f"Eltern-Ordner auch gelöscht: {parent}")
                    except OSError: pass
            except OSError:
                pass # Nicht leer
                
        if cleaned_count > 0:
            log.info(f"{cleaned_count} leere Verzeichnisse vom Cache entfernt.")

    def get_host_path(self, docker_path):
        mappings = self.config.get("path_mappings", {})
        best_match = ""; replacement = ""
        for d_path, h_path in mappings.items():
            if docker_path.startswith(d_path):
                if len(d_path) > len(best_match):
                    best_match = d_path; replacement = h_path
        if best_match: return docker_path.replace(best_match, replacement, 1)
        return docker_path

    def get_files_to_move(self, emby_item):
        raw_path = emby_item.get("Path", "")
        if not raw_path: return []
        host_path_str = self.get_host_path(raw_path)
        video_path = Path(host_path_str)
        
        if not video_path.exists():
            if "/mnt/user" in host_path_str:
                cache_try = host_path_str.replace("/mnt/user", self.config["cache_path"], 1)
                if Path(cache_try).exists(): video_path = Path(cache_try)
            if not video_path.exists(): return []

        files = []
        if emby_item.get("Type") == "Movie":
            for f in video_path.parent.rglob("*"):
                if f.is_file(): files.append(f)
        else:
            base_name = video_path.stem
            for f in video_path.parent.iterdir():
                if f.is_file() and f.name.startswith(base_name): files.append(f)
        return files

    def find_real_disk_path(self, user_share_path):
        try:
            p = Path(user_share_path)
            rel_path = ""
            if "/mnt/user0" in str(p): rel_path = str(p).split("/mnt/user0")[1].lstrip("/")
            elif "/mnt/user" in str(p): rel_path = str(p).split("/mnt/user")[1].lstrip("/")
            if not rel_path: return user_share_path 
            for i in range(1, 31):
                disk_candidate = Path(f"/mnt/disk{i}") / rel_path
                if disk_candidate.exists(): return str(disk_candidate)
        except: pass
        return user_share_path

    def get_resume_items_safe(self, inst, uid, limit):
        import requests
        items = []
        try:
            r = requests.get(f"{inst['url']}/Users/{uid}/Items/Resume", 
                             params={"api_key": inst['api_key'], "Limit": 100, "Fields": "Path,Type,SeriesId,DatePlayed,UserData,ParentIndexNumber,IndexNumber,SeriesName", "MediaTypes": "Video"}, timeout=10)
            if r.status_code == 200: items = r.json().get("Items", [])
        except: pass

        if not items:
            try:
                r = requests.get(f"{inst['url']}/Users/{uid}/Items", 
                                 params={"api_key": inst['api_key'], "Recursive": "true", "Filters": "IsResumable", "Limit": 100, "Fields": "Path,Type,SeriesId,DatePlayed,UserData,ParentIndexNumber,IndexNumber,SeriesName", "MediaTypes": "Video"}, timeout=10)
                if r.status_code == 200: items = r.json().get("Items", [])
            except: pass

        if not items: return []

        def get_sort_date(x):
            try: return x.get("UserData", {}).get("LastPlayedDate") or x.get("DatePlayed") or "1900-01-01"
            except: return "1900-01-01"

        items.sort(key=get_sort_date, reverse=True)
        safe_limit = max(5, limit * 2) 
        return items[:safe_limit]

    def get_next_episodes_for_series(self, inst, uid, series_id, limit, series_name="Unknown"):
        import requests
        try:
            params_eps = {
                "api_key": inst['api_key'], "ParentId": series_id, "Recursive": "true", "IncludeItemTypes": "Episode", 
                "IsPlayed": "false", "Limit": limit, "Fields": "Path,ParentIndexNumber,IndexNumber", 
                "SortBy": "ParentIndexNumber,IndexNumber", "SortOrder": "Ascending" 
            }
            r = requests.get(f"{inst['url']}/Users/{uid}/Items", params=params_eps, timeout=10)
            if r.status_code == 200: return r.json().get("Items", [])
        except: pass
        return []

    def get_on_deck_extended(self):
        import requests
        all_files_with_prio = [] 
        seen_paths = set()
        cfg_num = self.config.get("number_episodes", 3)
        valid_users = self.config.get("valid_users", [])
        
        for inst in self.config["instances"]:
            target_uids = valid_users
            if not target_uids:
                try:
                    r = requests.get(f"{inst['url']}/Users", params={"api_key": inst['api_key']}, timeout=5)
                    target_uids = [u["Id"] for u in r.json()]
                except Exception as e:
                    log.error(f"Fehler User-Liste {inst['url']}: {e}")
                    continue

            for uid in target_uids:
                resume_items = self.get_resume_items_safe(inst, uid, cfg_num) 
                for item in resume_items:
                    for f in self.get_files_to_move(item):
                        if f not in seen_paths:
                            all_files_with_prio.append((1, f, "RESUME"))
                            seen_paths.add(f)
                    
                    if item.get("Type") == "Episode" and "SeriesId" in item:
                        s_name = item.get("SeriesName", "Unknown Series")
                        next_episodes = self.get_next_episodes_for_series(inst, uid, item["SeriesId"], cfg_num, s_name)
                        for next_ep in next_episodes:
                            for f in self.get_files_to_move(next_ep):
                                if f not in seen_paths:
                                    all_files_with_prio.append((1, f, "RESUME-NEXT"))
                                    seen_paths.add(f)

                try:
                    params_fav = {"api_key": inst['api_key'], "IncludeItemTypes": "Series", "Filters": "IsFavorite", "Recursive": "true", "Limit": 50}
                    r_fav = requests.get(f"{inst['url']}/Users/{uid}/Items", params=params_fav, timeout=10)
                    if r_fav.status_code == 200:
                        for series in r_fav.json().get("Items", []):
                            next_eps = self.get_next_episodes_for_series(inst, uid, series["Id"], cfg_num, series.get("Name"))
                            for ep in next_eps:
                                for f in self.get_files_to_move(ep):
                                    if f not in seen_paths:
                                        all_files_with_prio.append((2, f, "FAVORIT"))
                                        seen_paths.add(f)
                except Exception as e: log.error(f"Favoriten Fehler User {uid}: {e}")
        
        all_files_with_prio.sort(key=lambda x: (x[0], str(x[1])))
        return all_files_with_prio

    def get_active_sessions(self):
        import requests
        playing = set()
        for inst in self.config["instances"]:
            try:
                r = requests.get(f"{inst['url']}/Sessions", params={"api_key": inst['api_key']}, timeout=5)
                for s in r.json():
                    if "NowPlayingItem" in s:
                        p = s["NowPlayingItem"].get("Path")
                        if p: playing.add(os.path.basename(p))
            except: pass
        return playing

    def robust_move(self, src, dst):
        src_p = Path(src); dst_p = Path(dst)
        if not dst_p.parent.exists():
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            try:
                src_stat = src_p.parent.stat()
                os.chown(dst_p.parent, src_stat.st_uid, src_stat.st_gid)
                os.chmod(dst_p.parent, src_stat.st_mode)
            except: pass 
        cmd = ["rsync", "-aAX", "--numeric-ids", src, dst]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            if dst_p.exists() and dst_p.stat().st_size == src_p.stat().st_size:
                try: os.remove(src); return True
                except: return False
            else: return False
        else:
            log.error(f"Rsync Fehler: {result.stderr.decode()}")
            return False

    def execute_unraid_mover(self, file_list):
        if not file_list: return
        paths_str = "\n".join(file_list) + "\n"
        
        try:
            process = subprocess.Popen(
                [self.mover_bin], # Standard Mover (leise)
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(input=paths_str)
            
            if process.returncode != 0:
                log.error(f"Unraid Mover Fehler (Code {process.returncode}): {stderr}")
            else:
                log.info("Unraid Mover erfolgreich ausgeführt.")
                self.cleanup_moved_source_dirs(file_list)

        except Exception as e:
            log.error(f"Fehler beim Ausführen des Movers: {e}")

    def run(self):
        import requests
        log.info(f"=== {'RUN' if self.run_mode else 'DRY-RUN'} ===")
        log.info(f"Benutze Mover Binary: {self.mover_bin}")
        
        on_deck_data = self.get_on_deck_extended() 
        sessions = self.get_active_sessions()
        current_on_deck_paths = set()
        
        try:
            stat = shutil.disk_usage(self.config["cache_path"])
            free_pct = (stat.free / stat.total) * 100
        except: free_pct = 0

        # --- PHASE 1: ARRAY -> CACHE ---
        if free_pct > self.config["min_free_percent"]:
            for prio, src_p, reason in on_deck_data:
                src = str(src_p)
                if self.config["libraries"] and not any(l in src for l in self.config["libraries"]): continue
                
                if "/mnt/user" in src:
                    cache_dst = Path(src.replace("/mnt/user", self.config["cache_path"], 1))
                    current_on_deck_paths.add(str(cache_dst))
                    arr_src = Path(src.replace("/mnt/user", self.config["array_path"], 1))

                    if arr_src.exists() and not cache_dst.exists():
                        if src_p.name in sessions:
                            log.info(f"[SKIP: PLAYING] {src_p.name} wird geschaut. Belasse auf Array.")
                            continue

                        size = os.path.getsize(arr_src)
                        self.stats["to_cache_bytes"] += size
                        log.info(f"[PLAN: -> CACHE ({reason})] {src_p.name} ({self.format_size(size)})")
                        
                        real_disk_path = self.find_real_disk_path(str(arr_src))
                        self.origin_data[str(cache_dst)] = real_disk_path

                        if self.run_mode:
                            self.robust_move(str(arr_src), str(cache_dst))
                else:
                    current_on_deck_paths.add(str(src_p))

        # --- PHASE 2: CACHE -> ARRAY (Unraid Mover) ---
        files_to_move_back = []
        
        for old_cache_file_str in self.previous_exclude_list:
            old_cache_p = Path(old_cache_file_str)
            
            if not old_cache_p.exists(): continue
            if str(old_cache_p) in current_on_deck_paths: continue
            
            if old_cache_p.name in sessions:
                log.info(f"[SKIP: PLAYING] {old_cache_p.name} soll zurück, wird aber geschaut.")
                current_on_deck_paths.add(str(old_cache_p)) 
                continue
            
            size = os.path.getsize(old_cache_p)
            self.stats["to_array_bytes"] += size
            log.info(f"[PLAN: -> ARRAY (Gesehen)] {old_cache_p.name} ({self.format_size(size)}) [via Mover]")
            
            files_to_move_back.append(str(old_cache_p))
            
            if self.run_mode:
                self.origin_data.pop(str(old_cache_p), None)

        if self.run_mode and files_to_move_back:
            self.execute_unraid_mover(files_to_move_back)

        self.clean_empty_dirs()
        log.info(f"Statistik: In: {self.format_size(self.stats['to_cache_bytes'])} | Out: {self.format_size(self.stats['to_array_bytes'])}")
        
        if self.run_mode:
            try: 
                self.exclude_file.write_text("\n".join(current_on_deck_paths), encoding="utf-8")
                clean_origin = {k: v for k, v in self.origin_data.items() if k in current_on_deck_paths}
                self.origin_file.write_text(json.dumps(clean_origin, indent=4), encoding="utf-8")
            except: pass
        else:
            log.info("[DRY-RUN] Listen wurden NICHT aktualisiert.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--run", action="store_true"); args = parser.parse_args()
    try: import requests