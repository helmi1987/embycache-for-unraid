#!/usr/bin/env python3
"""
start.py – Serial orchestrator for EmbyCache tools
=================================================
Runs the two stages **serially** (never in parallel):
  1) scripts/embycache_collect.py
  2) scripts/embycache_mover.py

Flags:
  --debug  → pass through to both scripts (verbose/dry‑run behaviour depends on the tool)
  --run    → pass through to both scripts
             • collect: writes embycache_mover_to_exclude.txt
             • mover:   performs copy/sync operations

Examples
  Dry‑run preview (no EXCLUDE write, no copy):
    python3 start.py --debug

  Real run (write EXCLUDE, perform move/sync):
    python3 start.py --run

  Real run with extra debug output:
    python3 start.py --run --debug
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
import logging
from datetime import datetime

# ───────────────────────── Paths / Setup ──────────────────────────────
ROOT      = Path(__file__).resolve().parent
SCRIPTS   = ROOT / "scripts"
LOG_DIR   = ROOT / "logs"; LOG_DIR.mkdir(exist_ok=True)
COLLECT   = SCRIPTS / "embycache_collect.py"
MOVER     = SCRIPTS / "embycache_mover.py"

for p in (COLLECT, MOVER):
    if not p.exists():
        print(f"ERROR: Script not found: {p}")
        sys.exit(1)

# ───────────────────────── CLI / Logging ──────────────────────────────
parser = argparse.ArgumentParser(description="Run EmbyCache collect + mover serially")
parser.add_argument("--debug", action="store_true", help="Pass --debug to both scripts")
parser.add_argument("--run",   action="store_true", help="Pass --run to both scripts")
ARGS = parser.parse_args()

log_file = LOG_DIR / f"start_{datetime.now():%Y%m%d_%H%M%S}.log"
logging.basicConfig(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("start")

# ───────────────────────── Helpers ────────────────────────────────────
def run_stage(cmd: list[str], title: str) -> int:
    log.info("=== %s ===", title)
    log.debug("Command: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=ROOT)
        rc = proc.returncode
    except Exception as e:
        log.exception("%s failed: %s", title, e)
        return 1
    if rc != 0:
        log.error("%s exited with code %d", title, rc)
    else:
        log.info("%s finished successfully", title)
    return rc

# ───────────────────────── Build commands ─────────────────────────────
py = sys.executable or "python3"
collect_cmd = [py, str(COLLECT)]
mover_cmd   = [py, str(MOVER)]

if ARGS.debug:
    collect_cmd.append("--debug")
    mover_cmd.append("--debug")
if ARGS.run:
    collect_cmd.append("--run")
    mover_cmd.append("--run")

# ───────────────────────── Run serially ───────────────────────────────
rc1 = run_stage(collect_cmd, "Collect stage")
if rc1 != 0:
    log.error("Aborting – collect stage failed.")
    sys.exit(rc1)

rc2 = run_stage(mover_cmd, "Mover stage")
if rc2 != 0:
    log.error("Finished with errors in mover stage.")
    sys.exit(rc2)

log.info("All stages completed successfully.")
