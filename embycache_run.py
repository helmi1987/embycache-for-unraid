#!/usr/bin/env python3
"""
EmbyCache – holt «Weiterschauen»-Inhalte, die nächsten Folgen und Favoriten-Serien der Emby-Benutzer
vom Unraid-Array auf den Cache-Pool und räumt nicht mehr benötigte Dateien wieder aufs Array.

Ablauf pro Lauf:
  1. Sessions abfragen – was gerade läuft, wird nie angefasst
  2. On-Deck-Liste berechnen (Weiterschauen, «Als Nächstes» (NextUp), nächste Folgen in Serienreihenfolge, Favoriten)
       Zähl-Modus (Default): number_episodes Folgen pro Serie, max_resume_items Einträge pro Benutzer
       Budget-Modus (cache_budget gesetzt, z.B. "2.5T"): das Budget wird fair auf die aktiven Benutzer verteilt
       (feste Budgets pro Benutzer möglich, ungenutzter Anteil fliesst an die anderen); pro Benutzer werden
       Filme und Serien nach Bytes im Verhältnis movie_share_percent gefüllt, Serien reihum eine Folge pro
       Runde – kleine Folgen ergeben viele, grosse wenige. Was nicht ins Budget passt, bleibt auf dem Array.
  3. Cleanup: Dateien aus der alten Exclude-Liste, die nicht mehr on deck sind -> Cache -> Array. Werkzeug per cleanup_tool:
       mover (Default): Unraid move-Binary wie das Original – setzt voraus, dass die Mover-Richtung des Shares
                        Cache -> Array ist (shareUseCache=yes); das Script prüft das vorher und meldet es
       rsync:           rsync /mnt/<pool>/<rel> -> /mnt/user0/<rel> (shfs wählt die Disk), Grössenvergleich, dann
                        Quelle löschen – unabhängig von der Mover-Richtung des Shares
  4. Befüllen: On-Deck-Dateien, die noch auf dem Array liegen -> auf den Cache. Werkzeug per fill_tool:
       rsync (Default): rsync -aAX --numeric-ids /mnt/user0/<rel> /mnt/<pool>/<rel> – wie das Original (plus
                        --info=progress2 für den Status, abschaltbar mit copy_progress=false);
                        Quelle erst nach Rückgabecode 0 und Grössenvergleich löschen. array_source = disk liest
                        stattdessen vom echten /mnt/diskN-Pfad (schneller, am FUSE vorbei) – optional
       mover:           Array-Pfade ans Unraid move-Binary pipen (wie der Stock-Mover bei «prefer»-Shares);
                        das Binary wählt den Pool aus der Share-Konfiguration – vorher mit einer Datei testen
     Der Freiplatz-Floor wird in beiden Fällen vorher für alle geplanten Kopien zusammen geprüft.
     rsync-Kopien optional parallel: parallel_per_disk pro Quell-Disk, parallel_total gesamt (Default 1/1 = sequentiell)
  5. Exclude-Liste neu schreiben (alle On-Deck-Dateien, die jetzt auf dem Cache liegen)

Aufruf:
  python3 embycache_run.py                 Dry-Run: zeigt alle geplanten Aktionen, verändert nichts
  python3 embycache_run.py --show-on-deck  Report pro Benutzer: Filme, dann Serien mit ihren Folgen, dann «Nicht im Budget»
      --user Benj,Kid   nur diese Benutzer anzeigen (Name oder ID); die Planung läuft immer für alle
      --compact         nur Einträge, keine einzelnen Dateien
  python3 embycache_run.py --run           Scharf
  python3 embycache_run.py --status        Live-Status des laufenden bzw. letzten Laufs (--watch [SEK] = laufend neu)

Umgebungsvariablen (überschreiben Config bzw. Defaults; CLI-Flags haben Vorrang vor EMBYCACHE_MODE):
  EMBYCACHE_MODE              dry | report | run
  EMBYCACHE_DIR               Arbeitsverzeichnis (Settings, Exclude-Liste, Lock, logs/); Default: Script-Verzeichnis
  EMBYCACHE_CONFIG            Pfad zur Settings-JSON
  EMBYCACHE_LOG_LEVEL         DEBUG | INFO | WARNING (Default INFO)
  EMBYCACHE_MIN_FREE_PERCENT  Mindest-Freiplatz auf dem Cache in Prozent (überschreibt min_free_percent)
  EMBYCACHE_MOVER_DEBUG       0–3, Parameter -d für das move-Binary (überschreibt mover_debug_level)
  EMBYCACHE_RSYNC_ARGS        vollständige rsync-Optionen, Default "-aAX --numeric-ids" (überschreibt rsync_args)
  EMBYCACHE_FILL_TOOL         rsync | mover (überschreibt fill_tool)
  EMBYCACHE_CLEANUP_TOOL      mover | rsync (überschreibt cleanup_tool)
  EMBYCACHE_CACHE_BUDGET      z.B. "2.5T" (überschreibt cache_budget; "" = Zähl-Modus)
  EMBYCACHE_REPORT_USER       wie --user
  EMBYCACHE_PARALLEL_PER_DISK gleichzeitige rsync-Kopien pro Quell-Disk (überschreibt parallel_per_disk)
  EMBYCACHE_PARALLEL_TOTAL    gleichzeitige rsync-Kopien insgesamt, 0 = ohne Limit (überschreibt parallel_total)
  EMBYCACHE_STATUS_LOG_INTERVAL  Sekunden zwischen Fortschrittszeilen im Log, 0 = aus

Beispiel User Scripts (Unraid):  cd /mnt/user/system/scripts/embycache && python3 embycache_run.py --run
"""
import argparse
import collections
import contextlib
import os
import sys
import threading
import time
from pathlib import Path

from embycache_lib import (
    ConfigError, EmbyApi, Locations, Status, acquire_lock, format_status, collect_sessions, detect_mover_bin, free_percent_after,
    human, is_playing, load_config, read_exclude, remove_empty_parents, run_mover, run_rsync, setup_logging,
    share_mode_ok, summarize_mover_output, unraid_mover_running, write_exclude, EXCLUDE_FILE,
    __version__,
)


def group_key(rel):
    """Gruppierung für die Log-Ausgabe: Share/oberster Ordner (Serie bzw. Filmordner), sonst der Share."""
    parts = Path(rel).parts
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def log_grouped(label, rels_with_size):
    """Listet Dateien gruppiert: eine Kopfzeile pro Serie/Filmordner, darunter die Dateien eingerückt."""
    groups = {}
    for rel, size in rels_with_size:
        groups.setdefault(group_key(rel), []).append((rel, size))
    for key, items in groups.items():
        log.info(f"[{label}] {key}: {len(items)} Datei{'en' if len(items) != 1 else ''}, {human(sum(sz for _, sz in items))}")
        for rel, size in items:
            log.info(f"      {human(size):>10}  {rel}")

log = setup_logging("EmbyCache", "embycache.log")


class OnDeckFile:
    __slots__ = ("rel", "size", "on_cache", "reason")

    def __init__(self, rel, size, on_cache, reason):
        self.rel, self.size, self.on_cache, self.reason = rel, size, on_cache, reason


# --------------------------------------------------------------------------- Planung
class Group:
    """Atomare Planungseinheit: alle Dateien eines Items (Film + Untertitel + Extras, Folge + srt …)."""
    __slots__ = ("files", "size", "category", "chain", "source", "series", "title", "reason")

    def __init__(self, files, category, chain, source, series, title, reason):
        self.files, self.category, self.chain = files, category, chain
        self.source, self.series, self.title, self.reason = source, series, title, reason
        self.size = sum(f.size for f in files)

    def location(self):
        on = sum(1 for f in self.files if f.on_cache)
        return "CACHE" if on == len(self.files) else "ARRAY" if on == 0 else "TEILS"


SOURCE_ORDER = ("Weiterschauen", "Als Nächstes", "Nächste Folge", "Favorit")


def ep_label(ep):
    """«S02E03 Titel» – oder nur der Titel, wenn Emby keine Nummern liefert."""
    s, e = ep.get("ParentIndexNumber"), ep.get("IndexNumber")
    name = ep.get("Name", "?")
    return f"S{s:02d}E{e:02d} {name}" if s is not None and e is not None else name


class UserPlan:
    """Kandidaten und Auswahl eines Benutzers (pro Instanz)."""

    def __init__(self, key, name, uid="", server=""):
        self.key, self.name, self.uid, self.server = key, name, uid, server
        self.movies = []       # Group, nach Aktualität (Weiterschauen)
        self.series = []       # Group, reihum über alle laufenden Serien und Favoriten
        self.selected = []
        self.skipped = []
        self.bytes = self.movie_bytes = self.series_bytes = 0
        self.budget = None
        self.exhausted = True

    def has_candidates(self):
        return bool(self.movies or self.series)


class Planner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.files = {}      # rel (str) -> OnDeckFile (Ergebnis)
        self.users = {}      # key -> UserPlan
        self.errors = 0      # API-Fehler: Planung unvollständig -> kein Cleanup in diesem Lauf
        self.budget_mode = int(cfg["cache_budget_bytes"]) > 0

    # ------------------------------------------------------------------ Dateien eines Items
    def files_for_item(self, loc, item):
        """Ermittelt alle Dateien (relativ zur Share-Ebene), die zu einem Emby-Item gehören."""
        raw = item.get("Path") or ""
        if not raw or item.get("LocationType") == "Virtual":
            return []
        rel = loc.rel_from_docker(raw)
        if rel is None:
            log.warning(f"Kein Path-Mapping für {raw} – Item übersprungen (Mapping in der Config prüfen)")
            return []
        exists_dir = loc.on_array(rel).is_dir() or loc.on_cache(rel).is_dir()
        exists_file = loc.on_array(rel).is_file() or loc.on_cache(rel).is_file()
        if not exists_dir and not exists_file:
            log.warning(f"Datei nicht gefunden: {rel} (Mapping oder array_path/cache_path prüfen)")
            return []
        if exists_dir:
            # Ordner-Item (DVD/BluRay-Struktur): den Ordner selbst mitnehmen, nie den Elternordner
            return loc.list_files(rel, recursive=True)
        parent = rel.parent
        if item.get("Type") == "Movie" and self.cfg["movie_mode"] == "folder" and not loc.is_library_root(parent):
            return loc.list_files(parent, recursive=True)
        # Episoden, Filme in flacher Ablage, movie_mode=file: nur Dateien mit gleichem Basisnamen (mkv, srt, nfo …)
        stem = rel.stem
        return [f for f in loc.list_files(parent, recursive=False) if f.name.startswith(stem)]

    def make_group(self, loc, item, category, chain, source, title, uname, series=None):
        reason = f"{uname}: {source} «{series + ' – ' if series else ''}{title}»"
        files = []
        for rel in self.files_for_item(loc, item):
            cache_p = loc.on_cache(rel)
            on_cache = cache_p.exists()
            src = cache_p if on_cache else loc.on_array(rel)
            try:
                size = src.stat().st_size
            except OSError as e:
                log.warning(f"Datei nicht lesbar, übersprungen: {src} ({e})")
                continue
            files.append(OnDeckFile(rel, size, on_cache, reason))
        return Group(files, category, chain, source, series, title, reason) if files else None

    # ------------------------------------------------------------------ Emby-Abfragen
    @staticmethod
    def ep_key(ep):
        return (ep.get("ParentIndexNumber") if ep.get("ParentIndexNumber") is not None else -1,
                ep.get("IndexNumber") if ep.get("IndexNumber") is not None else -1)

    def series_episodes(self, api, uid, series_id):
        eps = api.items(f"/Users/{uid}/Items", ParentId=series_id, Recursive="true", IncludeItemTypes="Episode",
                        SortBy="ParentIndexNumber,IndexNumber", SortOrder="Ascending",
                        Fields="Path,ParentIndexNumber,IndexNumber,LocationType")
        eps = [e for e in eps if e.get("Path") and e.get("LocationType") != "Virtual"]
        return sorted(eps, key=self.ep_key)

    def episode_cap(self):
        """Wie viele Folgen pro Serie höchstens: Zähl-Modus number_episodes (0 = keine),
        Budget-Modus max_episodes_per_series (0 = unbegrenzt, das Budget entscheidet)."""
        if self.budget_mode:
            n = int(self.cfg["max_episodes_per_series"])
            return n if n > 0 else None
        n = int(self.cfg["number_episodes"])
        return n if n > 0 else 0

    def next_episodes_after(self, api, uid, current):
        """Die Folgen nach der aktuellen – in Staffel-/Folgenreihenfolge, unabhängig vom Gesehen-Status."""
        cap = self.episode_cap()
        if cap == 0 or not current.get("SeriesId"):
            return []
        cur = self.ep_key(current)
        eps = self.series_episodes(api, uid, current["SeriesId"])
        after = [e for e in eps if self.ep_key(e) > cur and e.get("Id") != current.get("Id")]
        return after[:cap] if cap else after

    def next_unplayed(self, api, uid, series_id):
        """Für Favoriten ohne Resume-Eintrag: ab der ersten ungesehenen Folge."""
        cap = self.episode_cap()
        if cap == 0:
            return []
        eps = self.series_episodes(api, uid, series_id)
        unplayed = [e for e in eps if not (e.get("UserData") or {}).get("Played")]
        return unplayed[:cap] if cap else unplayed

    # ------------------------------------------------------------------ Kandidaten pro Benutzer
    def plan_user(self, api, loc, name, uid, uname):
        up = UserPlan(f"{name}:{uid}", uname, uid, name)
        try:
            resume = api.items(f"/Users/{uid}/Items/Resume", MediaTypes="Video", Limit=int(self.cfg["max_resume_items"]),
                               Fields="Path,SeriesId,ParentIndexNumber,IndexNumber,LocationType")
        except RuntimeError as e:
            self.errors += 1; log.error(f"[{name}] Resume-Liste von {uname} nicht abrufbar: {e}")
            return
        chains = []  # Listen von Groups je Serie, in Aktualitätsreihenfolge
        seen_series = set()
        for item in resume:
            if item.get("Type") == "Movie":
                g = self.make_group(loc, item, "movie", f"movie:{item.get('Id')}", "Weiterschauen", item.get("Name", "?"), uname)
                if g:
                    up.movies.append(g)
                continue
            if item.get("Type") != "Episode":
                continue
            sname = item.get("SeriesName", "?")
            sid = item.get("SeriesId") or f"ep:{item.get('Id')}"
            if sid in seen_series:
                continue
            seen_series.add(sid)
            chain = []
            g = self.make_group(loc, item, "series", f"series:{sid}", "Weiterschauen", ep_label(item), uname, sname)
            if g:
                chain.append(g)
            try:
                for ep in self.next_episodes_after(api, uid, item):
                    g = self.make_group(loc, ep, "series", f"series:{sid}", "Nächste Folge", ep_label(ep), uname, sname)
                    if g:
                        chain.append(g)
            except RuntimeError as e:
                self.errors += 1; log.error(f"[{name}] Folgen von «{sname}» nicht abrufbar: {e}")
            if chain:
                chains.append(chain)

        # «Als Nächstes» (NextUp): Serien, bei denen die letzte Folge fertig geschaut ist und die nächste
        # noch nicht läuft – ohne diese Quelle fiele die Serie zwischen zwei Folgen aus dem Cache
        if self.cfg["use_next_up"]:
            try:
                nextup = api.items("/Shows/NextUp", UserId=uid, Limit=int(self.cfg["max_resume_items"]),
                                   Fields="Path,SeriesId,ParentIndexNumber,IndexNumber,LocationType")
            except RuntimeError as e:
                self.errors += 1; log.error(f"[{name}] NextUp-Liste von {uname} nicht abrufbar: {e}")
                nextup = []
            for item in nextup:
                if item.get("Type") != "Episode":
                    continue
                sname = item.get("SeriesName", "?")
                sid = item.get("SeriesId") or f"ep:{item.get('Id')}"
                if sid in seen_series:
                    continue
                seen_series.add(sid)
                chain = []
                g = self.make_group(loc, item, "series", f"series:{sid}", "Als Nächstes", ep_label(item), uname, sname)
                if g:
                    chain.append(g)
                try:
                    for ep in self.next_episodes_after(api, uid, item):
                        g = self.make_group(loc, ep, "series", f"series:{sid}", "Nächste Folge", ep_label(ep), uname, sname)
                        if g:
                            chain.append(g)
                except RuntimeError as e:
                    self.errors += 1; log.error(f"[{name}] Folgen von «{sname}» nicht abrufbar: {e}")
                if chain:
                    chains.append(chain)

        max_fav = int(self.cfg["max_favorite_series"])
        if max_fav > 0:
            try:
                favs = api.items(f"/Users/{uid}/Items", Recursive="true", IncludeItemTypes="Series",
                                 Filters="IsFavorite", Limit=max_fav, Fields="Path")
            except RuntimeError as e:
                self.errors += 1; log.error(f"[{name}] Favoriten von {uname} nicht abrufbar: {e}")
                favs = []
            for series in favs:
                sid = series.get("Id")
                if sid in seen_series:
                    continue
                seen_series.add(sid)
                chain = []
                try:
                    for ep in self.next_unplayed(api, uid, sid):
                        g = self.make_group(loc, ep, "series", f"series:{sid}", "Favorit", ep_label(ep), uname, series.get("Name", "?"))
                        if g:
                            chain.append(g)
                except RuntimeError as e:
                    self.errors += 1; log.error(f"[{name}] Folgen von Favorit «{series.get('Name', '?')}» nicht abrufbar: {e}")
                if chain:
                    chains.append(chain)

        # Reihum über alle Serien: jede Serie eine Folge pro Runde – kleine Folgen ergeben so
        # automatisch mehr Stück im Budget als grosse, keine Serie kann alles belegen
        pos = 0
        while any(pos < len(c) for c in chains):
            for c in chains:
                if pos < len(c):
                    up.series.append(c[pos])
            pos += 1
        self.users[up.key] = up

    def plan_instance(self, inst, uids):
        api = EmbyApi(inst["url"], inst["api_key"], self.cfg["api_timeout"])
        loc = Locations(self.cfg, inst["_mappings"])
        name = inst["servername"]
        if not uids:
            try:
                users = api.get("/Users") or []
            except RuntimeError as e:
                self.errors += 1; log.error(f"[{name}] Benutzerliste nicht abrufbar: {e}")
                return
            uids = [(u["Id"], u.get("Name", u["Id"])) for u in users]
        for uid, uname in uids:
            self.plan_user(api, loc, name, uid, uname)

    # ------------------------------------------------------------------ Auswahl
    @staticmethod
    def select(up, budget, movie_share):
        """Füllt das Budget eines Benutzers: Filme und Serien im Verhältnis movie_share nach Bytes (weich –
        ist eine Kategorie leer, bekommt die andere den Rest). Passt ein Eintrag nicht mehr, wird seine
        Kette (die Serie) gestoppt, damit nie Folge 5 ohne Folge 4 gecacht wird."""
        queues = {"movie": up.movies, "series": up.series}
        idx = {"movie": 0, "series": 0}
        used = {"movie": 0, "series": 0}
        target = {"movie": budget * movie_share, "series": budget * (1.0 - movie_share)}
        stopped = set()
        up.selected, up.skipped, up.bytes = [], [], 0

        def fill_fraction(cat):
            return (used[cat] / target[cat]) if target[cat] > 0 else float("inf")

        while True:
            avail = [c for c in ("movie", "series") if idx[c] < len(queues[c])]
            if not avail:
                break
            cat = min(avail, key=fill_fraction)
            g = queues[cat][idx[cat]]
            idx[cat] += 1
            if g.chain in stopped:
                up.skipped.append(g)
                continue
            if up.bytes + g.size > budget:
                stopped.add(g.chain)
                up.skipped.append(g)
                continue
            up.selected.append(g)
            up.bytes += g.size
            used[cat] += g.size
        up.movie_bytes, up.series_bytes = used["movie"], used["series"]
        up.exhausted = not up.skipped
        up.budget = budget

    def apply_budget(self):
        """Verteilt cache_budget fair: feste Benutzer-Budgets zuerst, der Rest gleichmässig auf alle
        aktiven Benutzer; was ein Benutzer nicht braucht, fliesst an die anderen (Water-Filling)."""
        total = int(self.cfg["cache_budget_bytes"])
        share = float(self.cfg["movie_share_percent"]) / 100.0
        user_budgets = self.cfg["user_budgets"]
        active = [u for u in self.users.values() if u.has_candidates()]
        explicit = {u.key: user_budgets[u.uid] for u in active if u.uid in user_budgets}
        pool = total - sum(explicit.values())
        if pool < 0:
            log.warning(f"Feste Benutzer-Budgets ({human(sum(explicit.values()))}) übersteigen cache_budget ({human(total)})")
            pool = 0
        flexible = [u for u in active if u.key not in explicit]
        fixed = {}
        cap = 0.0
        for _ in range(len(flexible) + 1):
            open_users = [u for u in flexible if u.key not in fixed]
            if not open_users:
                break
            cap = (pool - sum(fixed.values())) / len(open_users)
            changed = False
            for u in open_users:
                self.select(u, cap, share)
                if u.exhausted and u.bytes < cap:
                    fixed[u.key] = u.bytes
                    changed = True
            if not changed:
                break
        for u in flexible:
            if u.key not in fixed:
                self.select(u, cap, share)
        for u in active:
            if u.key in explicit:
                self.select(u, float(explicit[u.key]), share)
        for u in active:
            skipped = sum(g.size for g in u.skipped)
            log.info(f"Budget {u.name}: {human(u.bytes)} von {human(u.budget)} "
                     f"(Filme {human(u.movie_bytes)}, Serien {human(u.series_bytes)})"
                     + (f" – nicht im Budget: {len(u.skipped)} Einträge, {human(skipped)}" if u.skipped else ""))

    def run(self):
        wanted = self.cfg["valid_users"]
        for inst in self.cfg["instances"]:
            uids = []
            if wanted:
                # Namen für die Log-Ausgabe auflösen; unbekannte IDs (z.B. andere Instanz) still überspringen
                try:
                    known = {u["Id"]: u.get("Name", u["Id"]) for u in EmbyApi(inst["url"], inst["api_key"], self.cfg["api_timeout"]).get("/Users") or []}
                except RuntimeError as e:
                    self.errors += 1; log.error(f"[{inst['servername']}] nicht erreichbar: {e}")
                    continue
                uids = [(u, known[u]) for u in wanted if u in known]
                if not uids:
                    log.warning(f"[{inst['servername']}] keiner der konfigurierten Benutzer existiert auf dieser Instanz")
                    continue
            self.plan_instance(inst, uids)

        if self.budget_mode:
            self.apply_budget()
        else:
            for u in self.users.values():
                u.selected = u.movies + u.series
        for u in self.users.values():
            for g in u.selected:
                for f in g.files:
                    self.files.setdefault(str(f.rel), f)
        if self.budget_mode:
            total = sum(f.size for f in self.files.values())
            log.info(f"Budget gesamt: {human(total)} von {human(int(self.cfg['cache_budget_bytes']))} "
                     f"für {sum(1 for u in self.users.values() if u.has_candidates())} aktive Benutzer")
        return list(self.files.values())


# --------------------------------------------------------------------------- Ausführung
class Runner:
    def __init__(self, cfg, mode, user_filter=None, show_files=True):
        self.cfg = cfg
        self.mode = mode  # dry | report | run
        self.user_filter = user_filter
        self.show_files = show_files
        self.run_mode = mode == "run"
        self.to_cache = self.to_array = 0
        self.copied_bytes = self.moved_back_bytes = 0
        self.fill_planned = self.cleanup_planned = 0
        self.sim_delta = 0  # Dry-Run: Bytes, die der Cleanup freigäbe, minus Bytes geplanter Kopien
        self.copied = self.moved_back = 0
        self.fill_failures = 0
        self._lock = threading.Lock()  # Zähler und Löschen/Ordner-Aufräumen bei parallelen Kopien
        self.status = Status(mode, log, enabled=self.run_mode, log_interval=cfg["status_log_interval"])

    def cleanup_with_rsync(self, loc, candidates, listing, protected):
        """Cache -> Array per rsync nach /mnt/user0 (shfs wählt die Disk), Quelle erst nach Prüfung löschen."""
        sizes = {str(loc.cache / r): sz for r, sz in listing}
        failures = 0
        self.status.phase("Cleanup (Cache -> Array, rsync)", len(candidates), sum(sizes.values()))
        for i, p in enumerate(candidates, 1):
            src = Path(p)
            rel = src.relative_to(loc.cache)
            dst = loc.on_array(rel)
            if failures >= 3:
                log.error("Drei rsync-Fehler in Folge – Cleanup abgebrochen, Ursache im Log prüfen")
                protected.add(p)
                continue
            if dst.exists():
                log.warning(f"Ziel existiert schon auf dem Array (Duplikat?), Datei bleibt auf dem Cache: {rel}")
                protected.add(p)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            key = f"{i}:{rel}"
            log.info(f"[{i}/{len(candidates)}] -> ARRAY {human(sizes.get(p, 0)):>10}  {rel}")
            self.status.job_start(key, rel, "Array", sizes.get(p, 0), i)
            cmd = self.rsync_cmd(src, dst)
            log.debug("rsync-Aufruf (Cleanup): " + " ".join(cmd))
            rc, out = run_rsync(cmd, lambda done, speed, eta: self.status.job_progress(key, done, speed, eta))
            if rc != 0:
                failures += 1
                log.error(f"rsync-Fehler (Code {rc}) bei {rel}: {out.strip()}")
                protected.add(p)
                self.status.job_end(key, False)
                continue
            failures = 0
            try:
                if dst.stat().st_size != src.stat().st_size:
                    log.error(f"Grösse stimmt nicht überein nach rsync, Datei bleibt auf dem Cache: {rel}")
                    protected.add(p)
                    self.status.job_end(key, False)
                    continue
                src.unlink()
            except OSError as e:
                log.error(f"Quelle konnte nicht gelöscht werden (Datei liegt jetzt doppelt!): {src} ({e})")
                protected.add(p)
                self.status.job_end(key, False)
                continue
            self.status.job_end(key, True)
            self.moved_back += 1
            self.moved_back_bytes += sizes.get(p, 0)
            remove_empty_parents(src.parent, loc.cache, log)

    def cleanup(self, loc, current, sessions, protected):
        """Alte Exclude-Einträge, die nicht mehr on deck sind -> move-Binary (Cache -> Array)."""
        previous = read_exclude()
        candidates, listing, checked = [], [], {}
        for p in sorted(previous):
            if p in current:
                continue
            path = Path(p)
            if not path.exists():
                continue
            try:
                rel = path.relative_to(loc.cache)
            except ValueError:
                log.warning(f"Exclude-Eintrag liegt nicht unter {loc.cache}, ignoriert: {p}")
                continue
            if is_playing(rel, sessions):
                log.info(f"[BLEIBT: läuft gerade] {rel}")
                protected.add(p)
                continue
            if not share_mode_ok(self.cfg, rel.parts[0], checked, log):
                protected.add(p)  # bleibt geschützt, bis die Share-Einstellung stimmt
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self.to_array += size
            candidates.append(p)
            listing.append((rel, size))
        self.cleanup_planned = len(candidates)
        if not candidates:
            return
        log_grouped("MOVE -> ARRAY" if self.run_mode else "PLAN: -> ARRAY", listing)
        log.info(f"Cleanup: {len(candidates)} Dateien ({human(self.to_array)}) vom Cache aufs Array")
        if not self.run_mode:
            self.sim_delta += self.to_array  # Dry-Run: diesen Platz gäbe der Cleanup frei
            return
        if self.cfg["cleanup_tool"] == "rsync":
            log.info("Cleanup-Werkzeug: rsync nach " + str(loc.array))
            self.cleanup_with_rsync(loc, candidates, listing, protected)
            return
        mover = detect_mover_bin(self.cfg)
        if not mover:
            log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen) – Cleanup übersprungen")
            protected.update(candidates)
            return
        self.status.phase("Cleanup (Cache -> Array, move-Binary)", len(candidates), self.to_array)
        sizes = {str(loc.cache / r): sz for r, sz in listing}
        tracked = [(p, Path(p).relative_to(loc.cache), sizes.get(p, 0), lambda rel: loc.on_disk(rel)) for p in candidates]
        with self.track_mover(tracked, "-> ARRAY"):
            rc, stdout, stderr = run_mover(mover, candidates, self.cfg["mover_debug_level"], log)
        messages = summarize_mover_output(stdout + "\n" + stderr, candidates)
        left_by_reason = {}
        for p in candidates:
            if Path(p).exists():
                protected.add(p)
                reason = messages.get(p, "keine Meldung vom Mover (in Benutzung oder Ziel existiert bereits)")
                left_by_reason.setdefault(reason, []).append(p)
            else:
                self.moved_back += 1
                self.moved_back_bytes += sizes.get(p, 0)
                remove_empty_parents(Path(p).parent, loc.cache, log)
        for reason, paths in left_by_reason.items():
            log.warning(f"{len(paths)} Dateien nicht verschoben – Mover: {reason}")
            for p in paths[:3]:
                log.warning(f"      {p}")
            if len(paths) > 3:
                log.warning(f"      … und {len(paths) - 3} weitere (alle im DEBUG-Log)")
            for p in paths[3:]:
                log.debug(f"      {p}")
            if "No space left" in reason:
                cache_hits = [l for l in stdout.splitlines() if "create_parent" in l and str(loc.cache) in l]
                if cache_hits:
                    log.error("Der Mover wollte das Ziel AUF DEM CACHE anlegen – für diesen Share ist der Cache sein Ziel "
                              "(Share-Einstellung/Standardwerte, siehe Zeile «Share «…»: shareUseCache=…» oben). "
                              "Entweder Share auf Cache: Yes (Mover Cache → Array) stellen oder cleanup_tool=rsync verwenden.")
                else:
                    log.error("Ziel voll: der Mover findet auf dem Array keinen Platz (Share-Einstellung «Minimum free space», "
                              "Allocation/Split-Level oder eingeschlossene Disks prüfen).")

    def fill(self, loc, files, sessions):
        """On-Deck-Dateien vom Array auf den Cache – per rsync (Quelle erst nach Prüfung löschen) oder per move-Binary.
        Erst wird geplant (Freiplatz inkl. aller geplanten Kopien), dann kopiert – sequentiell oder parallel pro Disk."""
        from_disk = self.cfg["array_source"] == "disk"
        min_free = float(self.cfg["min_free_percent"])
        use_mover = self.cfg["fill_tool"] == "mover"
        label = "MOVE" if use_mover else "COPY"
        jobs = []  # (OnDeckFile, src, disk)
        pending = 0  # scharfer Lauf: Bytes, die die geplanten Kopien noch belegen werden
        last_group = None
        for f in sorted(files, key=lambda x: str(x.rel)):
            if f.on_cache:
                continue
            if is_playing(f.rel, sessions):
                log.info(f"[SKIP: läuft gerade] {f.rel}")
                continue
            share_root = loc.cache / f.rel.parts[0]
            if not share_root.is_dir():
                if not self.cfg["create_share_root"]:
                    log.error(f"Share-Wurzel {share_root} fehlt. Auf ZFS-Pools zuerst als Dataset anlegen "
                              f"(zfs create <pool>/{f.rel.parts[0]}) oder create_share_root=true setzen. Übersprungen: {f.rel}")
                    continue
                if self.run_mode:
                    share_root.mkdir(parents=True, exist_ok=True)
            # Dataset (Quota) und Pool-Wurzel prüfen – appdata liegt oft auf demselben Pool
            delta = self.sim_delta if not self.run_mode else -pending
            free_after = free_percent_after(loc.cache, f.size, delta)
            if share_root.is_dir():
                free_after = min(free_after, free_percent_after(share_root, f.size, delta))
            if free_after < min_free:
                log.warning(f"[SKIP: Freiplatz] {f.rel} ({human(f.size)}) – danach nur {free_after:.1f} % frei, Minimum {min_free:g} %"
                            + (" (Dry-Run: Cleanup und geplante Kopien eingerechnet)" if not self.run_mode
                               else " (geplante Kopien eingerechnet)"))
                continue
            disks = loc.on_disk(f.rel)
            sources = disks if from_disk else []
            if len(disks) > 1:
                log.warning(f"Datei liegt auf mehreren Disks (Duplikat im Array!): {', '.join(map(str, disks))} – nehme die erste")
            src = sources[0] if sources else loc.on_array(f.rel)
            if not src.is_file():
                log.warning(f"Quelle nicht gefunden: {src}")
                continue
            disk = self._disk_name(disks[0], loc) if disks else "?"
            self.to_cache += f.size
            self.fill_planned += 1
            key = group_key(f.rel)
            if key != last_group:
                log.info(f"[{label if self.run_mode else 'PLAN:'} -> CACHE] {key}   ({f.reason})")
                last_group = key
            log.info(f"      {human(f.size):>10}  {disk:<7} {f.rel}")
            if not self.run_mode:
                self.sim_delta -= f.size  # Dry-Run: diese Kopie würde Platz belegen
                continue
            pending += f.size
            jobs.append((f, src, disk))

        per_disk, total = self.cfg["parallel_per_disk"], self.cfg["parallel_total"]
        if not use_mover and (per_disk > 1 or total != 1):
            n_disks = len({d for _, _, d in jobs}) or 1
            limit = per_disk * n_disks if total == 0 else min(total, per_disk * n_disks)
            log.info(f"Parallel: {per_disk} pro Disk, {'ohne Gesamtlimit' if total == 0 else f'höchstens {total} gesamt'}"
                     f" – {n_disks} Quell-Disk(s), also bis {limit} Kopien gleichzeitig")
        if not jobs:
            return
        if use_mover:
            self.fill_with_mover(loc, [(f, src) for f, src, _ in jobs])
            return
        self.status.phase("Befüllen (Array -> Cache)", len(jobs), sum(f.size for f, _, _ in jobs))
        if per_disk == 1 and total == 1:
            for i, job in enumerate(jobs, 1):  # wie das Original: eine Datei nach der anderen
                if self.fill_failures >= 3:
                    log.error("Drei rsync-Fehler in Folge – Befüllen abgebrochen, Ursache im Log prüfen")
                    break
                self.copy_one(loc, *job, i, len(jobs))
        else:
            self.copy_parallel(loc, jobs, per_disk, total)
        log.info(self.status.summary())

    def copy_parallel(self, loc, jobs, per_disk, total):
        """Pro Quell-Disk eine Warteschlange mit per_disk Workern; total begrenzt alle Disks zusammen (0 = ohne Limit)."""
        queues = {}
        for i, (f, src, disk) in enumerate(jobs, 1):
            queues.setdefault(disk, collections.deque()).append((f, src, disk, i))
        gate = threading.Semaphore(total) if total > 0 else None
        stop = threading.Event()

        def worker(disk):
            q = queues[disk]
            while not stop.is_set():
                with self._lock:
                    if not q:
                        return
                    f, src, d, i = q.popleft()
                if gate:
                    gate.acquire()
                try:
                    if stop.is_set():
                        return
                    self.copy_one(loc, f, src, d, i, len(jobs))
                    if self.fill_failures >= 3 and not stop.is_set():
                        stop.set()
                        log.error("Drei rsync-Fehler in Folge – Befüllen abgebrochen (laufende Kopien werden fertig), "
                                  "Ursache im Log prüfen")
                finally:
                    if gate:
                        gate.release()

        threads = [threading.Thread(target=worker, args=(disk,), name=f"copy-{disk}-{n}", daemon=True)
                   for disk, q in queues.items() for n in range(min(per_disk, len(q)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def rsync_cmd(self, src, dst):
        extra = ["--info=progress2"] if self.cfg["copy_progress"] else []
        return ["rsync", *self.cfg["rsync_args"], *extra, str(src), str(dst)]

    def copy_one(self, loc, f, src, disk, index, count):
        """Eine Datei Array -> Cache per rsync, Grössenvergleich, dann Quelle löschen. Thread-sicher."""
        dst = loc.on_cache(f.rel)
        key = f"{index}:{f.rel}"
        log.info(f"[{index}/{count}] Start  {disk:<7} {human(f.size):>10}  {f.rel}")
        self.status.job_start(key, f.rel, disk, f.size, index)
        t0 = time.time()
        ok = False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            cmd = self.rsync_cmd(src, dst)
            log.debug("rsync-Aufruf: " + " ".join(cmd))
            rc, out = run_rsync(cmd, lambda done, speed, eta: self.status.job_progress(key, done, speed, eta))
            if rc != 0:
                with self._lock:
                    self.fill_failures += 1
                log.error(f"rsync-Fehler (Code {rc}) bei {f.rel}: {out.strip()}")
                log.error("Quelle bleibt auf dem Array; Ziel prüfen, sonst liegt die Datei doppelt")
                return
            with self._lock:
                self.fill_failures = 0
                try:
                    if dst.stat().st_size != src.stat().st_size:
                        log.error(f"Grösse stimmt nicht überein nach rsync, Quelle bleibt: {f.rel}")
                        return
                    src.unlink()
                except OSError as e:
                    log.error(f"Quelle konnte nicht gelöscht werden (Datei liegt jetzt doppelt!): {src} ({e})")
                    return
                self.copied += 1
                self.copied_bytes += f.size
                f.on_cache = True
                ok = True
                remove_empty_parents(src.parent, self._disk_root(src, loc, src != loc.on_array(f.rel)), log)
        finally:
            self.status.job_end(key, ok)
            secs = time.time() - t0
            if ok:
                log.info(f"[{index}/{count}] Fertig {disk:<7} {human(f.size):>10} in {secs:.0f} s "
                         f"({human(f.size / secs if secs > 0 else 0)}/s)  {f.rel}")

    @staticmethod
    def _disk_name(path, loc):
        """/mnt/disk3/Filme/x.mkv -> disk3"""
        depth = len(Path(loc.disks_glob).parts)
        parts = Path(path).parts
        return parts[depth - 1] if len(parts) >= depth else "?"

    def fill_with_mover(self, loc, batch):
        """Array -> Cache über das Unraid move-Binary (ein Aufruf für alle Dateien), danach Ergebnis prüfen."""
        mover = detect_mover_bin(self.cfg)
        if not mover:
            log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen) – nichts kopiert")
            return
        log.info(f"Befüllen: {len(batch)} Dateien ({human(sum(f.size for f, _ in batch))}) per move-Binary Array -> Cache")
        self.status.phase("Befüllen (Array -> Cache, move-Binary)", len(batch), sum(f.size for f, _ in batch))
        tracked = [(str(src), f.rel, f.size, lambda rel: [loc.on_cache(rel)]) for f, src in batch]
        with self.track_mover(tracked, "-> CACHE"):
            run_mover(mover, [str(src) for _, src in batch], self.cfg["mover_debug_level"], log)
        for f, src in batch:
            dst = loc.on_cache(f.rel)
            if dst.exists() and not src.exists():
                self.copied += 1
                self.copied_bytes += f.size
                f.on_cache = True
                remove_empty_parents(src.parent, self._disk_root(src, loc, src != loc.on_array(f.rel)), log)
            elif dst.exists() and src.exists():
                log.error(f"Datei liegt jetzt doppelt (Quelle nicht entfernt): {f.rel}")
            else:
                log.warning(f"Nicht auf den Cache verschoben (in Benutzung, oder das Binary kennt keinen Pool für diesen Share?): {f.rel}")
        if self.copied == 0:
            log.warning("Das move-Binary hat nichts auf den Cache verschoben – fill_tool=rsync verwenden oder mover_debug_level=1 setzen und Log prüfen")

    @contextlib.contextmanager
    def track_mover(self, items, label):
        """Überwacht einen laufenden move-Binary-Aufruf: alle 2 s wird geprüft, welche Quellen schon weg sind.
        Das Binary arbeitet die Pfade der Reihe nach ab – die erste noch vorhandene Quelle gilt als aktuelle Datei;
        ihr Fortschritt ist die Grösse am Ziel, soweit dort schon sichtbar. items: (quelle, rel, size, targets(rel))."""
        stop = threading.Event()
        count = len(items)

        def monitor():
            pending = list(enumerate(items, 1))
            current = None
            skipped = set()  # schon als Fehler gezählt
            last_done = 0
            t_current = time.time()
            while True:
                finished = stop.wait(2)
                still = []
                for i, (src, rel, size, targets) in pending:
                    if os.path.exists(src):
                        still.append((i, (src, rel, size, targets)))
                        continue
                    key = f"m{i}"
                    if current != key:
                        self.status.job_start(key, rel, "move", size, i)
                    self.status.job_end(key, True)
                    secs = time.time() - t_current if current == key else 0
                    log.info(f"[{i}/{count}] {label} fertig {human(size):>10}"
                             + (f" in {secs:.0f} s" if secs else "") + f"  {rel}")
                    if current == key:
                        current = None
                    last_done = max(last_done, i)
                pending = still
                if finished:
                    # vom Binary liegengelassen (in Benutzung, Ziel voll …) – im Status als Fehler zählen
                    for i, (src, rel, size, targets) in pending:
                        key = f"m{i}"
                        if key in skipped:
                            continue
                        if current != key:
                            self.status.job_start(key, rel, "move", size, i)
                        self.status.job_end(key, False)
                    break
                if not pending:
                    break
                # aktuelle Datei = erste noch vorhandene nach der zuletzt fertigen (übersprungene bleiben zurück)
                i, (src, rel, size, targets) = next((x for x in pending if x[0] > last_done), pending[0])
                key = f"m{i}"
                if current != key:
                    if current:
                        skipped.add(current)
                        self.status.job_end(current, False)  # übersprungen (in Benutzung o.ä.) – Endergebnis prüft unten
                    current, t_current = key, time.time()
                    self.status.job_start(key, rel, "move", size, i)
                    log.info(f"[{i}/{count}] {label} läuft {human(size):>10}  {rel}")
                try:
                    done = max((t.stat().st_size for t in targets(rel) if t.is_file()), default=0)
                except OSError:
                    done = 0
                self.status.job_progress(key, done)

        t = threading.Thread(target=monitor, name="mover-monitor", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=30)

    @staticmethod
    def _disk_root(src, loc, from_disk):
        """/mnt/disk3/Filme/X/x.mkv -> /mnt/disk3; bei FUSE-Fallback array_path."""
        if not from_disk:
            return loc.array
        depth = len(Path(loc.disks_glob).parts)
        return Path(*src.parts[:depth]) if len(src.parts) > depth else loc.array

    @staticmethod
    def render_groups(groups, mark, show_files, indent="    "):
        """Filme einzeln, Serien als Block mit ihren Folgen in Reihenfolge."""
        chains = {}
        for g in groups:
            chains.setdefault(g.chain, []).append(g)
        n_files = "{n} Datei{pl}"
        for chain_groups in chains.values():
            first = chain_groups[0]
            if first.series is None:
                for g in chain_groups:
                    print(f"{indent}{mark} {g.title}   [{g.location()}] {human(g.size)}, "
                          f"{n_files.format(n=len(g.files), pl='en' if len(g.files) != 1 else '')}   ({g.source})")
                    if show_files:
                        for f in g.files:
                            print(f"{indent}  {mark} [{'CACHE' if f.on_cache else 'ARRAY'}] {human(f.size):>10}  {f.rel}")
                continue
            total = sum(g.size for g in chain_groups)
            locs = {g.location() for g in chain_groups}
            loc = locs.pop() if len(locs) == 1 else "TEILS"
            print(f"{indent}{mark} {first.series}   ({first.source})  {len(chain_groups)} Folge{'n' if len(chain_groups) != 1 else ''}, "
                  f"{human(total)}  [{loc}]")
            for g in chain_groups:
                print(f"{indent}  {mark} {g.title}   [{g.location()}] {human(g.size)}, "
                      f"{n_files.format(n=len(g.files), pl='en' if len(g.files) != 1 else '')}   ({g.source})")
                if show_files:
                    for f in g.files:
                        print(f"{indent}    {mark} [{'CACHE' if f.on_cache else 'ARRAY'}] {human(f.size):>10}  {f.rel}")

    def report(self, planner, user_filter=None, show_files=True):
        """On-Deck-Liste pro Benutzer: Filme, dann Serien mit ihren Folgen, dann was nicht ins Budget passt."""
        wanted = {u.strip().lower() for u in (user_filter or []) if u.strip()}
        users = sorted(planner.users.values(), key=lambda u: (u.server, u.name.lower()))
        if wanted:
            users = [u for u in users if u.name.lower() in wanted or u.uid.lower() in wanted]
            if not users:
                print(f"\nKein Benutzer passt auf: {', '.join(sorted(wanted))}")
                print("Bekannt: " + ", ".join(f"{u.name} ({u.uid})" for u in planner.users.values()))
                return
        shown = set()
        for u in users:
            head = f"=== {u.name} @ {u.server} ==="
            if planner.budget_mode and u.has_candidates():
                head += (f"  Budget: {human(u.bytes)} von {human(u.budget)}"
                         f" (Filme {human(u.movie_bytes)}, Serien {human(u.series_bytes)})")
            print(f"\n{head}")
            if not u.has_candidates():
                print("  (nichts on deck)")
                continue
            movies = [g for g in u.selected if g.series is None]
            series = [g for g in u.selected if g.series is not None]
            if movies:
                print(f"  Filme: {len(movies)} Einträge, {human(sum(g.size for g in movies))}")
                self.render_groups(movies, "•", show_files)
            if series:
                n_series = len({g.chain for g in series})
                print(f"  Serien: {n_series} Serie{'n' if n_series != 1 else ''}, {len(series)} Folgen, {human(sum(g.size for g in series))}")
                self.render_groups(series, "•", show_files)
            if u.skipped:
                print(f"  Nicht im Budget: {len(u.skipped)} Einträge, {human(sum(g.size for g in u.skipped))}"
                      "  (bleiben auf dem Array bzw. werden zurückgeräumt)")
                self.render_groups(u.skipped, "✗", show_files)
            for g in u.selected:
                for f in g.files:
                    shown.add(str(f.rel))
        files = [f for f in planner.files.values() if str(f.rel) in shown] if wanted else list(planner.files.values())
        on_c = sum(x.size for x in files if x.on_cache)
        on_a = sum(x.size for x in files if not x.on_cache)
        print(f"\n{'Auswahl' if wanted else 'Gesamt'}: {len(files)} Dateien – auf dem Cache {human(on_c)}, noch auf dem Array {human(on_a)}, zusammen {human(on_c + on_a)}")
        if not wanted and planner.budget_mode:
            print(f"Budget: {human(int(self.cfg['cache_budget_bytes']))} für {sum(1 for u in planner.users.values() if u.has_candidates())} aktive Benutzer")
        print(f"Exclude-Liste: {EXCLUDE_FILE} ({len(read_exclude())} Einträge)\n")

    def execute(self):
        state = "abgebrochen (Fehler)"
        try:
            rc = self._execute()
            state = "fertig"
            return rc
        finally:
            self.status.finish(state)

    def _execute(self):
        cfg = self.cfg
        log.info(f"=== EmbyCache {__version__} – Modus: {self.mode.upper()} ===")
        if unraid_mover_running():
            log.warning("Der reguläre Unraid-Mover läuft gerade – Cleanup könnte mit ihm kollidieren (nur Log-Meldungen, kein Datenrisiko)")

        sessions, sessions_ok = collect_sessions(cfg, log)
        planner = Planner(cfg)
        files = planner.run()
        log.info(f"On deck: {len(files)} Dateien, {human(sum(f.size for f in files))} "
                 f"(davon {sum(1 for f in files if f.on_cache)} bereits auf dem Cache)")
        if self.mode == "report":
            self.report(planner, self.user_filter, self.show_files)
            return 0

        # Referenz-Locations (Cache/Array sind global, Mappings spielen hier keine Rolle)
        loc = Locations(cfg, cfg["instances"][0]["_mappings"])
        current = {str(loc.on_cache(f.rel)) for f in files}
        protected = set()
        try:
            if planner.errors or not sessions_ok:
                log.warning("Planung unvollständig (Emby nicht vollständig erreichbar) – Cleanup wird übersprungen, "
                            "bisherige Exclude-Liste bleibt geschützt")
                protected.update(p for p in read_exclude() if Path(p).exists())
            else:
                self.cleanup(loc, current, sessions, protected)
            self.fill(loc, files, sessions)
        finally:
            if self.run_mode:
                on_cache = {str(loc.on_cache(f.rel)) for f in files if loc.on_cache(f.rel).exists()}
                write_exclude(on_cache | protected)
        if self.run_mode:
            excl = read_exclude()
            log.info(f"Ergebnis Cleanup:  {self.moved_back} von {self.cleanup_planned} Dateien aufs Array verschoben "
                     f"({human(self.moved_back_bytes)} von {human(self.to_array)})")
            log.info(f"Ergebnis Befüllen: {self.copied} von {self.fill_planned} Dateien auf den Cache kopiert "
                     f"({human(self.copied_bytes)} von {human(self.to_cache)})")
            log.info(f"Exclude-Liste: {len(excl)} Einträge"
                     + (f", davon {len(protected)} geschützt (laufen gerade oder konnten nicht verschoben werden)" if protected else ""))
        else:
            log.info(f"Dry-Run: {self.cleanup_planned} Dateien ({human(self.to_array)}) würden aufs Array, "
                     f"{self.fill_planned} Dateien ({human(self.to_cache)}) auf den Cache – nichts verändert")
        return 0


def main():
    parser = argparse.ArgumentParser(description="EmbyCache für Unraid", epilog="Details: siehe Kopf des Scripts")
    parser.add_argument("--run", action="store_true", help="Aktionen wirklich ausführen (Default: Dry-Run)")
    parser.add_argument("--show-on-deck", action="store_true", help="Nur die On-Deck-Liste anzeigen (pro Benutzer, nach Quelle)")
    parser.add_argument("--user", help="Report nur für diese Benutzer (Name oder ID, Komma-getrennt); nur mit --show-on-deck")
    parser.add_argument("--compact", action="store_true", help="Report ohne einzelne Dateien, nur Einträge")
    parser.add_argument("--status", action="store_true", help="Status des laufenden bzw. letzten Laufs anzeigen")
    parser.add_argument("--watch", type=float, nargs="?", const=2, metavar="SEK",
                        help="mit --status: alle SEK Sekunden neu anzeigen (Default 2), Ende mit Ctrl+C")
    args = parser.parse_args()
    if args.watch is not None and args.watch <= 0:
        parser.error("--watch braucht ein Intervall grösser als 0 Sekunden")
    if args.watch and not args.status:
        parser.error("--watch nur zusammen mit --status (in einer zweiten Shell, während --run läuft)")
    if args.status:
        if not args.watch:
            print(format_status())
            return 0
        try:
            while True:
                print("\033[2J\033[H" + format_status(), flush=True)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    mode = os.environ.get("EMBYCACHE_MODE", "dry").lower()
    if args.run:
        mode = "run"
    elif args.show_on_deck:
        mode = "report"
    if mode not in ("dry", "report", "run"):
        log.error(f"Ungültiger Modus: {mode}")
        return 2
    user_filter = args.user if args.user is not None else os.environ.get("EMBYCACHE_REPORT_USER")
    if user_filter and mode != "report":
        log.error("--user filtert nur die Anzeige und ist deshalb nur mit --show-on-deck erlaubt – "
                  "ein Lauf für einzelne Benutzer würde die Dateien der anderen zurückräumen")
        return 2

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error(str(e))
        return 1

    lock = acquire_lock(log)
    if lock is None:
        return 1
    try:
        return Runner(cfg, mode, user_filter.split(",") if user_filter else None, not args.compact).execute()
    except Exception:
        log.exception("Unerwarteter Fehler")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
