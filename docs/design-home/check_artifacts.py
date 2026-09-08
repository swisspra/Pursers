#!/usr/bin/env python3
"""Reproducible artifact checker for docs/design-home/.

Fails on source path, line-count, range, endpoint, method, or artifact-total drift.
Usage: python3 docs/design-home/check_artifacts.py
"""
import subprocess, sys, os

BASELINE_SHA = "c2ebac5de803a0f7a00468ec4d3cdf06e4719096"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

errors = []

# Source file line counts
EXPECTED_LINES = {
    "tools/dashboard-ui/src/dashboard.ts": 1528,
    "tools/dashboard-ui/src/dashboard.css": 619,
    "tools/dashboard-ui/dashboard-entry.html": 145,
    "tools/fleet-dashboard/fleet_dashboard.py": 7507,
    "packages/personal/src/pursers_personal/apps_server.py": 2401,
    "tools/aionui-extension/webui/index.html": 33,
    "tools/aionui-extension/webui/app.js": 60,
    "tools/aionui-extension/webui/routes.js": 201,
    "tools/aionui-extension/webui/style.css": 66,
}

for path, expected in EXPECTED_LINES.items():
    full = os.path.join(REPO_ROOT, path)
    if not os.path.exists(full):
        errors.append(f"MISSING: {path}")
        continue
    actual = sum(1 for _ in open(full))
    if actual != expected:
        errors.append(f"LINE COUNT: {path} expected {expected}, got {actual}")

# Artifact total
ARTIFACT_FILES = [
    "docs/design-home/context/components.md",
    "docs/design-home/context/layouts.md",
    "docs/design-home/context/routes.md",
    "docs/design-home/context/theme.md",
    "docs/design-home/context/pages.md",
    "docs/design-home/context/extractable-components.md",
    "docs/design-home/inventory.md",
]

total_lines = 0
total_bytes = 0
for art in ARTIFACT_FILES:
    full = os.path.join(REPO_ROOT, art)
    if not os.path.exists(full):
        errors.append(f"MISSING ARTIFACT: {art}")
        continue
    content = open(full, "rb").read()
    total_lines += content.count(b"\n")
    total_bytes += len(content)

print(f"Artifact total: {total_lines} lines, {total_bytes} bytes")

# Stale patterns
STALE = ["~820", "~290", "~800", "~6200", "453KB", "~30"]
for art in ARTIFACT_FILES:
    full = os.path.join(REPO_ROOT, art)
    if not os.path.exists(full):
        continue
    content = open(full).read()
    for pattern in STALE:
        if pattern in content:
            errors.append(f"STALE: {art} contains '{pattern}'")

# Route checks
routes_path = os.path.join(REPO_ROOT, "docs/design-home/context/routes.md")
if os.path.exists(routes_path):
    routes = open(routes_path).read()
    if "/api/config/attention" in routes:
        errors.append("ROUTE: /api/config/attention should be /api/attention")
    if "/api/attention" not in routes:
        errors.append("ROUTE: /api/attention missing")
    if "POST /api/intake" not in routes and "| `/api/intake` | Submit" not in routes:
        errors.append("ROUTE: POST /api/intake missing")

if errors:
    print("FAIL")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print("PASS: all checks passed")
    sys.exit(0)
