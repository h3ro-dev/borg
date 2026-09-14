"""Stage this host's exact native additions; never apply or change a runtime file."""
import argparse
import hashlib
import json
from pathlib import Path

from .install import Installer


def stage_host(machine, plans, staging_dir):
    installer = Installer(staging_dir)
    results = []
    for entry in plans:
        if entry["machine"] != machine:
            continue
        target = Path(entry["target"])
        before = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        try:
            plan = installer.plan(entry["runtime"], target, changes=entry["changes"], format=entry["format"])
            staged = installer.stage(plan)
            after = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
            if before != after:
                raise RuntimeError("source_changed_during_stage")
            results.append({"target": str(target), "runtime": entry["runtime"], "state": "STAGED_NOT_APPLIED",
                            "source_unchanged": True, "baseline_sha256": before, "stage": staged})
        except Exception as exc:
            # Never print a source parser exception: it can contain private values.
            results.append({"target": str(target), "runtime": entry["runtime"],
                            "state": "NOT_STAGED", "error_type": type(exc).__name__})
    return {"machine": machine, "targets": len(results), "staged": sum(r["state"] == "STAGED_NOT_APPLIED" for r in results),
            "live_configuration_changed": False, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--plans", required=True)
    parser.add_argument("--staging-dir", required=True)
    args = parser.parse_args()
    result = stage_host(args.machine, json.loads(Path(args.plans).read_text()), args.staging_dir)
    destination = Path(args.staging_dir) / "STAGING-RECEIPT.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"machine": result["machine"], "targets": result["targets"], "staged": result["staged"],
                      "receipt": str(destination), "live_configuration_changed": False}))
    return 0 if result["targets"] == result["staged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
