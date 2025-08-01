#!/usr/bin/env python3
"""start.py — wrapper to run *scripts/embycache_collect.py* and *scripts/embycache_mover.py* sequentially"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

# ───────────────────── Projekt‑Root & Skript-Pfade ─────────────────────
ROOT_DIR = Path(__file__).resolve().parent
COLLECT_SCRIPT = ROOT_DIR / "scripts" / "embycache_collect.py"
MOVER_SCRIPT   = ROOT_DIR / "scripts" / "embycache_mover.py"

# ───────────────────── Kommandozeilen-Flags ───────────────────────────
parser = argparse.ArgumentParser(description="Start EmbyCache collect, then run mover")
parser.add_argument("--run",   action="store_true", help="Run mover in run mode (otherwise dry-run)")
parser.add_argument("--debug", action="store_true", help="Run both scripts in debug mode")
# All other flags are forwarded to collect (except --run)
parser.add_argument("--skip-cache", action="store_true", help="Skip cache flag for collect")
ARGS = parser.parse_args()

# ───────────────────── Check if scripts exist ────────────────────
for script in (COLLECT_SCRIPT, MOVER_SCRIPT):
    if not script.exists():
        sys.stderr.write(f"Error: {script} not found.\n")
        sys.exit(1)

# ───────────────────── Build collect command ───────────────────────────
collect_cmd = [sys.executable, str(COLLECT_SCRIPT)]
if ARGS.debug:
    collect_cmd.append("--debug")
if ARGS.skip_cache:
    collect_cmd.append("--skip-cache")

# ───────────────────── Run Collect sequentiell execute ───────────────────
print(f"Starting Collect: {' '.join(collect_cmd)}")
ret = subprocess.call(collect_cmd)
if ret != 0:
    sys.stderr.write(f"Collect script failed mit Code {ret}\n")
    sys.exit(ret)

# ───────────────────── Build mover command ────────────────────────────
mover_cmd = [sys.executable, str(MOVER_SCRIPT)]
if ARGS.debug:
    mover_cmd.append("--debug")
if ARGS.run:
    mover_cmd.append("--run")

# ───────────────────── Run Mover sequentiell execute ─────────────────────
print(f"Starting Mover:  {' '.join(mover_cmd)}")
ret = subprocess.call(mover_cmd)
if ret != 0:
    sys.stderr.write(f"Mover script failed mit Code {ret}\n")
sys.exit(ret)
