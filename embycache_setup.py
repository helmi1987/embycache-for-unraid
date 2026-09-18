#!/usr/bin/env python3
"""
EmbyCache – interaktiver Konfigurator. Legt embycache_settings.json an oder aktualisiert sie.

Aufruf:  python3 embycache_setup.py
Ort der Settings: $EMBYCACHE_CONFIG bzw. $EMBYCACHE_DIR/embycache_settings.json (Default: Script-Verzeichnis)

Fragt Emby-Instanzen (URL, API-Key) ab, liest Bibliotheken und Benutzer von jeder Instanz, schlägt Host-Pfade
für die Docker-Pfade vor und schreibt alle Systemwerte (Cache/Array-Pfade, Limits, Freiplatz).
Alle Werte lassen sich später auch direkt in der JSON ändern; Schlüssel und Defaults stehen in embycache_lib.DEFAULTS.
"""
import copy
import json
import sys

from embycache_lib import CONFIG_FILE, DEFAULTS, EmbyApi, save_config

# Emby-interne Pfade, die nie Medien sind
IGNORED_PREFIXES = ("/config", "/metadata", "/transcoding-temp", "/cache", "/logs", "/var", "/boot", "/tmp")


def ask(prompt, current):
    res = input(f"{prompt} [{current}]: ").strip()
    return res if res else str(current)


def ask_int(prompt, current):
    while True:
        val = ask(prompt, current)
        try:
            return int(val)
        except ValueError:
            print("   Bitte eine ganze Zahl eingeben.")


def suggest_mapping(internal_path, user_path):
    """Rät den Host-Pfad: /data/Serien oder /media/Serien -> /mnt/user/Serien."""
    for prefix in ("/data", "/media", "/mnt/user", "/mnt"):
        if internal_path == prefix or internal_path.startswith(prefix + "/"):
            return f"{user_path}/{internal_path[len(prefix):].lstrip('/')}".rstrip("/")
    return f"{user_path}{internal_path}"


def read_instance(inst, timeout):
    """Bibliotheken (Name -> Docker-Pfade) und Benutzer (Id -> Name) einer Instanz."""
    api = EmbyApi(inst["url"], inst["api_key"], timeout)
    libs, users = {}, {}
    for lib in api.get("/Library/VirtualFolders") or []:
        locs = [l for l in lib.get("Locations", []) if not l.startswith(IGNORED_PREFIXES)]
        if locs:
            libs[lib["Name"]] = locs
    for u in api.get("/Users") or []:
        users[u["Id"]] = u.get("Name", u["Id"])
    return libs, users


def setup():
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            print(f"Bestehende Konfiguration geladen: {CONFIG_FILE}")
        except json.JSONDecodeError as e:
            print(f"WARNUNG: {CONFIG_FILE} ist kein gültiges JSON ({e}) – wird neu erstellt.")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, copy.deepcopy(val))

    print("\n--- 1. Emby Server ---")
    if not cfg["instances"]:
        cfg["instances"] = [{"servername": "Emby", "url": "http://IP:8096", "api_key": ""}]
    count = ask_int("Anzahl Instanzen", len(cfg["instances"]))
    while len(cfg["instances"]) < count:
        cfg["instances"].append({"servername": f"Emby{len(cfg['instances']) + 1}", "url": "http://IP:8096", "api_key": ""})
    cfg["instances"] = cfg["instances"][:count]
    for i, inst in enumerate(cfg["instances"], 1):
        print(f"   Server {i}:")
        inst["servername"] = ask("   Name", inst.get("servername", f"Emby{i}"))
        inst["url"] = ask("   URL", inst.get("url", "")).rstrip("/")
        inst["api_key"] = ask("   API Key", inst.get("api_key", ""))

    print("\n--- Lade Daten von den Instanzen ---")
    data = {}
    for inst in cfg["instances"]:
        try:
            data[inst["servername"]] = read_instance(inst, cfg["api_timeout"])
            libs, users = data[inst["servername"]]
            print(f"   {inst['servername']}: {len(libs)} Bibliotheken, {len(users)} Benutzer")
        except RuntimeError as e:
            print(f"   WARNUNG {inst['servername']}: {e}")
            data[inst["servername"]] = ({}, {})

    print("\n--- 2. Bibliotheken ---")
    all_libs = sorted({name for libs, _ in data.values() for name in libs})
    print(f"   Gefunden: {', '.join(all_libs) or '(keine)'}")
    current = ", ".join(cfg["libraries"]) if cfg["libraries"] else ", ".join(all_libs)
    res = ask("   Welche cachen? (Komma-getrennt)", current)
    cfg["libraries"] = [l.strip() for l in res.split(",") if l.strip()]

    print("\n--- 3. Pfad-Mapping (Docker-Pfad -> Host-Pfad) ---")
    print("   Nur Pfade der gewählten Bibliotheken; alles andere fasst das Script nie an.")
    old_global = cfg.get("path_mappings", {})
    for inst in cfg["instances"]:
        libs, _ = data[inst["servername"]]
        paths = sorted({p for name, locs in libs.items() if name in cfg["libraries"] for p in locs})
        if not paths:
            print(f"   {inst['servername']}: keine Medienpfade gefunden")
            continue
        old = dict(old_global)
        old.update(inst.get("path_mappings") or {})
        new = {}
        print(f"   {inst['servername']}:")
        for p in paths:
            new[p] = ask(f"     Host-Pfad für '{p}'", old.get(p, suggest_mapping(p, cfg["user_path"]))).rstrip("/")
        inst["path_mappings"] = new
    cfg["path_mappings"] = {}  # Mappings liegen jetzt pro Instanz

    print("\n--- 4. Benutzer ---")
    print(f"   {'ID':<34} | {'SERVER':<15} | NAME")
    print("   " + "-" * 70)
    for inst in cfg["instances"]:
        _, users = data[inst["servername"]]
        for uid, name in users.items():
            print(f"   {uid:<34} | {inst['servername'][:15]:<15} | {name}")
    print("   " + "-" * 70)
    print("   Mehrere IDs mit Komma trennen. Leer = alle Benutzer aller Instanzen.")
    vu = cfg["valid_users"]
    old_budgets = {k: (v or {}).get("budget", "") for k, v in vu.items()} if isinstance(vu, dict) else {}
    current = ", ".join(vu.keys() if isinstance(vu, dict) else vu)
    res = ask("   User-IDs", current)
    ids = [u.strip() for u in res.split(",") if u.strip()]
    cfg["valid_users"] = ids

    print("\n--- 5. System ---")
    cfg["cache_path"] = ask("   Cache-Pool (z.B. /mnt/cache oder /mnt/master)", cfg["cache_path"]).rstrip("/")
    cfg["array_path"] = ask("   Array-Sicht ohne Pools", cfg["array_path"]).rstrip("/")
    cfg["user_path"] = ask("   User-Share-Sicht", cfg["user_path"]).rstrip("/")
    cfg["array_disks_glob"] = ask("   Glob für die Array-Disks", cfg["array_disks_glob"])
    print("\n--- 6. Umfang: Zähl- oder Budget-Modus ---")
    print("   Budget-Modus: Gesamtgrösse (z.B. 2.5T) wird fair auf die aktiven Benutzer verteilt,")
    print("   Filme/Serien nach Bytes gefüllt, Serien reihum eine Folge pro Runde. Leer = Zähl-Modus.")
    cfg["cache_budget"] = ask("   Cache-Budget gesamt (z.B. 2.5T; '-' = Zähl-Modus)", cfg["cache_budget"] or "-").strip()
    if cfg["cache_budget"] in ("-", "0"):
        cfg["cache_budget"] = ""
    if cfg["cache_budget"]:
        cfg["movie_share_percent"] = ask_int("   Anteil Filme am Benutzer-Budget in % (weich)", int(cfg["movie_share_percent"]))
        cfg["max_episodes_per_series"] = ask_int("   Folgen pro Serie höchstens (0 = das Budget entscheidet)", cfg["max_episodes_per_series"])
        if ids:
            print("   Festes Budget für einzelne Benutzer? Format id=300G, mehrere mit Komma; leer = fair verteilen.")
            current = ", ".join(f"{k}={v}" for k, v in old_budgets.items() if v and k in ids)
            res = ask("   Benutzer-Budgets", current)
            budgets = {}
            for part in res.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() in ids and v.strip():
                        budgets[k.strip()] = v.strip()
            if budgets:
                cfg["valid_users"] = {u: ({"budget": budgets[u]} if u in budgets else {}) for u in ids}
    else:
        cfg["number_episodes"] = ask_int("   Folgen vorladen (nach der aktuellen)", cfg["number_episodes"])
    cfg["max_resume_items"] = ask_int("   Max. Weiterschauen-Einträge pro Benutzer", cfg["max_resume_items"])
    cfg["max_favorite_series"] = ask_int("   Max. Favoriten-Serien pro Benutzer (0 = aus)", cfg["max_favorite_series"])
    cfg["min_free_percent"] = ask_int("   Mindestens frei auf dem Cache in % (ZFS: 20 ist sinnvoll)", cfg["min_free_percent"])
    cfg["movie_mode"] = ask("   Filme: 'folder' = ganzer Filmordner, 'file' = nur gleichnamige Dateien", cfg["movie_mode"])

    save_config(cfg)
    print(f"\n✔ Gespeichert: {CONFIG_FILE}")
    print("  Nächster Schritt: python3 embycache_run.py --show-on-deck")


if __name__ == "__main__":
    try:
        setup()
    except (KeyboardInterrupt, EOFError):
        print("\nAbgebrochen, nichts gespeichert.")
        sys.exit(1)
