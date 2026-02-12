
# EmbyCache - Intelligentes Medien-Caching für Unraid

**EmbyCache** ist eine Skript-Sammlung für Unraid, die Medien basierend auf dem Nutzerverhalten von Emby (oder Jellyfin) proaktiv vom Array auf den schnellen Cache (SSD/NVMe) verschiebt. Es sorgt dafür, dass angefangene Filme und die nächsten Episoden einer Serie sofort ohne "Spin-up"-Verzögerung abspielbar sind.

---

## 🚀 Features (Version 4.4)

* **Intelligentes Caching:** Analysiert "Weiterschauen" (Resume) und lädt Inhalte auf den Cache.
* **Binge-Ready Logik:** Erkennt Serien und lädt automatisch die nächsten *X* Episoden vor – auch bei komplexen Ordnerstrukturen (Staffel-Ordner).
* **Origin-Aware (Ursprungs-Gedächtnis):** Merkt sich, von welcher Disk (z.B. `disk1`, `disk2`) eine Datei kam und schreibt sie beim Aufräumen exakt dorthin zurück. Verhindert das "Verstreuen" von Dateien durch Unraid.
* **Session-Schutz:** Verschiebt keine Dateien, die gerade aktiv abgespielt werden.
* **Favoriten-Support:** Kann optional auch Favoriten-Serien vorladen.
* **Mover-Kompatibilität:** Erstellt eine Exclude-Liste, um Konflikte mit dem Standard-Mover zu vermeiden.

---

## 📋 Voraussetzungen

* **Unraid OS** (oder ein vergleichbares Linux-System mit Python).
* **Python 3** (Vorinstalliert oder via NerdTools/Plugin).
* **Python Library "requests":** Wird für die API-Kommunikation benötigt.

### Installation der Abhängigkeiten

Führe folgenden Befehl im Unraid-Terminal aus:

```
pip install requests
```

---

## 🛠️ Installation & Einrichtung

### 1. Dateien kopieren

Erstelle einen Ordner auf deinem System (z.B. `/mnt/user/system/scripts/embycache`) und lege die folgenden beiden Skripte dort ab:

* `embycache_setup.py` (Konfigurations-Assistent)
* `embycache_run.py` (Hauptprogramm)

### 2. Konfiguration (Setup)

Starte den interaktiven Einrichtungs-Assistenten. Er führt dich durch alle notwendigen Einstellungen und erstellt die `embycache_settings.json`.

```
python3 embycache_setup.py
```

**Was abgefragt wird:**

* **Emby Server:** URL (z.B. http://192.168.1.10:8096) und API Key.
* **Pfade:** Zuordnung von Docker-Pfaden zu Unraid-Pfaden (z.B. `/data/Serien` -> `/mnt/user/Serien`).
* **Benutzer:** Auswahl der Benutzer, deren "Weiterschauen"-Liste überwacht werden soll.
* **Cache & Array:** Pfade zu deinem Cache-Pool und dem Array (meist `/mnt/user0`).
* **Limits:** Wie viele Episoden sollen im Voraus geladen werden?

---

## ▶️ Verwendung

### Manueller Test (Dry-Run)

Standardmäßig läuft das Skript im Simulations-Modus. Es zeigt im Log an, was es tun würde, verschiebt aber keine Dateien. Ideal zum Testen.

```
python3 embycache_run.py
```

### Live-Modus (Dateien verschieben)

Um die Dateien wirklich zu bewegen, muss das Argument `--run` angehängt werden.

```
python3 embycache_run.py --run
```

### Automatisierung (User Scripts Plugin)

Es wird empfohlen, das Skript über das **"User Scripts"** Plugin in Unraid laufen zu lassen.

1. Erstelle ein neues Script.
2. Füge den Befehl ein: `python3 /pfad/zu/deinem/script/embycache_run.py --run`
3. Setze den Zeitplan (z.B. "Hourly" oder alle X Stunden).

---

## 📂 Dateistruktur & Erklärung

Nach der ersten Ausführung wirst du folgende Dateien im Ordner finden:

* **embycache_run.py:** Das Hauptskript (Logik).
* **embycache_setup.py:** Der Assistent zum Erstellen der Config.
* **embycache_settings.json:** Deine Konfiguration (URL, API-Keys, Pfade).
* **embycache_exclude.txt:** Eine Liste aller Dateien, die sich aktuell auf dem Cache befinden. Kann für Mover-Tuning Plugins verwendet werden.
* **embycache_origin.json:** Die interne Datenbank. Hier merkt sich das Skript, von welcher Disk (z.B. Disk 5) eine Datei kam, um sie später exakt dorthin zurückzulegen.
* **logs/embycache.log:** Das Logfile für Fehlerdiagnose und Aktivitätsnachweis.