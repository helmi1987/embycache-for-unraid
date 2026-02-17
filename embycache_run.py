#!/usr/bin/env python3
import os
import sys
import json
import logging
import subprocess
import argparse
from pathlib import Path
from logging.handlers import RotatingFileHandler

# Logging Setup (Requirement: 10MB, 20 Files, English Logs)
logDir = Path("logs")
logDir.mkdir(exist_ok=True)
logHandler = RotatingFileHandler(logDir / "embycache.log", maxBytes=10*1024*1024, backupCount=20, encoding="utf-8")
logHandler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
log = logging.getLogger("EmbyCache")
log.setLevel(logging.INFO)
log.addHandler(logHandler)
log.addHandler(logging.StreamHandler(sys.stdout))

class EmbyCache:
    def __init__(self, runMode=False):
        self.runMode = runMode
        self.config = self.loadConfig()
        self.excludeFile = Path("embycache_exclude.txt")
        self.previousExcludeList = self.loadPreviousExclude()
        self.moverBin = self.detectMoverBin()
        self.currentOnDeckPaths = set()
        self.toCacheBytes = 0
        self.toArrayBytes = 0

    def loadConfig(self):
        p = Path("embycache_settings.json")
        if not p.exists():
            log.error("Configuration file not found!")
            sys.exit(1)
        return json.loads(p.read_text(encoding="utf-8"))

    def loadPreviousExclude(self):
        if not self.excludeFile.exists(): return set()
        try:
            return set(line.strip() for line in self.excludeFile.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception as e:
            log.error(f"Error loading exclude file: {e}")
            return set()

    def detectMoverBin(self):
        if os.path.exists("/usr/libexec/unraid/move"): return "/usr/libexec/unraid/move"
        return "/usr/local/bin/move"

    def formatSize(self, size):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024: return f"{size:.2f} {unit}"
            size /= 1024

    def isProtectedRoot(self, folderPath, basePath):
        try:
            rel = Path(folderPath).relative_to(basePath)
            return len(rel.parts) <= 1
        except: return True

    def cleanupEmptyDirs(self, folderPath, basePath):
        if not self.runMode: return
        p, base = Path(folderPath), Path(basePath)
        if self.isProtectedRoot(p, base): return
        try:
            if p.exists() and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                log.info(f"Removed empty directory: {p}")
                self.cleanupEmptyDirs(p.parent, base)
        except: pass

    def getHostPath(self, dockerPath):
        mappings = self.config.get("path_mappings", {})
        bestMatch = ""; replacement = ""
        for dPath, hPath in mappings.items():
            if dockerPath.startswith(dPath) and len(dPath) > len(bestMatch):
                bestMatch = dPath; replacement = hPath
        return dockerPath.replace(bestMatch, replacement, 1) if bestMatch else dockerPath

    def getFilesToMove(self, embyItem):
        rawPath = embyItem.get("Path", "")
        if not rawPath: return []
        
        hostPath = Path(self.getHostPath(rawPath))
        try:
            # Safely calculate relative path from /mnt/user/
            relPath = Path(*hostPath.parts[3:]) 
            arrayPath = Path(self.config["array_path"]) / relPath
            cachePath = Path(self.config["cache_path"]) / relPath
            
            actualPath = arrayPath if arrayPath.exists() else (cachePath if cachePath.exists() else None)
            if not actualPath: return []

            files = []
            if embyItem.get("Type") == "Movie":
                for f in actualPath.parent.rglob("*"):
                    if f.is_file(): files.append(f)
            else:
                baseName = actualPath.stem
                for f in actualPath.parent.iterdir():
                    if f.is_file() and f.name.startswith(baseName): files.append(f)
            return files
        except: return []

    def run(self):
        import requests
        log.info(f"=== EmbyCache RUN_MODE: {self.runMode} ===")
        
        sessions = set()
        for inst in self.config["instances"]:
            try:
                r = requests.get(f"{inst['url']}/Sessions", params={"api_key": inst['api_key']}, timeout=5)
                if r.status_code == 200:
                    for s in r.json():
                        if "NowPlayingItem" in s:
                            p = s["NowPlayingItem"].get("Path")
                            if p: sessions.add(os.path.basename(p))
            except: pass

        numEps = self.config.get("number_episodes", 3)
        validUsers = self.config.get("valid_users", [])
        arrayRoot = Path(self.config["array_path"])
        cacheRoot = Path(self.config["cache_path"])

        plannedItems = []
        seenFiles = set()

        # Phase 1: Planning
        for inst in self.config["instances"]:
            for uid in validUsers:
                try:
                    rRes = requests.get(f"{inst['url']}/Users/{uid}/Items/Resume", params={"api_key": inst['api_key'], "Fields": "Path,Type,SeriesId", "MediaTypes": "Video"}, timeout=10)
                    rFav = requests.get(f"{inst['url']}/Users/{uid}/Items", params={"api_key": inst['api_key'], "Recursive": "true", "IncludeItemTypes": "Series", "Filters": "IsFavorite", "Fields": "Path"}, timeout=10)
                    
                    items = rRes.json().get("Items", [])[:numEps*2] if rRes.status_code == 200 else []
                    favs = rFav.json().get("Items", []) if rFav.status_code == 200 else []

                    for item in items + favs:
                        # Process main item
                        for f in self.getFilesToMove(item):
                            if f not in seenFiles: plannedItems.append(f); seenFiles.add(f)
                        # Process next episodes
                        seriesId = item.get("SeriesId") if item.get("Type") == "Episode" else (item.get("Id") if item.get("Type") == "Series" else None)
                        if seriesId:
                            rNext = requests.get(f"{inst['url']}/Users/{uid}/Items", params={"api_key": inst['api_key'], "ParentId": seriesId, "Recursive": "true", "IncludeItemTypes": "Episode", "IsPlayed": "false", "Limit": numEps, "Fields": "Path"}, timeout=5)
                            if rNext.status_code == 200:
                                for ne in rNext.json().get("Items", []):
                                    for f in self.getFilesToMove(ne):
                                        if f not in seenFiles: plannedItems.append(f); seenFiles.add(f)
                except: continue

        # Phase 1: Execution (Array -> Cache)
        for fPath in plannedItems:
            isArray = arrayRoot in fPath.parents
            cPathVersion = cacheRoot / fPath.relative_to(arrayRoot) if isArray else fPath
            self.currentOnDeckPaths.add(str(cPathVersion))

            if isArray and not cPathVersion.exists():
                if fPath.name not in sessions:
                    try:
                        fileSize = fPath.stat().st_size
                        self.toCacheBytes += fileSize
                        log.info(f"[PLAN: -> CACHE] {fPath.name}")
                        if self.runMode:
                            cPathVersion.parent.mkdir(parents=True, exist_ok=True)
                            if subprocess.run(["rsync", "-aAX", "--numeric-ids", str(fPath), str(cPathVersion)], capture_output=True).returncode == 0:
                                fPath.unlink()
                                self.cleanupEmptyDirs(fPath.parent, arrayRoot)
                    except: pass

        # Phase 2: Cleanup (Cache -> Array via Mover)
        filesToMoveBack = []
        for oldFileStr in self.previousExcludeList:
            oldFile = Path(oldFileStr)
            if str(oldFile) in self.currentOnDeckPaths or not oldFile.exists(): continue
            if oldFile.name in sessions:
                self.currentOnDeckPaths.add(str(oldFile)); continue
            
            try:
                self.toArrayBytes += oldFile.stat().st_size
                log.info(f"[PLAN: -> ARRAY] {oldFile.name}")
                filesToMoveBack.append(str(oldFile))
            except: pass

        if self.runMode and filesToMoveBack:
            process = subprocess.Popen([self.moverBin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate(input="\n".join(filesToMoveBack) + "\n")
            # Log mover results for transparency
            for line in stdout.splitlines(): log.info(line)
            for f in filesToMoveBack: self.cleanupEmptyDirs(Path(f).parent, cacheRoot)

        # Final Statistics
        log.info(f"Statistics: In: {self.formatSize(self.toCacheBytes)} | Out: {self.formatSize(self.toArrayBytes)}")
        if self.runMode:
            self.excludeFile.write_text("\n".join(sorted(list(self.currentOnDeckPaths))), encoding="utf-8")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--run", action="store_true"); args = parser.parse_args()
    try:
        import requests
        EmbyCache(runMode=args.run).run()
    except ImportError:
        print("Error: Library 'requests' missing.")
