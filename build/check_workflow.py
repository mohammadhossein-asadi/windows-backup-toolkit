#!/usr/bin/env python3
"""Sanity-check the GitHub Actions workflow without running GitHub.

    python3 build/check_workflow.py

Parses the YAML, prints the triggers and steps, and runs `bash -n` on every
`run:` block (heredocs included) so shell syntax errors are caught locally.
Requires PyYAML (pip install pyyaml); exits with a clear message if missing.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-portable.yml"

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required for this check: python3 -m pip install pyyaml")

if not WORKFLOW.exists():
    sys.exit(f"Missing workflow: {WORKFLOW}")

data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
problems = []

print(f"workflow: {WORKFLOW.relative_to(WORKFLOW.parent.parent.parent)}")
print(f"triggers: {sorted(data.get(True, data.get('on', {})))}")

for job_name, job in data["jobs"].items():
    print(f"job {job_name} on {job.get('runs-on')}")
    for step in job["steps"]:
        name = step.get("name") or step.get("uses") or "(unnamed)"
        print(f"  - {name}")
        run = step.get("run")
        if not run:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write("set -euo pipefail\n" + run)
            path = fh.name
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        os.unlink(path)
        if result.returncode != 0:
            problems.append(f"{name}: {result.stderr.strip()}")

if problems:
    print("\nFAILED:")
    for problem in problems:
        print(f"  {problem}")
    sys.exit(1)

print("\nOK: YAML parsed and every run block passes bash -n")
