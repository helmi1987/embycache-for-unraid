#!/usr/bin/env python3
"""
EmbyCache – holt «Weiterschauen»-Inhalte, die nächsten Folgen und Favoriten-Serien der Emby-Benutzer
vom Unraid-Array auf den Cache-Pool und räumt nicht mehr benötigte Dateien wieder aufs Array.

Ablauf pro Lauf:
  1. Sessions abfragen – was gerade läuft, wird nie angefasst
  2. On-Deck-Liste berechnen (Resume-Einträge, nächste Folgen in Serienreihenfolge, Favoriten)
       Zähl-Modus (Default): number_episodes Folgen pro Serie, max_resume_items Einträge pro Benutzer
       Budget-Modus (cache_budget gesetzt, z.B. "2.5T"): das Budget wird fair auf die aktiven Benutzer verteilt
       (feste Budgets pro Benutzer möglich, ungenutzter Anteil fliesst an die anderen); pro Benutzer werden
       Filme und Serien nach Bytes im Verhältnis movie_share_percent gefüllt, Serien reihum eine Folge pro
       Runde – kleine Folgen ergeben viele, grosse wenige. Was nicht ins Budget passt, bleibt auf dem Array.
  3. Cleanup: Dateien aus der alten Exclude-Liste, die nicht mehr on deck sind -> Unraid move-Binary (Cache -> Array)
  4. Befüllen: On-Deck-Dateien, die noch auf dem Array liegen -> auf den Cache. Werkzeug per fill_tool:
       rsync (Default): rsync -aAX --numeric-ids /mnt/user0/<rel> /mnt/<pool>/<rel> – exakt wie das Original;
                        Quelle erst nach Rückgabecode 0 und Grössenvergleich löschen. array_source = disk liest
                        stattdessen vom echten /mnt/diskN-Pfad (schneller, am FUSE vorbei) – optional
       mover:           Array-Pfade ans Unraid move-Binary pipen (wie der Stock-Mover bei «prefer»-Shares);
                        das Binary wählt den Pool aus der Share-Konfiguration – vorher mit einer Datei testen
     Der Freiplatz-Floor wird in beiden Fällen vorher geprüft
  5. Exclude-Liste neu schreiben (alle On-Deck-Dateien, die jetzt auf dem Cache liegen)

Aufruf:
  python3 embycache_run.py                 Dry-Run: zeigt alle geplanten Aktionen, verändert nichts
  python3 embycache_run.py --show-on-deck  Report: On-Deck-Liste mit Speicherort, keine Dateioperationen
  python3 embycache_run.py --run           Scharf

Umgebungsvariablen (überschreiben Config bzw. Defaults; CLI-Flags haben Vorrang vor EMBYCACHE_MODE):
  EMBYCACHE_MODE              dry | report | run
  EMBYCACHE_DIR               Arbeitsverzeichnis (Settings, Exclude-Liste, Lock, logs/); Default: Script-Verzeichnis
  EMBYCACHE_CONFIG            Pfad zur Settings-JSON
  EMBYCACHE_LOG_LEVEL         DEBUG | INFO | WARNING (Default INFO)
  EMBYCACHE_MIN_FREE_PERCENT  Mindest-Freiplatz auf dem Cache in Prozent (überschreibt min_free_percent)
  EMBYCACHE_MOVER_DEBUG       0–3, Parameter -d für das move-Binary (überschreibt mover_debug_level)
  EMBYCACHE_RSYNC_ARGS        vollständige rsync-Optionen, Default "-aAX --numeric-ids" (überschreibt rsync_args)
  EMBYCACHE_FILL_TOOL         rsync | mover (überschreibt fill_tool)
  EMBYCACHE_CACHE_BUDGET      z.B. "2.5T" (überschreibt cache_budget; "" = Zähl-Modus)

Beispiel User Scripts (Unraid):  cd /mnt/user/system/scripts/embycache && python3 embycache_run.py --run
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from embycache_lib import (
    ConfigError, EmbyApi, Locations, acquire_lock, collect_sessions, detect_mover_bin, free_percent_after,
    human, is_playing, load_config, read_exclude, remove_empty_parents, run_mover, setup_logging,
    unraid_mover_running, write_exclude, EXCLUDE_FILE,
)

log = setup_logging("EmbyCache", "embycache.log")


class OnDeckFile:
    __slots__ = ("rel", "size", "on_cache", "reason")

    def __init__(self, rel, size, on_cache, reason):
        self.rel, self.size, self.on_cache, self.reason = rel, size, on_cache, reason


# --------------------------------------------------------------------------- Planung
class Group:
    """Atomare Planungseinheit: alle Dateien eines Items (Film + Untertitel + Extras, Folge + srt …)."""
    __slots__ = ("files", "size", "category", "chain", "reason")

    def __init__(self, files, category, chain, reason):
        self.files, self.category, self.chain, self.reason = files, category, chain, reason
        self.size = sum(f.size for f in files)


class UserPlan:
    """Kandidaten und Auswahl eines Benutzers (pro Instanz)."""

    def __init__(self, key, name):
        self.key, self.name = key, name
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

    def make_group(self, loc, item, category, chain, reason):
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
        return Group(files, category, chain, reason) if files else None

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
        up = UserPlan(f"{name}:{uid}", uname)
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
                g = self.make_group(loc, item, "movie", f"movie:{item.get('Id')}", f"{uname}: Weiterschauen «{item.get('Name', '?')}»")
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
            g = self.make_group(loc, item, "series", f"series:{sid}", f"{uname}: Weiterschauen «{sname} – {item.get('Name', '?')}»")
            if g:
                chain.append(g)
            try:
                for ep in self.next_episodes_after(api, uid, item):
                    g = self.make_group(loc, ep, "series", f"series:{sid}", f"{uname}: nächste Folge von «{sname}»")
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
                        g = self.make_group(loc, ep, "series", f"series:{sid}", f"{uname}: Favorit «{series.get('Name', '?')}»")
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
        explicit = {u.key: user_budgets[u.key.split(":", 1)[1]] for u in active if u.key.split(":", 1)[1] in user_budgets}
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
    def __init__(self, cfg, mode):
        self.cfg = cfg
        self.mode = mode  # dry | report | run
        self.run_mode = mode == "run"
        self.to_cache = self.to_array = 0
        self.copied = self.moved_back = 0

    def cleanup(self, loc, current, sessions, protected):
        """Alte Exclude-Einträge, die nicht mehr on deck sind -> move-Binary (Cache -> Array)."""
        previous = read_exclude()
        candidates = []
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
            try:
                self.to_array += path.stat().st_size
            except OSError:
                continue
            log.info(f"[{'MOVE' if self.run_mode else 'PLAN:'} -> ARRAY] {rel}")
            candidates.append(p)
        if not candidates:
            return
        log.info(f"Cleanup: {len(candidates)} Dateien ({human(self.to_array)}) vom Cache aufs Array")
        if not self.run_mode:
            return
        mover = detect_mover_bin(self.cfg)
        if not mover:
            log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen) – Cleanup übersprungen")
            protected.update(candidates)
            return
        run_mover(mover, candidates, self.cfg["mover_debug_level"], log)
        for p in candidates:
            if Path(p).exists():
                log.warning(f"Noch auf dem Cache (in Benutzung oder Ziel existiert bereits?): {p}")
                protected.add(p)
            else:
                self.moved_back += 1
                remove_empty_parents(Path(p).parent, loc.cache, log)

    def fill(self, loc, files, sessions):
        """On-Deck-Dateien vom Array auf den Cache – per rsync (Quelle erst nach Prüfung löschen) oder per move-Binary."""
        rsync_args = list(self.cfg["rsync_args"])
        from_disk = self.cfg["array_source"] == "disk"
        min_free = float(self.cfg["min_free_percent"])
        use_mover = self.cfg["fill_tool"] == "mover"
        label = "MOVE" if use_mover else "COPY"
        mover_batch = []  # (OnDeckFile, src) für fill_tool=mover
        failures = 0
        for f in sorted(files, key=lambda x: str(x.rel)):
            if f.on_cache:
                continue
            if failures >= 3:
                log.error("Drei rsync-Fehler in Folge – Befüllen abgebrochen, Ursache im Log prüfen")
                break
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
            check_path = share_root if share_root.is_dir() else loc.cache
            free_after = free_percent_after(check_path, f.size)
            if free_after < min_free:
                log.warning(f"[SKIP: Freiplatz] {f.rel} ({human(f.size)}) – danach nur {free_after:.1f} % frei, Minimum {min_free:g} %")
                continue
            sources = loc.on_disk(f.rel) if from_disk else []
            if len(sources) > 1:
                log.warning(f"Datei liegt auf mehreren Disks (Duplikat im Array!): {', '.join(map(str, sources))} – nehme die erste")
            src = sources[0] if sources else loc.on_array(f.rel)
            if not src.is_file():
                log.warning(f"Quelle nicht gefunden: {src}")
                continue
            self.to_cache += f.size
            log.info(f"[{label if self.run_mode else 'PLAN:'} -> CACHE] {f.rel} ({human(f.size)}) – {f.reason}")
            if not self.run_mode:
                continue
            if use_mover:
                mover_batch.append((f, src))
                continue

            dst = loc.on_cache(f.rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["rsync", *rsync_args, str(src), str(dst)]
            log.debug("rsync-Aufruf: " + " ".join(cmd))
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                failures += 1
                log.error(f"rsync-Fehler (Code {res.returncode}) bei {f.rel}: {res.stderr.strip() or res.stdout.strip()}")
                log.error("Quelle bleibt auf dem Array; Ziel prüfen, sonst liegt die Datei doppelt")
                continue
            failures = 0
            try:
                if dst.stat().st_size != src.stat().st_size:
                    log.error(f"Grösse stimmt nicht überein nach rsync, Quelle bleibt: {f.rel}")
                    continue
                src.unlink()
            except OSError as e:
                log.error(f"Quelle konnte nicht gelöscht werden (Datei liegt jetzt doppelt!): {src} ({e})")
                continue
            self.copied += 1
            f.on_cache = True
            remove_empty_parents(src.parent, self._disk_root(src, loc, bool(sources)), log)

        if mover_batch:
            self.fill_with_mover(loc, mover_batch)

    def fill_with_mover(self, loc, batch):
        """Array -> Cache über das Unraid move-Binary (ein Aufruf für alle Dateien), danach Ergebnis prüfen."""
        mover = detect_mover_bin(self.cfg)
        if not mover:
            log.error("Kein Unraid move-Binary gefunden (mover_bin in der Config setzen) – nichts kopiert")
            return
        log.info(f"Befüllen: {len(batch)} Dateien ({human(sum(f.size for f, _ in batch))}) per move-Binary Array -> Cache")
        run_mover(mover, [str(src) for _, src in batch], self.cfg["mover_debug_level"], log)
        for f, src in batch:
            dst = loc.on_cache(f.rel)
            if dst.exists() and not src.exists():
                self.copied += 1
                f.on_cache = True
                remove_empty_parents(src.parent, self._disk_root(src, loc, src != loc.on_array(f.rel)), log)
            elif dst.exists() and src.exists():
                log.error(f"Datei liegt jetzt doppelt (Quelle nicht entfernt): {f.rel}")
            else:
                log.warning(f"Nicht auf den Cache verschoben (in Benutzung, oder das Binary kennt keinen Pool für diesen Share?): {f.rel}")
        if self.copied == 0:
            log.warning("Das move-Binary hat nichts auf den Cache verschoben – fill_tool=rsync verwenden oder mover_debug_level=1 setzen und Log prüfen")

    @staticmethod
    def _disk_root(src, loc, from_disk):
        """/mnt/disk3/Filme/X/x.mkv -> /mnt/disk3; bei FUSE-Fallback array_path."""
        if not from_disk:
            return loc.array
        depth = len(Path(loc.disks_glob).parts)
        return Path(*src.parts[:depth]) if len(src.parts) > depth else loc.array

    def report(self, files):
        print(f"\n--- ON DECK ({len(files)} Dateien) ---")
        for f in sorted(files, key=lambda x: str(x.rel)):
            where = "CACHE" if f.on_cache else "ARRAY"
            print(f"[{where}] {human(f.size):>10}  {f.rel}    ({f.reason})")
        on_c = sum(x.size for x in files if x.on_cache)
        on_a = sum(x.size for x in files if not x.on_cache)
        print(f"\nAuf dem Cache: {human(on_c)} | Noch auf dem Array: {human(on_a)} | Gesamt: {human(on_c + on_a)}")
        print(f"Exclude-Liste: {EXCLUDE_FILE} ({len(read_exclude())} Einträge)\n")

    def execute(self):
        cfg = self.cfg
        log.info(f"=== EmbyCache Modus: {self.mode.upper()} ===")
        if unraid_mover_running():
            log.warning("Der reguläre Unraid-Mover läuft gerade – Cleanup könnte mit ihm kollidieren (nur Log-Meldungen, kein Datenrisiko)")

        sessions, sessions_ok = collect_sessions(cfg, log)
        planner = Planner(cfg)
        files = planner.run()
        log.info(f"On deck: {len(files)} Dateien, {human(sum(f.size for f in files))} "
                 f"(davon {sum(1 for f in files if f.on_cache)} bereits auf dem Cache)")
        if self.mode == "report":
            self.report(files)
            return 0

        # Referenz-Locations (Cache/Array sind global, Mappings spielen hier keine Rolle)
        loc = Locations(cfg, cfg["instances"][0]["_mappings"])
        current = {str(loc.on_cache(f.rel)) for f in files}
        protected = set()
        exclude_written = False
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
                exclude_written = True
        log.info(f"Statistik: -> Cache {human(self.to_cache)} ({self.copied} kopiert) | "
                 f"-> Array {human(self.to_array)} ({self.moved_back} verschoben)"
                 + (f" | Exclude-Liste: {len(read_exclude())} Einträge" if exclude_written else " | Dry-Run: nichts verändert"))
        return 0


def main():
    parser = argparse.ArgumentParser(description="EmbyCache für Unraid", epilog="Details: siehe Kopf des Scripts")
    parser.add_argument("--run", action="store_true", help="Aktionen wirklich ausführen (Default: Dry-Run)")
    parser.add_argument("--show-on-deck", action="store_true", help="Nur die On-Deck-Liste anzeigen")
    args = parser.parse_args()
    mode = os.environ.get("EMBYCACHE_MODE", "dry").lower()
    if args.run:
        mode = "run"
    elif args.show_on_deck:
        mode = "report"
    if mode not in ("dry", "report", "run"):
        log.error(f"Ungültiger Modus: {mode}")
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
        return Runner(cfg, mode).execute()
    except Exception:
        log.exception("Unerwarteter Fehler")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
