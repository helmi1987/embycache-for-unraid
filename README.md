# 🎬 EmbyCache v6.0 Multi-Server

Intelligentes Caching-System für Unraid: Verschiebt Medien basierend auf Nutzerverhalten von Emby auf die SSD.

🌐Multi-Server Unterstützt mehrere Emby-Instanzen gleichzeitig. Benutzer und Pfade werden pro Server getrennt verwaltet.

🧹Smart Cleanup **Zuerst aufräumen:** Alte Dateien werden erst zurück aufs Array verschoben, um Platz für neue On-Deck Inhalte zu schaffen.

🍿Binge-Ready Erkennt Serien automatisch. Lädt nicht nur die aktuelle, sondern auch die nächsten X Episoden vor.

🛑Active Protection Prüft live via API, ob eine Datei abgespielt wird. Verhindert Verschieben/Löschen während der Wiedergabe.

👤Granulare Kontrolle Limits (Anzahl Filme/Episoden) können pro Benutzer und pro Bibliothek individuell eingestellt werden.

🗺️Path Mapping Übersetzt Docker-Pfade (`/media/...`) zuverlässig in Unraid Host-Pfade (`/mnt/user/...`).

👀On-Deck Report Der Schalter `--show-on-deck` zeigt exakt an, was geplant ist, ohne eine einzige Datei zu bewegen.

📂Auto-Detection Erkennt automatisch Bibliotheks-Typen (Filme vs. Serien) und schlägt passende Einstellungen vor.

Inhaltsverzeichnis

*   [1\. Installation & Setup](#installation)
*   [2\. Die Script-Dateien](#files)
*   [3\. JSON Struktur & Config](#structure)
*   [4\. Logik & Ablauf](#workflow)
*   [5\. Befehle & Ausführung](#commands)
*   [6\. FAQ & Troubleshooting](#faq)

## <a id="installation"></a>1\. Installation & Setup

*   Schritt 1: Vorbereitung Stelle sicher, dass Python 3 und die Library `requests` auf deinem Unraid Server installiert sind.  
    `pip install requests`
*   Schritt 2: Dateien kopieren Erstelle einen Ordner (z.B. `/mnt/user/system/scripts/embycache`) und kopiere `embycache_setup.py` und `embycache_run.py` hinein.
*   Schritt 3: Setup Wizard starten Führe den Konfigurator aus:  
    `python3 embycache_setup.py`  
    Folge den Anweisungen, um Server, API-Keys und Pfade zu definieren.
*   Schritt 4: Testlauf Prüfe die Konfiguration mit dem Report-Modus:  
    `python3 embycache_run.py --show-on-deck`

## <a id="files"></a>2\. Die Script-Dateien

🧙‍♂️

embycache_setup.py Wizard Interaktiver Konfigurator. Liest Server-Daten, erstellt/updatet die JSON und erkennt Bibliotheken.

⚙️

embycache_run.py Core Das Hauptskript. Berechnet On-Deck, schützt aktive Streams, verschiebt Dateien (rsync/mover).

📄

embycache_settings.json Speichert die gesamte Konfiguration (Server, User, Limits, Pfade) in hierarchischer Struktur.

## <a id="structure"></a>3\. JSON Struktur & Config

Die `embycache_settings.json` ist das Herzstück. Hier ein detailliertes Beispiel mit 2 Servern, 2 Usern und unterschiedlichen Bibliothekstypen.
```
{
    // Globale Pfade für Unraid
    "cache_path": "/mnt/cache",
    "user_path": "/mnt/user",
    "array_path": "/mnt/user0",
    "min_free_percent": 40,

    // Liste der Emby-Instanzen
    "instances": [
        {
            "servername": "HomeServer",
            "url": "http://192.168.1.10:8096",
            "api_key": "api_key_home_server_123",
            "path_mappings": {
                "/media/Filme": "/mnt/user/Filme",
                "/media/Serien": "/mnt/user/Serien"
            }
        },
        {
            "servername": "Ferienhaus",
            "url": "http://192.168.1.20:8096",
            "api_key": "api_key_remote_server_456",
            "path_mappings": {
                "/data/Movies": "/mnt/user/Movies",
                "/data/TV": "/mnt/user/TV"
            }
        }
    ],

    // Konfigurierte Benutzer (Key ist die Emby User ID)
    "valid_users": {
        // User 1 auf dem Hauptserver (Mixed Content)
        "a1b2c3d4e5f6...": {
            "username": "Papa",
            "on_server": "HomeServer",
            "libraries": {
                "Action Filme": {
                    "libraries_type": "movies",
                    "max_use_count_on_deck": 5
                },
                "Serien Highlights": {
                    "libraries_type": "serien",
                    "max_use_count_on_deck": 10,
                    "number_episodes": 3
                }
            }
        },
        // User 2 auf dem Remote Server (Nur Serien)
        "9876543210ab...": {
            "username": "Kids",
            "on_server": "Ferienhaus",
            "libraries": {
                "Cartoons": {
                    "libraries_type": "serien",
                    "max_use_count_on_deck": 20,  // Mehr Auswahl für Kinder
                    "number_episodes": 5   // Binge-Faktor hoch
                }
            }
        }
    }
}
```

## <a id="workflow"></a>4\. Logik & Ablauf

Das Skript arbeitet strikt sequenziell, um Datenverlust oder volle Caches zu vermeiden.

| Phase | Aktion | Beschreibung |
| --- | --- | --- |
| **1\.&nbsp;Analyse** | Daten sammeln | Liest für jeden User "Weiterschauen" aus. Berechnet bei Serien zusätzlich die nächsten X Episoden und erstellt eine Liste aller benötigten Dateien (On-Deck). |
| **2\.&nbsp;Schutz** | Session Check | Fragt alle Server ab: Welche Datei wird **jetzt gerade** abgespielt? Diese Pfade landen auf einer internen Blacklist für Verschiebungen. |
| **3\.&nbsp;Cleanup** | Cache -> Array | Dateien, die NICHT mehr auf der neuen On-Deck-Liste stehen (und nicht abgespielt werden), werden vom Cache auf das Array verschoben.  <br>_Ziel: Platz schaffen._ |
| **4\.&nbsp;Move** | Array -> Cache | Die neuen On-Deck-Dateien werden vom Array auf den Cache kopiert (rsync).  <br>_Ziel: Performance._ |

## <a id="commands"></a>5\. Befehle & Ausführung

### Modus: Report (Nur schauen)

Zeigt an, was das Skript tun würde und welche Medien als "On Deck" erkannt werden. Ideal zur Fehlersuche.

`python3 embycache_run.py --show-on-deck`

### Modus: Dry-Run (Simulation)

Standardmodus beim Aufruf ohne Argumente. Berechnet alle Verschiebungen und zeigt Statistiken, führt aber keine Dateioperationen aus.

`python3 embycache_run.py`

### Modus: Real (Scharf)

Führt die Aktionen wirklich durch (rsync & mover) und aktualisiert die `embycache_exclude.txt`.

`python3 embycache_run.py --run`

## <a id="faq"></a>6\. FAQ & Troubleshooting

⚠️ Wichtiger Hinweis zum Mover

Das Skript nutzt den Unraid Mover für den Cleanup (Cache -> Array). Damit das funktioniert, muss die Datei `embycache_exclude.txt` korrekt geschrieben werden. Tools wie das "Mover Tuning Plugin" können diese Liste nutzen, um geschützte Dateien auf dem Cache zu ignorieren.

### Warum werden meine Filme nicht verschoben?

Prüfe das **Path Mapping** in der `embycache_settings.json`. Wenn der Docker-Pfad (Emby) nicht in einen gültigen Unraid-Pfad übersetzt werden kann, ignoriert das Skript die Datei sicherheitshalber.

### Wird mein laufender Film unterbrochen?

**Nein.** Die Active-Protection-Logik erkennt laufende Streams und überspringt diese Dateien bei jeglichen Verschiebe-Aktionen.

### Wo finde ich Logs?

Ausführliche Logs (mit kompletten Pfaden) liegen in `logs/embycache.log`. Die Konsolenausgabe ist bewusst minimal gehalten.

## 7\. Dateistruktur

Die folgenden Dateien befinden sich im Skript-Verzeichnis:

*   `embycache_setup.py`: Der Konfigurations-Wizard.
*   `embycache_run.py`: Das Hauptskript.
*   `embycache_settings.json`: Speichert Server, User und Limits.
*   `embycache_exclude.txt`: Liste der Dateien, die aktuell auf dem Cache liegen (wird vom Mover-Tuning Plugin genutzt, um diese Dateien NICHT zu verschieben).
*   `logs/`: Ordner für Rotierende Logs (Max 20 Dateien à 10MB).

EmbyCache | Version 6.0 Multi-Instance
