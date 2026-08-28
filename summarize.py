#!/usr/bin/env python3
"""Print a compact summary of LocalJudgeAgent experiment records."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_records():
    records = []
    for path in sorted((ROOT / "runs").glob("*/record.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: skipped {path}: {exc}")
            continue
        records.append(record)
    return records


def oj_statuses(record):
    history = record.get("oj_history") or []
    if record.get("platform") == "codeforces":
        confirmed = [entry for entry in history if entry.get("submission_id") is not None]
        if confirmed:
            return [entry.get("status", "OJ_UNKNOWN") for entry in confirmed]
        oj = record.get("oj", {})
        return [oj.get("status", "OJ_UNKNOWN")] if (
            oj.get("submitted") and oj.get("submission_id") is not None) else []
    if history:
        return [entry.get("status", "OJ_UNKNOWN") for entry in history]
    status = record.get("oj", {}).get("status")
    return [status] if status else []


def main():
    records = load_records()
    headers = ("Problem", "Platform", "Difficulty/Rating", "FirstPass", "Repairs", "Sample", "OJ",
               "OJAttempts", "OJRepairs", "TotalTime")
    rows = []
    for record in records:
        passed = bool(record.get("final_sample_passed", False))
        sample_status = "SAMPLE_AC" if passed else "FAILED"
        statuses = oj_statuses(record)
        oj = statuses[-1] if statuses else str(record.get("oj", {}).get("status", "-"))
        oj_attempts = len(statuses)
        rows.append((
            str(record.get("problem_id", "?")),
            str(record.get("platform", "luogu")),
            str(record.get("rating") if record.get("platform") == "codeforces"
                else record.get("difficulty", "")),
            "YES" if record.get("first_generation_success", False) else "NO",
            str(record.get("repair_attempts", 0)),
            sample_status,
            oj,
            str(oj_attempts),
            str(record.get("oj_repair_attempts", 0)),
            f"{float(record.get('total_time_sec', 0)):.1f}s",
        ))
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))

    count = len(records)
    first_pass = sum(bool(record.get("first_generation_success", False)) for record in records)
    final_pass = sum(bool(record.get("final_sample_passed", False)) for record in records)
    avg_repairs = sum(float(record.get("repair_attempts", 0)) for record in records) / count if count else 0
    avg_total = sum(float(record.get("total_time_sec", 0)) for record in records) / count if count else 0
    avg_model = sum(float(record.get("model_time_sec", 0)) for record in records) / count if count else 0
    evaluated = [record for record in records if oj_statuses(record)]
    first_oj_ac = sum(oj_statuses(record)[0] == "OJ_AC" for record in evaluated)
    final_oj_ac = sum(oj_statuses(record)[-1] == "OJ_AC" for record in evaluated)
    avg_oj_attempts = sum(len(oj_statuses(record)) for record in evaluated) / len(evaluated) if evaluated else 0
    avg_oj_repairs = sum(record.get("oj_repair_attempts", 0) for record in evaluated) / len(evaluated) if evaluated else 0
    print()
    print(f"Total problems: {count}")
    print(f"First-generation success: {first_pass}/{count}")
    print(f"Final sample success: {final_pass}/{count}")
    print(f"Average repairs: {avg_repairs:.2f}")
    print(f"Average total time: {avg_total:.1f}s")
    print(f"Average model time: {avg_model:.1f}s")
    print(f"OJ evaluated problems: {len(evaluated)}")
    print(f"First OJ AC: {first_oj_ac}/{len(evaluated)}")
    print(f"Final OJ AC: {final_oj_ac}/{len(evaluated)}")
    print(f"Average OJ attempts: {avg_oj_attempts:.2f}")
    print(f"Average OJ repairs: {avg_oj_repairs:.2f}")


if __name__ == "__main__":
    main()
