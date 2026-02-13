# EmbyCache - Intelligentes Medien-Caching für Unraid

**EmbyCache** ist eine Skript-Sammlung für Unraid, die Medien basierend auf dem Nutzerverhalten von Emby (oder Jellyfin) proaktiv vom Array auf den schnellen Cache (SSD/NVMe) verschiebt. Es sorgt dafür, dass angefangene Filme und die nächsten Episoden einer Serie sofort ohne "Spin-up"-Verzögerung abspielbar sind.

- - -

## 🚀 Features (Version 4.8)

*   **Intelligentes Caching:** Analysiert "Weiterschauen" (Resume) und lädt Inhalte auf den Cache.
*   **Binge-Ready Logik:** Erkennt Serien und lädt automatisch die nächsten _X_ Episoden vor – unterstützt auch tiefe Ordnerstrukturen (z.B. Staffel-Unterordner).
*   **Deep Scan Support:** Robuste Erkennung von Episoden, unabhängig davon, ob die Serie eine flache Struktur hat oder in Staffel-Ordnern organisiert ist (löst Probleme mit Metadaten-Sortierung).
*   **Native Mover Integration:** Nutzt beim Zurückschieben auf das Array die offizielle Unraid Mover Binary (`/usr/libexec/unraid/move` oder `/usr/local/bin/move`), um maximale Kompatibilität mit User Shares und FUSE zu gewährleisten.
*   **Smart Cleanup & Protection:**
    *   Löscht leere Ordner auf dem Cache automatisch, nachdem Dateien verschoben wurden.
    *   **Root Protection:** Schützt Hauptordner (z.B. `Filme`, `Serien`) davor, gelöscht zu werden, selbst wenn sie kurzzeitig leer sind.
*   **Session-Schutz:** Verschiebt keine Dateien, die gerade aktiv abgespielt werden.
*   **Favoriten-Support:** Kann optional auch Favoriten-Serien vorladen.
*   **Mover-Kompatibilität:** Erstellt eine Exclude-Liste, um Konflikte mit dem Standard-Mover zu vermeiden.

- - -

## 📋 Voraussetzungen

*   **Unraid OS** (6.x oder 7.x).
*   **Python 3** (Vorinstalliert oder via NerdTools/Plugin).
*   **Python Library "requests":** Wird für die API-Kommunikation benötigt.

### Installation der Abhängigkeiten

Führe folgenden Befehl im Unraid-Terminal aus:

```
pip install requests
```

- - -

## 🛠️ Installation & Einrichtung

### 1\. Dateien kopieren

Erstelle einen Ordner auf deinem System (z.B. `/mnt/user/system/scripts/embycache`) und lege die folgenden Skripte dort ab:

*   `embycache_setup.py` (Konfigurations-Assistent)
*   `embycache_run.py` (Hauptprogramm)
*   `embycache_cleaner.py` (Optional: Zum Aufräumen von verwaisten Dateien)

### 2\. Konfiguration (Setup)

Starte den interaktiven Einrichtungs-Assistenten. Er führt dich durch alle notwendigen Einstellungen und erstellt die `embycache_settings.json`.

```
python3 embycache_setup.py
```

**Was abgefragt wird:**

*   **Emby Server:** URL (z.B. `http://192.168.1.10:8096`) und API Key.
*   **Pfade:** Zuordnung von Docker-Pfaden zu Unraid-Pfaden (z.B. `/data/Serien` -> `/mnt/user/Serien`).
*   **Benutzer:** Auswahl der Benutzer, deren "Weiterschauen"-Liste überwacht werden soll.
*   **Cache & Array:** Pfade zu deinem Cache-Pool (z.B. `/mnt/cache`) und dem Array (meist `/mnt/user0`).
*   **Limits:** Wie viele Episoden sollen im Voraus geladen werden?

- - -

## ▶️ Verwendung

### Hauptskript (EmbyCache)

**Manueller Test (Dry-Run)**  
Standardmäßig läuft das Skript im Simulations-Modus. Es zeigt im Log an, was es tun würde, verschiebt aber keine Dateien.

```
python3 embycache_run.py
```

**Live-Modus (Dateien verschieben)**  
Um die Dateien wirklich zu bewegen (Array -> Cache via Rsync, Cache -> Array via Mover), muss das Argument `--run` angehängt werden.

```
python3 embycache_run.py --run
```

### Cleaner Skript (Optional)

Dieses Skript hilft, Dateien auf dem Cache zu finden, die nicht in der Exclude-Liste stehen ("Waisen").

*   `python3 embycache_cleaner.py` : Zeigt unbekannte Dateien an (Dry-Run).
*   `python3 embycache_cleaner.py --run` : Verschiebt gefundene Dateien sofort aufs Array.
*   `python3 embycache_cleaner.py --add-to-list` : Fügt gefundene Dateien zur Exclude-Liste hinzu (adoptieren).

- - -

## 🤖 Automatisierung (User Scripts Plugin)

Es wird empfohlen, das Skript über das **"User Scripts"** Plugin in Unraid laufen zu lassen.

1.  Erstelle ein neues Script.
2.  Füge den Befehl ein:  
    `python3 /mnt/user/system/scripts/embycache/embycache_run.py --run`
3.  Setze den Zeitplan (z.B. "Hourly" oder alle X Stunden).

- - -

## 📂 Dateistruktur & Erklärung

Nach der ersten Ausführung wirst du folgende Dateien im Ordner finden:

*   **embycache\_run.py:** Das Hauptskript (Logik für Resume/Next-Up).
*   **embycache\_setup.py:** Der Assistent zum Erstellen der Config.
*   **embycache\_cleaner.py:** Tool zum Bereinigen von Dateileichen auf dem Cache.
*   **embycache\_settings.json:** Deine Konfiguration (URL, API-Keys, Pfade).
*   **embycache\_exclude.txt:** Eine Liste aller Dateien, die sich aktuell auf dem Cache befinden (für Mover-Tuning).
*   **embycache\_origin.json:** Interne Datenbank für Ursprungs-Pfade (wird aktuell nur informativ gepflegt).
*   **logs/embycache.log:** Das Logfile für Fehlerdiagnose und Aktivitätsnachweis.
