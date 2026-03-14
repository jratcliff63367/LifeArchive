from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

RUN_TECHNICAL = True
RUN_FACE = True
RUN_AESTHETIC = True
RUN_SEMANTIC = False
RUN_HERO = False

STOP_ON_ERROR = True

SCRIPT_DIR = Path(__file__).resolve().parent

TECHNICAL_SCRIPT = SCRIPT_DIR / "technical-image-score.py"
FACE_SCRIPT = SCRIPT_DIR / "face-detect-score.py"
AESTHETIC_SCRIPT = SCRIPT_DIR / "image-aesthetic-score-clip.py"
SEMANTIC_SCRIPT = SCRIPT_DIR / "semantic-score.py"
HERO_SCRIPT = SCRIPT_DIR / "hero-score.py"

# Optional extra arguments per script
TECHNICAL_ARGS = []
FACE_ARGS = []
AESTHETIC_ARGS = []
SEMANTIC_ARGS = []
HERO_ARGS = []

# ============================================================
# IMPLEMENTATION
# ============================================================

def run_step(name: str, script_path: Path, enabled: bool, extra_args: list[str]) -> bool:
    if not enabled:
        print(f"[SKIP] {name}")
        return True

    if not script_path.exists():
        print(f"[ERROR] {name}: script not found: {script_path}")
        return False

    cmd = [sys.executable, str(script_path), *extra_args]

    print("=" * 80)
    print(f"[START] {name}")
    print(f"Script : {script_path}")
    print(f"Command: {' '.join(cmd)}")

    start = time.time()

    result = subprocess.run(cmd)

    elapsed = time.time() - start

    if result.returncode == 0:
        print(f"[DONE] {name} in {elapsed / 60:.2f} minutes")
        return True

    print(f"[FAIL] {name} exited with code {result.returncode} after {elapsed / 60:.2f} minutes")
    return False


def main() -> int:
    steps = [
        ("Technical Image Score", TECHNICAL_SCRIPT, RUN_TECHNICAL, TECHNICAL_ARGS),
        ("Face Detect Score", FACE_SCRIPT, RUN_FACE, FACE_ARGS),
        ("Aesthetic Score", AESTHETIC_SCRIPT, RUN_AESTHETIC, AESTHETIC_ARGS),
        ("Semantic Score", SEMANTIC_SCRIPT, RUN_SEMANTIC, SEMANTIC_ARGS),
        ("Hero Score", HERO_SCRIPT, RUN_HERO, HERO_ARGS),
    ]

    for name, script_path, enabled, extra_args in steps:
        ok = run_step(name, script_path, enabled, extra_args)
        if not ok and STOP_ON_ERROR:
            print("[ABORT] Stopping due to failure.")
            return 1

    print("=" * 80)
    print("All enabled steps completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())