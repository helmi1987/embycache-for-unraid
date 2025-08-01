import json, os, requests, ntpath, posixpath
from urllib.parse import urlparse

"""
embycache_setup.py – (re‑)Configuration helper for *embycache.py*
=================================================================
*Initial Setup* **oder** komfortables *Update* einer bestehenden
`embycache_settings.json`.

**New in this version (Aug 2025)**
------------------------------------
* Detects existing settings and offers an *update mode*.
  – Existing values are pre-filled in the prompts.
  – Press *Enter* to keep the current value.
* All logic from previous iterations remains intact
  (Benutzer‑Kommentare, Docker path note etc.).
"""

# ─── Basics ───────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
script_folder = os.path.join(ROOT_DIR, "config")  # write settings under repo_root/config
os.makedirs(script_folder, exist_ok=True)
settings_filename  = os.path.join(script_folder, "embycache_settings.json")
settings_data: dict = {}

# ─── Helper ───────────────────────────────────────────────────────────────────

def is_valid_emby_url(url: str) -> bool:
    try:
        return requests.get(url.rstrip("/")+"/System/Info/Public", timeout=5).ok
    except requests.exceptions.RequestException:
        return False

def read_json(fn: str):
    with open(fn, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(fn: str, data: dict):
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def emby_get(base: str, api_key: str, endpoint: str, *, params=None):
    params = params or {}
    params["api_key"] = api_key
    r = requests.get(base.rstrip("/")+endpoint, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def ask(msg: str, default: str|None=None):
    prompt = f"{msg} [{'Enter' if default is not None else ''}{'=' + str(default) if default is not None else ''}]: "
    resp = input(prompt)
    return default if resp == "" and default is not None else resp

def ask_yesno(msg: str, default: bool=False) -> bool:
    dv = "Y/n" if default else "y/N"
    resp = input(f"{msg} [{dv}] ") or ("y" if default else "n")
    return resp.lower().startswith("y")

def ask_int(msg: str, default: int) -> int:
    while True:
        resp = input(f"{msg} [{default}]: ")
        if resp == "":
            return default
        if resp.isdigit():
            return int(resp)
        print("Bitte Zahl eingeben.")

# ─── Setup Routine ────────────────────────────────────────────────────────────

def setup():
    global settings_data
    settings_data.setdefault("firststart", False)

    # 1) URL ------------------------------------------------------------------
    settings_data["EMBY_URL"] = ask("Emby URL", settings_data.get("EMBY_URL","http://localhost:8096")).rstrip("/")
    while not is_valid_emby_url(settings_data["EMBY_URL"]):
        print("URL nicht erreichbar – bitte korrigieren.")
        settings_data["EMBY_URL"] = ask("Emby URL", settings_data["EMBY_URL"]).rstrip("/")

    # 2) API key & Libraryen ----------------------------------------------
    while True:
        settings_data["EMBY_API_KEY"] = ask("Admin‑API key", settings_data.get("EMBY_API_KEY",""))
        try:
            info = emby_get(settings_data["EMBY_URL"], settings_data["EMBY_API_KEY"], "/System/Info")
            print("✓ verbunden mit", info.get("ServerName","Emby"))
            break
        except Exception as e:
            print("Key invalid:", e)

    libs = emby_get(settings_data["EMBY_URL"], settings_data["EMBY_API_KEY"], "/Library/VirtualFolders")
    current_valid = set(settings_data.get("valid_libraries", []))
    valid = []
    emby_folders = settings_data.get("emby_library_folders", [])
    if not emby_folders:
        emby_folders = []
    for lib in libs:
        name = lib["Name"]
        take = ask_yesno(f"Library '{name}' include?", name in current_valid)
        if take:
            valid.append(name)
            for loc in lib.get("Locations", []):
                base = os.path.basename(loc.rstrip("/\\"))
                if base not in emby_folders:
                    emby_folders.append(base)
            if "emby_source" not in settings_data:
                first_loc = lib.get("Locations", [None])[0]
                root = ntpath.splitdrive(first_loc)[0]+ntpath.sep if os.name=='nt' else os.path.dirname(first_loc)
                settings_data["emby_source"] = root
    settings_data["valid_libraries"] = valid
    settings_data["emby_library_folders"] = emby_folders

    # 3) NextUp Parameter ------------------------------------------------------
    settings_data["number_episodes"] = ask_int("Episoden pro Serie", settings_data.get("number_episodes",5))
    settings_data["days_to_monitor"] = ask_int("Max Alter (Tage)", settings_data.get("days_to_monitor",99))

    # 4) Multi‑User -----------------------------------------------------------
    users_toggle = ask_yesno("Consider other users", settings_data.get("users_toggle", False))
    settings_data["users_toggle"] = users_toggle
    skip_users, skip_ondeck = settings_data.get("skip_users", []), settings_data.get("skip_ondeck", [])
    skip_users_info, skip_ondeck_info = settings_data.get("skip_users_info", []), settings_data.get("skip_ondeck_info", [])
    if users_toggle:
        users = emby_get(settings_data["EMBY_URL"], settings_data["EMBY_API_KEY"], "/Users")
        for u in users:
            uid, name = u["Id"], u["Name"]
            if uid in skip_users:
                default_skip = True
            else:
                default_skip = False
            if ask_yesno(f"User '{name}' skip entirely", default_skip):
                if uid not in skip_users:
                    skip_users.append(uid); skip_users_info.append({"id":uid,"name":name})
                if ask_yesno("  Skip NextUp as well", uid in skip_ondeck):
                    if uid not in skip_ondeck:
                        skip_ondeck.append(uid); skip_ondeck_info.append({"id":uid,"name":name})
            else:
                if uid in skip_users:
                    i = skip_users.index(uid)
                    skip_users.pop(i); skip_users_info.pop(i)
                if uid in skip_ondeck:
                    j = skip_ondeck.index(uid)
                    skip_ondeck.pop(j); skip_ondeck_info.pop(j)
    settings_data["skip_users"], settings_data["skip_users_info"] = skip_users, skip_users_info
    settings_data["skip_ondeck"], settings_data["skip_ondeck_info"] = skip_ondeck, skip_ondeck_info

    # 5) Watched‑Move ---------------------------------------------------------
    wm_default = settings_data.get("watched_move", False)
    settings_data["watched_move"] = ask_yesno("Move watched items back", wm_default)
    settings_data["watched_cache_expiry"] = ask_int("Cache Ablauf (h)", settings_data.get("watched_cache_expiry",48))

    # 6) Pathe ---------------------------------------------------------------
    settings_data["cache_dir"] = ask("Cache‑Path (Host)", settings_data.get("cache_dir","/mnt/cache")).rstrip('/\\')
    settings_data["real_source"] = ask("Array‑Path (Host)", settings_data.get("real_source","/mnt/user")).rstrip('/\\')
    nas_map = settings_data.get("nas_library_folders", [])
    if len(nas_map)!=len(emby_folders):
        nas_map = []
        for fld in emby_folders:
            nas_map.append( ask(f"Host folder for '{fld}'", fld).strip('/\\') )
    settings_data["nas_library_folders"] = nas_map

    # 7) Verhalten / parallel / debug ----------------------------------------
    settings_data["exit_if_active_session"] = ask_yesno("Script beenden bei aktiver Wiedergabe", settings_data.get("exit_if_active_session",False))
    settings_data["max_concurrent_moves_cache"] = ask_int("Parallele Moves → Cache", settings_data.get("max_concurrent_moves_cache",5))
    settings_data["max_concurrent_moves_array"] = ask_int("Parallele Moves → Array", settings_data.get("max_concurrent_moves_array",2))
    settings_data["debug"] = ask_yesno("Debug mode (nothing will be moved)", settings_data.get("debug",False))

    write_json(settings_filename, settings_data)
    print("\n✔ Settings saved — you can now run start.py or the individual scripts.")

# ─── Main ─────────────────────────────────────────────────────────────────────
os.makedirs(script_folder, exist_ok=True)
if os.path.exists(settings_filename):
    try:
        settings_data = read_json(settings_filename)
    except json.JSONDecodeError:
        print("⚠️  Settings file is invalid — starting fresh setup.")
        settings_data = {}

    if ask_yesno("Update existing configuration?", False):
        setup()
    else:
        print("No changes made.")
else:
    print("No settings found — starting initial setup…")
    setup()
