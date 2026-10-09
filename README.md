# 🎬 EmbyCache für Unraid

Verschiebt die Medien, die Emby-Benutzer als Nächstes sehen werden («Weiterschauen», die nächsten Folgen, Favoriten-Serien), vom Array auf den Cache-Pool und räumt sie wieder aufs Array, sobald sie nicht mehr gebraucht werden. Die Array-Platten können dadurch schlafen, und die Wiedergabe startet ohne Spin-up.

Emby merkt davon nichts, weil es die Medien über `/mnt/user/...` sieht – egal ob eine Datei gerade auf dem Pool oder auf einer Array-Disk liegt.

## Was das Script macht

| Phase | Aktion | Beschreibung |
| --- | --- | --- |
| 1. Schutz | Sessions | Fragt alle Instanzen, was **gerade läuft**. Diese Dateien werden nie verschoben. |
| 2. Analyse | On-Deck | Pro Benutzer: «Weiterschauen»-Einträge und Embys «Als Nächstes» (NextUp – damit eine Serie zwischen zwei Folgen nicht aus dem Cache fällt), bei Serien die nächsten Folgen **in Serienreihenfolge** (nach der aktuellen Folge), plus die nächsten ungesehenen Folgen von Favoriten-Serien. Umfang nach Zähler (`number_episodes`) oder nach **Budget** (`cache_budget`, siehe unten). |
| 3. Cleanup | Cache → Array | Dateien aus der letzten Exclude-Liste, die nicht mehr on deck sind, gehen zurück aufs Array – über das Unraid-`move`-Binary (`cleanup_tool: mover`, Default wie im Original; setzt Mover-Richtung Cache → Array voraus, das Script prüft und meldet sie) oder per `rsync` nach `/mnt/user0` (`cleanup_tool: rsync`, unabhängig von der Share-Einstellung). **Zuerst**, damit Platz frei wird. |
| 4. Befüllen | Array → Cache | On-Deck-Dateien kommen auf den Pool – per `rsync -aAX --numeric-ids /mnt/user0/… /mnt/<pool>/…` **exakt wie im Original**, Quelle erst gelöscht, wenn rsync mit 0 endet und die Grösse stimmt (`fill_tool: rsync`, Default). Optional liest rsync vom echten `/mnt/diskN`-Pfad (`array_source: disk`) oder die Array-Pfade gehen ans Unraid-`move`-Binary wie beim Stock-Mover für «prefer»-Shares (`fill_tool: mover`). Der Freiplatz wird vorher für alle geplanten Kopien zusammen geprüft. rsync-Kopien laufen auf Wunsch parallel pro Quell-Disk (`parallel_per_disk`, `parallel_total`, Default: eine nach der anderen). |
| 5. Exclude-Liste | `embycache_exclude.txt` | Alle On-Deck-Dateien, die jetzt auf dem Cache liegen – für Mover Tuning («File list path»), damit der reguläre Mover sie in Ruhe lässt. |

Der Dry-Run trifft dieselben Entscheidungen wie der scharfe Lauf: Er schreibt nichts (auch nicht die Exclude-Liste), rechnet aber beim Freiplatz-Check den Platz ein, den der Cleanup freigäbe, und zieht jede geplante Kopie ab – so sieht er den Pool an jeder Stelle so, wie ihn der scharfe Lauf dort sähe.

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
| `embycache_exclude.txt` | Aktuell gecachte Dateien; wird atomar geschrieben (über `embycache_exclude.tmp`) |
| `embycache.lock` | Verhindert parallele Läufe |
| `embycache_status.json` | Live-Status während `--run` (für `--status`); wird atomar geschrieben (über `embycache_status.tmp`) |
| `logs/` | Rotierende Logs (10 MB × 20) |

Alle Pfade sind relativ zum **Script-Verzeichnis**, nicht zum Arbeitsverzeichnis – das Script läuft also auch aus cron/User Scripts ohne `cd`. Mit `EMBYCACHE_DIR` lässt sich das Arbeitsverzeichnis trotzdem umbiegen.

## Konfiguration (`embycache_settings.json`)

```jsonc
{
    "cache_path": "/mnt/cache",            // Pool, auf den gecacht wird (z.B. /mnt/master)
    "array_path": "/mnt/user0",            // Array-Sicht ohne Pools
    "user_path": "/mnt/user",              // Sicht, die Emby gemountet hat
    "array_disks_glob": "/mnt/disk[0-9]*", // echte Array-Disks: Quelle bei array_source: disk, Disk-Zuordnung für Parallelität/Status
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
    "libraries": [],                       // nur informativ (vom Wizard gesetzt); gefiltert wird über path_mappings

    "valid_users": [],                     // User-IDs; leer = alle Benutzer (auch Dict {id: {...}} wird akzeptiert)
    "number_episodes": 3,                  // Zähl-Modus: Folgen nach der aktuellen vorladen
    "cache_budget": "",                    // Budget-Modus: z.B. "2.5T" – leer = Zähl-Modus
    "movie_share_percent": 50,             // Budget-Modus: Anteil Filme am Benutzer-Budget (weich, nach Bytes)
    "max_episodes_per_series": 0,          // Budget-Modus: Folgen pro Serie höchstens (0 = das Budget entscheidet)
    "max_resume_items": 10,                // Weiterschauen-Einträge pro Benutzer (gilt auch als Limit für «Als Nächstes»)
    "max_favorite_series": 10,             // Favoriten-Serien pro Benutzer (0 = aus)
    "use_next_up": true,                   // Embys «Als Nächstes» als Quelle (fertige Folge, nächste noch nicht gestartet)
    "min_free_percent": 20,                // darunter wird nichts mehr kopiert (ZFS: nicht unter 15–20 gehen)
    "movie_mode": "folder",                // folder = ganzer Filmordner, file = nur gleichnamige Dateien
    "create_share_root": false,            // Share-Wurzel auf dem Pool automatisch anlegen (siehe ZFS-Hinweis)
    "mover_bin": "",                       // leer = erstes vorhandene von /usr/libexec/unraid/move, /usr/local/sbin/move, /usr/local/bin/move
    "mover_debug_level": 0,                // -d für das move-Binary (1 = Dateien loggen)
    "rsync_args": ["-aAX", "--numeric-ids"], // vollständige rsync-Optionen, Default = Original
    "fill_tool": "rsync",                  // Array -> Cache: rsync oder mover (Unraid move-Binary, siehe Hinweise)
    "cleanup_tool": "mover",               // Cache -> Array: mover (wie Original) oder rsync (unabhängig von der Mover-Richtung)
    "parallel_per_disk": 1,                // Befüllen per rsync: gleichzeitige Kopien pro Quell-Disk (1 = wie das Original)
    "parallel_total": 1,                   // gleichzeitige Kopien insgesamt (0 = ohne Limit); 1/1 = sequentiell wie das Original
    "copy_progress": true,                 // rsync zusätzlich mit --info=progress2 (nur Ausgabe) – Fortschritt pro Datei
    "status_log_interval": 60,             // Sekunden zwischen Fortschrittszeilen im Log (0 = aus)
    "api_timeout": 10,
    "shares_cfg_dir": "/boot/config/shares" // für die Prüfung der Mover-Richtung pro Share
}
```

### Status und paralleles Kopieren

Während `--run` schreibt das Script laufend `embycache_status.json` (alle 2 s, atomar). Aus einer zweiten Shell:

```
python3 embycache_run.py --status          # einmal anzeigen
python3 embycache_run.py --status --watch  # alle 2 s neu (--watch 5 = alle 5 s), Ende mit Ctrl+C
```

```
EmbyCache 7.3.0 – Status: läuft, PID 930, Modus RUN
Start 2026-10-09 03:00:01, letzte Aktualisierung 2026-10-09 03:01:12
Phase: Befüllen (Array -> Cache)  –  3/6 Dateien, 10.73 GB von 17.17 GB (62 %)
Aktiv (2):
  [3/6] disk1     25 %   732.42 MB / 2.86 GB   180.00MB/s  Rest 0:00:12  Serien/Show1/E3.mkv
  [5/6] disk2     25 %   732.42 MB / 2.86 GB   175.00MB/s  Rest 0:00:13  Serien/Show2/E2.mkv
Zuletzt fertig:
  ok     disk1       2.86 GB     16.0 s  Serien/Show1/E2.mkv
```

Im Log steht pro Datei `[n/N] Start disk3 …` und `[n/N] Fertig … in X s (Y/s)`, dazu alle `status_log_interval` Sekunden eine Zeile «Fortschritt …» mit Gesamtstand, Rate, Restzeit und den aktiven Kopien. Der Fortschritt innerhalb einer Datei kommt aus `--info=progress2` von rsync (reine Ausgabe-Option, ändert nichts am Kopieren; `copy_progress: false` schaltet sie ab, dann zeigt der Status nur ganze Dateien). Endet der Prozess ohne Abschluss, zeigt `--status` «abgebrochen». `--watch` funktioniert nur zusammen mit `--status` (zweite Shell), nicht mit `--run`.

Beim move-Binary (`cleanup_tool: mover`, `fill_tool: mover`) gibt es keine Fortschrittsausgabe. Das Script prüft deshalb alle 2 s, welche Quellen schon weg sind: Log-Zeilen `[n/N] -> ARRAY läuft …` / `… fertig … in X s`, und im Status die aktuelle Datei mit der Grösse, die am Ziel schon sichtbar ist (Cleanup: `/mnt/diskN/<Datei>`, Befüllen: Pool). Die aktuelle Datei ist abgeleitet (das Binary arbeitet die Pfade der Reihe nach ab), nicht vom Binary gemeldet.

**Parallel (nur `fill_tool: rsync`).** Die Dateien werden nach Quell-Disk (`/mnt/diskN`, auch bei `array_source: user0` ermittelt) gruppiert. `parallel_per_disk` = gleichzeitige Kopien pro Disk, `parallel_total` = Obergrenze über alle Disks (0 = keine). Beispiel `parallel_per_disk: 1`, `parallel_total: 4`: bis zu 4 Disks lesen gleichzeitig, jede eine Datei. Der Freiplatz-Check passiert vor dem Start für alle Kopien zusammen (geplante Kopien werden abgezogen). Drei rsync-Fehler in Folge stoppen neue Kopien, laufende werden fertig.

Hinweise:
* Mehr als 1 pro Disk heisst: eine HDD liest zwei Dateien gleichzeitig, der Kopf springt zwischen beiden. Bei HDDs bringt das in der Regel keinen höheren Gesamtdurchsatz, oft weniger. Der Gewinn kommt aus mehreren Disks parallel. Selbst messen: `parallel_per_disk` 1 vs. 2 bei gleichem `parallel_total`.
* Mit `array_source: user0` läuft jede Kopie über shfs (FUSE); `array_source: disk` liest direkt von `/mnt/diskN`.
* `fill_tool: mover` und der Cleanup über das move-Binary bleiben sequentiell (ein Aufruf mit allen Pfaden). Ob mehrere move-Binaries gleichzeitig laufen dürfen, ist von Unraid nicht dokumentiert – deshalb nicht parallelisiert. Der Cleanup per rsync läuft ebenfalls sequentiell (Ziel-Disk wählt shfs, Paritäts-Schreiben ist der Engpass).

### Budget-Modus

Zähler wissen nichts über Dateigrössen: drei Folgen einer SD-Serie sind 1 GB, drei Remux-Folgen 30 GB. Mit `cache_budget` (z.B. `"2.5T"`) rechnet das Script in Bytes:

* Das Budget wird **fair auf die aktiven Benutzer** verteilt (Benutzer ohne «Weiterschauen»/Favoriten zählen nicht). Was ein Benutzer nicht braucht, fliesst automatisch an die anderen (Water-Filling). Einzelnen Benutzern lässt sich ein festes Budget geben: `"valid_users": {"<id>": {"budget": "300G"}, "<id2>": {}}`.
* Innerhalb des Benutzer-Budgets werden **Filme und Serien nach Bytes** im Verhältnis `movie_share_percent` gefüllt – weich: ist eine Kategorie leer, bekommt die andere den Rest. Filme in der Reihenfolge der Weiterschauen-Liste (neueste zuerst).
* Serien werden **reihum** gefüllt: jede laufende Serie und jeder Favorit bekommt pro Runde eine Folge. Kleine Folgen ergeben so automatisch viele Stück, grosse wenige, und keine Serie kann das ganze Budget belegen. `max_episodes_per_series` deckelt zusätzlich (0 = nur das Budget entscheidet).
* Passt ein Eintrag nicht mehr, wird seine Serie gestoppt (nie Folge 5 ohne Folge 4); ein zu grosser Film wird übersprungen, der nächste probiert.
* Was nicht ins Budget passt, bleibt auf dem Array bzw. wird beim nächsten Lauf zurückgeräumt. Das Log zeigt pro Benutzer `Budget <Name>: belegt von zugeteilt (Filme …, Serien …) – nicht im Budget: …`.

Der Freiplatz-Check (`min_free_percent`) prüft das Share-Dataset **und** die Pool-Wurzel und nimmt den kleineren Wert – eine grosszügige Dataset-Quota kann den Pool mit appdata darauf also nicht vollaufen lassen.

Das Budget bezieht sich auf den Inhalt der Exclude-Liste, also auf das, was das Script auf dem Pool hält. `min_free_percent` und eine ZFS-Quota bleiben die harte Grenze darunter.

Filme: Liegt der Film in einem eigenen Ordner, wird der ganze Ordner mitgenommen (Untertitel, nfo, Extras). Liegt er direkt im Bibliotheksordner (flache Ablage), werden nur die Dateien mit gleichem Basisnamen genommen – die Bibliothek wird nie als Ganzes verschoben. Ordner-Items (DVD/BluRay-Struktur) werden als Ordner behandelt.

## Befehle

| Befehl | Wirkung |
| --- | --- |
| `python3 embycache_run.py --show-on-deck` | Report **pro Benutzer**: Filme, dann jede Serie mit ihren Folgen, dann «Nicht im Budget»; je Eintrag Quelle, Speicherort (CACHE/ARRAY/TEILS), Grösse und Dateien |
| `… --show-on-deck --user Benj,Kid` | Report nur für diese Benutzer (Name oder ID). Die Planung läuft immer für alle – der Filter betrifft nur die Anzeige und ist deshalb mit `--run` nicht erlaubt |
| `… --show-on-deck --compact` | Report ohne einzelne Dateien |
| `python3 embycache_run.py` | Dry-Run: alle geplanten Aktionen, keine Dateioperationen |
| `python3 embycache_run.py --run` | Scharf |
| `python3 embycache_run.py --status [--watch [SEK]]` | Live-Status des laufenden bzw. letzten Laufs (aus `embycache_status.json`) |
| `python3 embycache_cleaner.py` | Waisen auf dem Cache anzeigen |
| `python3 embycache_cleaner.py --run` | Waisen aufs Array verschieben – per `cleanup_tool` (mover oder rsync), mit derselben Prüfung der Mover-Richtung wie das Hauptscript |
| `python3 embycache_cleaner.py --add-to-list` | Waisen in die Exclude-Liste aufnehmen |

Umgebungsvariablen: `EMBYCACHE_MODE` (dry / report / run), `EMBYCACHE_DIR`, `EMBYCACHE_CONFIG`, `EMBYCACHE_LOG_LEVEL`, `EMBYCACHE_MIN_FREE_PERCENT`, `EMBYCACHE_MOVER_DEBUG`, `EMBYCACHE_RSYNC_ARGS`, `EMBYCACHE_FILL_TOOL`, `EMBYCACHE_CLEANUP_TOOL`, `EMBYCACHE_CACHE_BUDGET`, `EMBYCACHE_REPORT_USER`, `EMBYCACHE_PARALLEL_PER_DISK`, `EMBYCACHE_PARALLEL_TOTAL`, `EMBYCACHE_STATUS_LOG_INTERVAL`. Der Hilfetext im Kopf jedes Scripts beschreibt sie. `EMBYCACHE_MOVER_DEBUG`, `EMBYCACHE_LOG_LEVEL`, `EMBYCACHE_CLEANUP_TOOL` und `EMBYCACHE_RSYNC_ARGS` gelten auch für `embycache_cleaner.py`.

Beispiel-Report (`--show-on-deck`, mit `--compact` ohne die Dateizeilen):

```
=== Benj @ Nostromo ===  Budget: 312.40 GB von 833.33 GB (Filme 120.10 GB, Serien 192.30 GB)
  Filme: 2 Einträge, 61.70 GB
    • Inception   [CACHE] 60.20 GB, 4 Dateien   (Weiterschauen)
      • [CACHE]   60.10 GB  Filme/Inception (2010)/Inception (2010).mkv
      …
  Serien: 3 Serien, 9 Folgen, 12.70 GB
    • South Park   (Als Nächstes)  4 Folgen, 1.60 GB  [ARRAY]
      • S13E02 The Coon   [ARRAY] 410.00 MB, 1 Datei   (Als Nächstes)
      • S13E03 Margaritaville   [ARRAY] 405.00 MB, 1 Datei   (Nächste Folge)
      …
    • The Expanse   (Weiterschauen)  3 Folgen, 9.10 GB  [TEILS]
      • S03E05 Triple Point   [CACHE] 3.10 GB, 2 Dateien   (Weiterschauen)
      • S03E06 Immolation   [ARRAY] 3.00 GB, 2 Dateien   (Nächste Folge)
    • Dark   (Favorit)  2 Folgen, 2.00 GB  [ARRAY]
  Nicht im Budget: 1 Einträge, 82.00 GB  (bleiben auf dem Array bzw. werden zurückgeräumt)
    ✗ Dune Part Two   [ARRAY] 82.00 GB, 3 Dateien   (Weiterschauen)
```

Pro Benutzer zuerst die Filme, dann jede Serie als Block mit ihren Folgen in Reihenfolge; hinter der Serie steht, warum sie on deck ist (Weiterschauen, Als Nächstes, Favorit), hinter jeder Folge, woher sie kommt. `[TEILS]` heisst: ein Teil liegt schon auf dem Cache.

## Worauf du auf Unraid achten musst

**Share-Einstellung der Medien-Shares – entscheidend.** Das `move`-Binary nimmt die Richtung aus `/boot/config/shares/<Share>.cfg`, nicht aus dem Pfad, den es bekommt. Steht der Share auf **prefer** oder **only** (Primary: Cache, Mover Array → Cache bzw. kein Array), schiebt das Binary Dateien dieses Shares *immer Richtung Cache* – der Cleanup Cache → Array ist damit unmöglich und endet bei vollem Pool mit `No space left on device` und `create_parent: /mnt/cache/…`. Das Script liest vor dem Cleanup pro Share die Konfiguration (`/boot/config/shares/<Share>.cfg`, sonst die Standardwerte aus `/boot/config/share.cfg`), schreibt immer eine Zeile `Share «…»: shareUseCache=… (Primary …, Secondary …; aus …)` ins Log und überspringt Shares, bei denen der Mover nicht Cache → Array kann. Wer die Share-Einstellung nicht ändern will, nimmt `cleanup_tool: rsync`: Das ist das Spiegelbild des Befüllens (`rsync -aAX --numeric-ids /mnt/<pool>/<Datei> /mnt/user0/<Datei>`, shfs wählt die Disk, Grössenvergleich, dann Quelle löschen) und hängt nicht an der Mover-Richtung. Richtig ist *Primary: Cache, Secondary: Array, Mover: Cache → Array* (alt: «Cache: Yes»). Dann schiebt allerdings auch der reguläre Mover die gecachten Dateien nachts zurück – dagegen hilft **Mover Tuning** («File list path» auf `embycache_exclude.txt`) oder der Smart Mover mit `excludes=`. Bei *Array only* (`no`) fasst der reguläre Mover den Share nicht an; ob das Binary Pool-Pfade dann aufs Array schiebt, ist ungetestet – mit einer Datei und `mover_debug_level: 1` prüfen.

**Log lesen.** Dry-Run und Run listen Cleanup und Befüllen pro Serie bzw. Filmordner mit den Dateien darunter (beim Befüllen mit der Quell-Disk). Im scharfen Lauf folgt pro Datei eine Start- und eine Fertig-Zeile `[n/N] …` sowie periodisch eine Zeile «Fortschritt …» (siehe «Status und paralleles Kopieren»). Nach dem Mover fasst das Script zusammen, was liegen blieb und warum (die Meldung des Binaries pro Ursache, z.B. `No space left on device`), und am Ende stehen zwei Ergebniszeilen: «Ergebnis Cleanup: X von Y Dateien aufs Array verschoben» und «Ergebnis Befüllen: X von Y Dateien auf den Cache kopiert». Die rohen Mover-Zeilen stehen im DEBUG-Log.

**ZFS-Pool als Cache.** Ein Share-Ordner, der per `mkdir` auf dem Pool entsteht, ist ein normales Verzeichnis, kein Dataset. Datasets legt Unraid nur über die User-Share-Mechanik an. Lege die Share-Wurzeln deshalb vorher an (`zfs create pool/Filme`), das Script bricht sonst mit einem Hinweis ab (`create_share_root` bleibt bewusst `false`). Eine `zfs set quota=…` auf diese Datasets ist die harte Grenze gegen einen vollen Pool – zusätzlich zu `min_free_percent`, das bei einem Dataset mit Quota gegen die Quota rechnet.

**Kopieren und Mover wie im Original.** Mit den Defaults sind die beiden Aufrufe identisch mit der seit über einem Jahr produktiven Version – bis auf `--info=progress2`, das nur die Fortschrittsausgabe einschaltet (`copy_progress: false` = exakt wie das Original): `rsync -aAX --numeric-ids /mnt/user0/<Datei> /mnt/<pool>/<Datei>` pro Datei, eine nach der anderen, danach `unlink` der Quelle; Cache → Array über `/usr/libexec/unraid/move` (sonst `/usr/local/sbin/move`, `/usr/local/bin/move`) mit den Cache-Pfaden zeilenweise auf stdin, ohne `-d`, solange `mover_debug_level` 0 ist. Neu ist nur, was *nach* dem Aufruf passiert: Exit-Code und stderr werden geloggt, die Grösse wird vor dem Löschen verglichen, und nach dem Mover wird geprüft, was noch auf dem Cache liegt. `EMBYCACHE_LOG_LEVEL=DEBUG` zeigt jeden Aufruf wörtlich.

**Array → Cache über den Unraid-Mover (`fill_tool: mover`).** Der Stock-Mover bringt Dateien von «prefer»-Shares mit `find /mnt/user0/<share> | move` auf den Pool; das Script übergibt dem Binary dieselben Pfade (bevorzugt den echten `/mnt/diskN`-Pfad). Welchen Pool das Binary nimmt, steht in der Share-Konfiguration – für einen Share auf *Array only* gibt es keinen, dann bleibt die Datei liegen und das Script meldet es. Deshalb: mit `mover_debug_level: 1` und einer einzelnen Datei testen, bevor du umstellst. Vorteil gegenüber rsync: fuser-Check und keine doppelte Datei bei Abbruch. Nachteil: kein paralleles Kopieren. Das Werkzeug für den Cleanup (Cache → Array) wählt unabhängig davon `cleanup_tool`.

**Emby-Mounts.** Emby muss die Medien über `/mnt/user/...` sehen (nicht `/mnt/user0` oder `/mnt/diskN`), sonst bricht der transparente Wechsel zwischen Pool und Array.

**Hardlinks.** Sind Medien per Hardlink mit einem Download-Ordner verbunden (*arr-Setups), trennt rsync + löschen den Hardlink; nach dem Zurückschieben liegt die Datei doppelt.

**Duplikate.** Bricht ein Kopiervorgang ab, bleibt die Quelle auf dem Array und rsync räumt seine Temp-Datei weg. Bleibt trotzdem einmal eine Datei doppelt liegen (z.B. Löschen fehlgeschlagen), steht es als ERROR im Log; `embycache_cleaner.py` findet solche Dateien.

## FAQ

**Warum passiert nichts?** Meist fehlt das Path-Mapping: Ohne Übersetzung des Docker-Pfads wird ein Item mit WARNING übersprungen (`--show-on-deck` zeigt es). Prüfe auch `valid_users` – leer heisst alle.

**Wird mein laufender Film unterbrochen?** Nein. Laufende Wiedergaben werden über `/Sessions` erkannt und weder kopiert noch zurückgeschoben. Wenn Sessions nicht abrufbar sind, wird nichts vom Cache entfernt.

**Wo sind die Logs?** `logs/embycache.log` und `logs/embycache_cleaner.log`, mit vollständigen Pfaden. `EMBYCACHE_LOG_LEVEL=DEBUG` zeigt zusätzlich die Mover-Aufrufe.

## Lizenz

Copyright (C) 2025–2026 helmi1987

Dieses Programm ist freie Software: Du kannst es unter den Bedingungen der
GNU General Public License, Version 3, wie von der Free Software Foundation
veröffentlicht, weitergeben und/oder verändern.

Es wird in der Hoffnung verbreitet, dass es nützlich ist, aber **ohne jede
Garantie** – sogar ohne die implizite Garantie der Marktreife oder der Eignung
für einen bestimmten Zweck. Details stehen in der Datei [LICENSE](LICENSE)
(GNU GPL v3, SPDX: `GPL-3.0-or-later`).
