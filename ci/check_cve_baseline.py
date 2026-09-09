# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""A distro severity downgrade must not masquerade as runtime remediation."""

import json
import sys
from pathlib import Path

baseline = {
    line
    for line in Path("ci/runtime-cve-baseline.txt").read_text().splitlines()
    if line and not line.startswith("#")
}
report = json.loads(Path(sys.argv[1]).read_text())
# Preserve and narrowly validate assessments instead of silently dropping every
# ignored match. The workflow first executes the regressions supporting VEX.
vex = json.loads(Path("ci/runtime.openvex.json").read_text())
assessed = {
    (statement["vulnerability"]["name"], product["@id"])
    for statement in vex["statements"]
    if statement["status"] == "not_affected"
    and statement["justification"] == "vulnerable_code_not_present"
    for product in statement["products"]
}
for match in report.get("ignoredMatches", []):
    key = (match["vulnerability"]["id"], match["artifact"]["purl"])
    rules = match.get("appliedIgnoreRules", [])
    if (
        key not in assessed
        or not rules
        or any(
            rule.get("namespace") != "vex" or rule.get("vex-status") != "not_affected"
            for rule in rules
        )
    ):
        raise SystemExit(f"Unreviewed vulnerability suppression: {key}")

remaining = set()
for match in report["matches"]:
    identifiers = {match["vulnerability"]["id"]}
    identifiers.update(v["id"] for v in match.get("relatedVulnerabilities", []))
    remaining.update(identifiers & baseline)
if remaining:
    raise SystemExit(
        f"{len(remaining)} original High/Critical CVEs still matched, regardless of current severity:\n"
        + "\n".join(sorted(remaining))
    )
print(
    f"No unresolved matches for the {len(baseline)} original High/Critical CVEs; "
    f"{len(report.get('ignoredMatches', []))} exact-package VEX assessments retained"
)
