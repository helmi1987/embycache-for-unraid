import json
import requests
import os
from pathlib import Path

# Diese Pfade werden ignoriert
IGNORED_PREFIXES = ["/config", "/metadata", "/transcoding-temp", "/cache", "/logs", "/var", "/boot"]

def ask(prompt, current):
    res = input(f"{prompt} [{current}]: ").strip()
    return res if res else current

def get_emby_data(instances):
    data = {"paths": set(), "libraries": set(), "users": {}}
    for i, inst in enumerate(instances):
        url, key = inst.get("url", "").rstrip('/'), inst.get("api_key", "")
        if not url or not key: continue
        try:
            print(f"   ...lese Instanz {i+1} ({url})...")
            # Bibliotheken
            r = requests.get(f"{url}/Library/VirtualFolders", params={"api_key": key}, timeout=5)
            for lib in r.json():
                data["libraries"].add(lib["Name"])
                for loc in lib.get("Locations", []):
                    # Filter: Systempfade ignorieren
                    if any(loc.startswith(prefix) for prefix in IGNORED_PREFIXES): continue
                    data["paths"].add(loc)
            # User (Nur für interne Logik, Anzeige erfolgt in Schritt 4 neu)
            r_u = requests.get(f"{url}/Users", params={"api_key": key}, timeout=5)
            for u in r_u.json():
                data["users"][u["Id"]] = u["Name"]
        except Exception as e:
            print(f"   WARNUNG Instanz {i+1}: {e}")
    return data

def suggest_mapping(internal_path):
    """Versucht intelligenten Host-Pfad zu raten."""
    # Ersetze gängige Docker-Mount-Points durch /mnt/user
    prefixes = ["/data", "/media", "/mnt"]
    for prefix in prefixes:
        if internal_path.startswith(prefix):
            # z.B. /data/Serien -> /mnt/user/Serien
            # Schneide Prefix ab und hänge Rest an /mnt/user an
            suffix = internal_path[len(prefix):]
            if suffix.startswith("/"): suffix = suffix[1:]
            return f"/mnt/user/{suffix}"
    
    return "/mnt/user" + internal_path

def setup():
    cfg_p = Path("embycache_settings.json")
    cfg = {}
    if cfg_p.exists():
        try: cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        except: cfg = {}

    # Defaults
    cfg.setdefault("instances", [{"url": "http://10.87.100.200:8096", "api_key": ""}])
    cfg.setdefault("path_mappings", {})
    cfg.setdefault("libraries", [])
    cfg.setdefault("valid_users", [])
    cfg.setdefault("cache_path", "/mnt/cache")
    cfg.setdefault("array_path", "/mnt/user0")
    cfg.setdefault("number_episodes", 3)
    cfg.setdefault("min_free_percent", 10)

    print("\n--- 1. Emby Server ---")
    count = int(ask("Anzahl Instanzen", len(cfg["instances"])))
    while len(cfg["instances"]) < count: cfg["instances"].append({"url": "http://IP:8096", "api_key": ""})
    cfg["instances"] = cfg["instances"][:count]
    for i, inst in enumerate(cfg["instances"]):
        print(f"   Server {i+1}:")
        inst["url"] = ask("   URL", inst["url"])
        inst["api_key"] = ask("   API Key", inst["api_key"])

    print("\n--- Lade Daten... ---")
    emby_data = get_emby_data(cfg["instances"])

    print("\n--- 2. Pfad-Mapping (Pro Ordner) ---")
    found_paths = sorted(list(emby_data["paths"]))
    mappings = cfg.get("path_mappings", {})
    
    if found_paths:
        print(f"   Gefunden: {len(found_paths)} Ordner.")
        print("   Bitte bestätige den Host-Pfad für JEDEN Ordner:")
        
        new_mappings = {}
        for p in found_paths:
            # Bestehendes Mapping oder intelligenter Vorschlag
            current = mappings.get(p, suggest_mapping(p))
            val = ask(f"   Host-Pfad für '{p}'", current)
            new_mappings[p] = val
        
        cfg["path_mappings"] = new_mappings
    else:
        print("   (Keine Medien-Pfade gefunden)")

    print("\n--- 3. Bibliotheken ---")
    avail = sorted(list(emby_data["libraries"]))
    print(f"   Gefunden: {', '.join(avail)}")
    cur_libs = ", ".join(cfg["libraries"]) if cfg["libraries"] else ", ".join(avail)
    res = ask("   Welche überwachen? (Komma)", cur_libs)
    cfg["libraries"] = [l.strip() for l in res.split(",") if l.strip()]

    # --- 4. BENUTZER (ANGEPASST MIT SERVER-SPALTE) ---
    print("\n--- 4. Benutzer ---")
    # Header
    print(f"   {'ID':<32} | {'SERVER':<15} | {'NAME'}")
    print("   " + "-" * 70)

    # Wir iterieren hier erneut über die Instanzen, um die Server-Zuordnung für die Anzeige zu haben
    for inst in cfg["instances"]:
        url = inst.get("url", "").rstrip('/')
        key = inst.get("api_key", "")
        if not url or not key: continue

        # Server-Name generieren (aus IP/URL)
        try:
            srv_name = url.replace("http://", "").replace("https://", "").split(":")[0]
            if len(srv_name) > 15: srv_name = srv_name[:13] + ".."
        except: srv_name = "Server"

        try:
            r = requests.get(f"{url}/Users", params={"api_key": key}, timeout=3)
            if r.status_code == 200:
                for u in r.json():
                    print(f"   {u['Id']:<32} | {srv_name:<15} | {u['Name']}")
            else:
                print(f"   {'---':<32} | {srv_name:<15} | (Fehler: {r.status_code})")
        except:
            print(f"   {'---':<32} | {srv_name:<15} | (Offline)")

    print("   " + "-" * 70)
    print("   Hinweis: Mehrere IDs können durch Komma getrennt werden (z.B. id1,id2).")

    cur_usr = ", ".join(cfg["valid_users"])
    res = ask("   User-IDs (Leer = Alle)", cur_usr)
    
    # Input am Komma splitten und säubern
    cfg["valid_users"] = [u.strip() for u in res.split(",") if u.strip()]

    print("\n--- 5. System ---")
    cfg["cache_path"] = ask("   Cache Pfad", cfg["cache_path"])
    cfg["array_path"] = ask("   Array Pfad", cfg["array_path"])
    cfg["number_episodes"] = int(ask("   Anzahl Folgen", cfg["number_episodes"]))
    cfg["min_free_percent"] = int(ask("   Min % Frei", cfg["min_free_percent"]))

    with open(cfg_p, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=4)
    print("\n✔ Fertig.")

if __name__ == "__main__": setup()
