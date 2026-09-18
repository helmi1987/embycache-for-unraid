# 🎬 EmbyCache für Unraid

Verschiebt die Medien, die Emby-Benutzer als Nächstes sehen werden («Weiterschauen», die nächsten Folgen, Favoriten-Serien), vom Array auf den Cache-Pool und räumt sie wieder aufs Array, sobald sie nicht mehr gebraucht werden. Die Array-Platten können dadurch schlafen, und die Wiedergabe startet ohne Spin-up.

Emby merkt davon nichts, weil es die Medien über `/mnt/user/...` sieht – egal ob eine Datei gerade auf dem Pool oder auf einer Array-Disk liegt.

## Was das Script macht

| Phase | Aktion | Beschreibung |
| --- | --- | --- |
| 1. Schutz | Sessions | Fragt alle Instanzen, was **gerade läuft**. Diese Dateien werden nie verschoben. |
| 2. Analyse | On-Deck | Pro Benutzer: «Weiterschauen»-Einträge, bei Serien die nächsten Folgen **in Serienreihenfolge** (nach der aktuellen Folge), plus die nächsten ungesehenen Folgen von Favoriten-Serien. Umfang nach Zähler (`number_episodes`) oder nach **Budget** (`cache_budget`, siehe unten). |
| 3. Cleanup | Cache → Array | Dateien aus der letzten Exclude-Liste, die nicht mehr on deck sind, gehen über das Unraid-`move`-Binary zurück aufs Array. **Zuerst**, damit Platz frei wird. |
| 4. Befüllen | Array → Cache | On-Deck-Dateien kommen auf den Pool – per `rsync -aAX --numeric-ids /mnt/user0/… /mnt/<pool>/…` **exakt wie im Original**, Quelle erst gelöscht, wenn rsync mit 0 endet und die Grösse stimmt (`fill_tool: rsync`, Default). Optional liest rsync vom echten `/mnt/diskN`-Pfad (`array_source: disk`) oder die Array-Pfade gehen ans Unraid-`move`-Binary wie beim Stock-Mover für «prefer»-Shares (`fill_tool: mover`). Vorher wird der Freiplatz geprüft. |
| 5. Exclude-Liste | `embycache_exclude.txt` | Alle On-Deck-Dateien, die jetzt auf dem Cache liegen – für Mover Tuning («File list path»), damit der reguläre Mover sie in Ruhe lässt. |

Ist Emby nicht (vollständig) erreichbar, wird der Cleanup übersprungen und die alte Exclude-Liste bleibt geschützt – sonst würde ein kurzer Emby-Neustart den ganzen Cache leeren.

## Installation

1. Python 3 auf Unraid (z.B. Plugin «Python 3 for UNRAID»). Weitere Pakete braucht es **nicht** – die Scripte nutzen nur die Standardbibliothek.
2. Ordner anlegen, z.B. `/mnt/user/system/scripts/embycache`, und `embycache_lib.py`, `embycache_run.py`, `embycache_setup.py`, `embycache_cleaner.py` hineinkopieren.
3. Wizard ausführen: `python3 embycache_setup.py`
4. Prüfen: `python3 embycache_run.py --show-on-deck` (Report) und `python3 embycache_run.py` (Dry-Run)
5. Scharf schalten, z.B. als User Script stündlich:
   ```
   #!/bin/bash
   cd /mnt/user/system/scripts/embycache && python3 embycache_run.py --run
   ```

## Dateien

| Datei | Zweck |
| --- | --- |
| `embycache_setup.py` | Interaktiver Konfigurator: Instanzen, Bibliotheken, Pfad-Mapping, Benutzer, Systemwerte |
| `embycache_run.py` | Hauptscript (Report / Dry-Run / Run) |
| `embycache_cleaner.py` | Findet Dateien auf dem Cache, die nicht in der Exclude-Liste stehen (Waisen), und räumt sie auf Wunsch weg |
| `embycache_lib.py` | Gemeinsamer Code (Config, Pfade, Emby-API, Mover, Lock, Logging) |
| `embycache_settings.json` | Konfiguration (siehe unten) |
| `embycache_exclude.txt` | Aktuell gecachte Dateien; wird atomar geschrieben |
| `embycache.lock` | Verhindert parallele Läufe |
| `logs/` | Rotierende Logs (10 MB × 20) |

Alle Pfade sind relativ zum **Script-Verzeichnis**, nicht zum Arbeitsverzeichnis – das Script läuft also auch aus cron/User Scripts ohne `cd`. Mit `EMBYCACHE_DIR` lässt sich das Arbeitsverzeichnis trotzdem umbiegen.

## Konfiguration (`embycache_settings.json`)

```jsonc
{
    "cache_path": "/mnt/cache",            // Pool, auf den gecacht wird (z.B. /mnt/master)
    "array_path": "/mnt/user0",            // Array-Sicht ohne Pools
    "user_path": "/mnt/user",              // Sicht, die Emby gemountet hat
    "array_disks_glob": "/mnt/disk[0-9]*", // echte Array-Disks (nur für array_source: disk)
    "array_source": "user0",               // rsync-Quelle: user0 = wie das Original, disk = /mnt/diskN (am FUSE vorbei)

    "instances": [
        {
            "servername": "Nostromo",
            "url": "http://192.168.7.10:8096",
            "api_key": "…",
            "path_mappings": {                 // Docker-Pfad -> Host-Pfad, nur gecachte Bibliotheken
                "/media/Filme":  "/mnt/user/Filme",
                "/media/Serien": "/mnt/user/Serien"
            }
        }
    ],
    "path_mappings": {},                   // optional: globales Mapping für alle Instanzen

    "valid_users": [],                     // User-IDs; leer = alle Benutzer (auch Dict {id: {...}} wird akzeptiert)
    "number_episodes": 3,                  // Zähl-Modus: Folgen nach der aktuellen vorladen
    "cache_budget": "",                    // Budget-Modus: z.B. "2.5T" – leer = Zähl-Modus
    "movie_share_percent": 50,             // Budget-Modus: Anteil Filme am Benutzer-Budget (weich, nach Bytes)
    "max_episodes_per_series": 0,          // Budget-Modus: Folgen pro Serie höchstens (0 = das Budget entscheidet)
    "max_resume_items": 10,                // Weiterschauen-Einträge pro Benutzer
    "max_favorite_series": 10,             // Favoriten-Serien pro Benutzer (0 = aus)
    "min_free_percent": 20,                // darunter wird nichts mehr kopiert (ZFS: nicht unter 15–20 gehen)
    "movie_mode": "folder",                // folder = ganzer Filmordner, file = nur gleichnamige Dateien
    "create_share_root": false,            // Share-Wurzel auf dem Pool automatisch anlegen (siehe ZFS-Hinweis)
    "mover_bin": "",                       // leer = /usr/libexec/unraid/move bzw. /usr/local/sbin/move
    "mover_debug_level": 0,                // -d für das move-Binary (1 = Dateien loggen)
    "rsync_args": ["-aAX", "--numeric-ids"], // vollständige rsync-Optionen, Default = Original
    "fill_tool": "rsync",                  // Array -> Cache: rsync oder mover (Unraid move-Binary, siehe Hinweise)
    "api_timeout": 10
}
```

### Budget-Modus

Zähler wissen nichts über Dateigrössen: drei Folgen einer SD-Serie sind 1 GB, drei Remux-Folgen 30 GB. Mit `cache_budget` (z.B. `"2.5T"`) rechnet das Script in Bytes:

* Das Budget wird **fair auf die aktiven Benutzer** verteilt (Benutzer ohne «Weiterschauen»/Favoriten zählen nicht). Was ein Benutzer nicht braucht, fliesst automatisch an die anderen (Water-Filling). Einzelnen Benutzern lässt sich ein festes Budget geben: `"valid_users": {"<id>": {"budget": "300G"}, "<id2>": {}}`.
* Innerhalb des Benutzer-Budgets werden **Filme und Serien nach Bytes** im Verhältnis `movie_share_percent` gefüllt – weich: ist eine Kategorie leer, bekommt die andere den Rest. Filme in der Reihenfolge der Weiterschauen-Liste (neueste zuerst).
* Serien werden **reihum** gefüllt: jede laufende Serie und jeder Favorit bekommt pro Runde eine Folge. Kleine Folgen ergeben so automatisch viele Stück, grosse wenige, und keine Serie kann das ganze Budget belegen. `max_episodes_per_series` deckelt zusätzlich (0 = nur das Budget entscheidet).
* Passt ein Eintrag nicht mehr, wird seine Serie gestoppt (nie Folge 5 ohne Folge 4); ein zu grosser Film wird übersprungen, der nächste probiert.
* Was nicht ins Budget passt, bleibt auf dem Array bzw. wird beim nächsten Lauf zurückgeräumt. Das Log zeigt pro Benutzer `Budget <Name>: belegt von zugeteilt (Filme …, Serien …) – nicht im Budget: …`.

Das Budget bezieht sich auf den Inhalt der Exclude-Liste, also auf das, was das Script auf dem Pool hält. `min_free_percent` und eine ZFS-Quota bleiben die harte Grenze darunter.

Filme: Liegt der Film in einem eigenen Ordner, wird der ganze Ordner mitgenommen (Untertitel, nfo, Extras). Liegt er direkt im Bibliotheksordner (flache Ablage), werden nur die Dateien mit gleichem Basisnamen genommen – die Bibliothek wird nie als Ganzes verschoben. Ordner-Items (DVD/BluRay-Struktur) werden als Ordner behandelt.

## Befehle

| Befehl | Wirkung |
| --- | --- |
| `python3 embycache_run.py --show-on-deck` | Report: On-Deck-Liste mit Speicherort (CACHE/ARRAY), Grösse und Grund |
| `python3 embycache_run.py` | Dry-Run: alle geplanten Aktionen, keine Dateioperationen |
| `python3 embycache_run.py --run` | Scharf |
| `python3 embycache_cleaner.py` | Waisen auf dem Cache anzeigen |
| `python3 embycache_cleaner.py --run` | Waisen aufs Array verschieben |
| `python3 embycache_cleaner.py --add-to-list` | Waisen in die Exclude-Liste aufnehmen |

Umgebungsvariablen: `EMBYCACHE_MODE` (dry / report / run), `EMBYCACHE_DIR`, `EMBYCACHE_CONFIG`, `EMBYCACHE_LOG_LEVEL`, `EMBYCACHE_MIN_FREE_PERCENT`, `EMBYCACHE_MOVER_DEBUG`, `EMBYCACHE_RSYNC_ARGS`, `EMBYCACHE_FILL_TOOL`, `EMBYCACHE_CACHE_BUDGET`. Der Hilfetext im Kopf jedes Scripts beschreibt sie.

## Worauf du auf Unraid achten musst

**Share-Einstellung der Medien-Shares.** Steht der Share auf *Primary: Cache, Secondary: Array, Mover: Cache → Array* (alt: «Cache: Yes»), schiebt der reguläre Mover die gecachten Dateien nachts wieder zurück. Dann brauchst du **Mover Tuning** mit der Option «File list path» auf `embycache_exclude.txt`. Steht der Share auf *Array only*, fasst der reguläre Mover ihn gar nicht an und die Exclude-Liste ist nur für den Cleanup des Scripts nötig – vorher aber mit einer Datei testen, dass das `move`-Binary den Pool-Pfad trotzdem aufs Array schiebt (`mover_debug_level: 1`).

**ZFS-Pool als Cache.** Ein Share-Ordner, der per `mkdir` auf dem Pool entsteht, ist ein normales Verzeichnis, kein Dataset. Datasets legt Unraid nur über die User-Share-Mechanik an. Lege die Share-Wurzeln deshalb vorher an (`zfs create pool/Filme`), das Script bricht sonst mit einem Hinweis ab (`create_share_root` bleibt bewusst `false`). Eine `zfs set quota=…` auf diese Datasets ist die harte Grenze gegen einen vollen Pool – zusätzlich zu `min_free_percent`, das bei einem Dataset mit Quota gegen die Quota rechnet.

**Kopieren und Mover wie im Original.** Mit den Defaults sind die beiden Aufrufe identisch mit der seit über einem Jahr produktiven Version: `rsync -aAX --numeric-ids /mnt/user0/<Datei> /mnt/<pool>/<Datei>` pro Datei, danach `unlink` der Quelle; Cache → Array über `/usr/libexec/unraid/move` (sonst `/usr/local/sbin/move`, `/usr/local/bin/move`) mit den Cache-Pfaden zeilenweise auf stdin, ohne `-d`, solange `mover_debug_level` 0 ist. Neu ist nur, was *nach* dem Aufruf passiert: Exit-Code und stderr werden geloggt, die Grösse wird vor dem Löschen verglichen, und nach dem Mover wird geprüft, was noch auf dem Cache liegt. `EMBYCACHE_LOG_LEVEL=DEBUG` zeigt jeden Aufruf wörtlich.

**Array → Cache über den Unraid-Mover (`fill_tool: mover`).** Der Stock-Mover bringt Dateien von «prefer»-Shares mit `find /mnt/user0/<share> | move` auf den Pool; das Script übergibt dem Binary dieselben Pfade (bevorzugt den echten `/mnt/diskN`-Pfad). Welchen Pool das Binary nimmt, steht in der Share-Konfiguration – für einen Share auf *Array only* gibt es keinen, dann bleibt die Datei liegen und das Script meldet es. Deshalb: mit `mover_debug_level: 1` und einer einzelnen Datei testen, bevor du umstellst. Vorteil gegenüber rsync: fuser-Check und keine doppelte Datei bei Abbruch; der Cleanup (Cache → Array) läuft immer über das Binary.

**Emby-Mounts.** Emby muss die Medien über `/mnt/user/...` sehen (nicht `/mnt/user0` oder `/mnt/diskN`), sonst bricht der transparente Wechsel zwischen Pool und Array.

**Hardlinks.** Sind Medien per Hardlink mit einem Download-Ordner verbunden (*arr-Setups), trennt rsync + löschen den Hardlink; nach dem Zurückschieben liegt die Datei doppelt.

**Duplikate.** Bricht ein Kopiervorgang ab, bleibt die Quelle auf dem Array und rsync räumt seine Temp-Datei weg. Bleibt trotzdem einmal eine Datei doppelt liegen (z.B. Löschen fehlgeschlagen), steht es als ERROR im Log; `embycache_cleaner.py` findet solche Dateien.

## FAQ

**Warum passiert nichts?** Meist fehlt das Path-Mapping: Ohne Übersetzung des Docker-Pfads wird ein Item mit WARNING übersprungen (`--show-on-deck` zeigt es). Prüfe auch `valid_users` – leer heisst alle.

**Wird mein laufender Film unterbrochen?** Nein. Laufende Wiedergaben werden über `/Sessions` erkannt und weder kopiert noch zurückgeschoben. Wenn Sessions nicht abrufbar sind, wird nichts vom Cache entfernt.

**Wo sind die Logs?** `logs/embycache.log` und `logs/embycache_cleaner.log`, mit vollständigen Pfaden. `EMBYCACHE_LOG_LEVEL=DEBUG` zeigt zusätzlich die Mover-Aufrufe.
