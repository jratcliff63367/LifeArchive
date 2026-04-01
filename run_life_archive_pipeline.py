#!/usr/bin/env python3
r"""
Run the Life Archive post-ingest analysis pipeline in a sensible order.

This wrapper intentionally does NOT try to be clever about per-script arguments.
It just launches the analysis scripts one after another, stops on the first failure,
and prints a clear summary.

Assumptions:
- Each underlying script already supports incremental reruns and will skip images
  it has already processed with the current model/version.
- You are running from: C:\Users\jratc\python-code
- Your .venv is already configured the same way you use it for the individual scripts.

Edit only the SCRIPT_COMMANDS list below if your filenames differ.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

WORKDIR = Path(r"C:\Users\jratc\python-code")

# Update these filenames once if your actual script names differ.
# Order chosen:
#   1. face detection
#   2. face expression
#   3. aesthetic scoring
#   4. AI summary
#   5. geolocation / reverse geocoding
#
# Rationale:
# - face expression often conceptually depends on face-oriented processing
# - aesthetic and AI summary are independent image analyses
# - geolocation last is convenient because it updates the Places experience
#   after the visual-analysis passes are already done
#
# If your actual filenames differ, just change the strings on the right side.
SCRIPT_COMMANDS = [
#    ("Rebuild Ingestion",     "ingest-photos.py"),
#    ("Face detection",        "face-detect-score.py"),
#    ("Face expression",       "face-expression-sidecar.py"),
#    ("Aesthetic scoring",     "image-aesthetic-score-clip.py"),
#    ("Technical scoring",     "technical-image-score.py"),
#    ("Semantic scoring",      "semantic-score.py"),
    #("AI summary",            "ai-summary-sidecar.py"),
    ("Geolocation",           "geotag_sidecar_opencage.py"),
    ("Derived score refresh", "derived-score-refresh.py"), 
]

# If True, stop immediately on the first failure.
FAIL_FAST = True


# ---------------------------------------------------------------------------
# IMPLEMENTATION
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    name: str
    command: list[str]
    returncode: int
    elapsed_seconds: float


def resolve_python() -> str:
    if sys.executable:
        return sys.executable
    return "python"


def normalize_command(command_text: str) -> list[str]:
    return [resolve_python(), *shlex.split(command_text)]


def run_step(name: str, command_text: str, workdir: Path) -> StepResult:
    command = normalize_command(command_text)
    start = time.time()

    print("=" * 90)
    print(f"[START] {name}")
    print(f"[CMD]   {' '.join(command)}")
    print(f"[DIR]   {workdir}")
    print("=" * 90)

    completed = subprocess.run(command, cwd=str(workdir))
    elapsed = time.time() - start

    print("-" * 90)
    print(f"[END]   {name}")
    print(f"[RC]    {completed.returncode}")
    print(f"[TIME]  {elapsed:.1f}s")
    print("-" * 90)
    print()

    return StepResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        elapsed_seconds=elapsed,
    )


def main() -> int:
    if not WORKDIR.exists():
        print(f"ERROR: Working directory does not exist: {WORKDIR}", file=sys.stderr)
        return 2

    missing = []
    for name, script_name in SCRIPT_COMMANDS:
        script_path = WORKDIR / script_name
        if not script_path.exists():
            missing.append((name, script_path))

    if missing:
        print("ERROR: One or more configured scripts were not found.", file=sys.stderr)
        for name, script_path in missing:
            print(f"  - {name}: {script_path}", file=sys.stderr)
        print()
        print("Edit SCRIPT_COMMANDS at the top of this file to match your actual filenames.")
        return 2

    results: list[StepResult] = []
    total_start = time.time()

    for name, script_name in SCRIPT_COMMANDS:
        result = run_step(name, script_name, WORKDIR)
        results.append(result)

        if result.returncode != 0 and FAIL_FAST:
            print(f"Pipeline stopped because '{name}' failed.")
            break

    total_elapsed = time.time() - total_start

    print("=" * 90)
    print("PIPELINE SUMMARY")
    print("=" * 90)
    for result in results:
        status = "OK" if result.returncode == 0 else f"FAILED ({result.returncode})"
        print(f"{result.name:20}  {status:12}  {result.elapsed_seconds:8.1f}s")

    print("-" * 90)
    print(f"Total elapsed: {total_elapsed:.1f}s")

    failed = [r for r in results if r.returncode != 0]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
