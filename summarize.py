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


def main():
    records = load_records()
    headers = ("Problem", "Difficulty", "FirstPass", "Repairs", "Result", "TotalTime")
    rows = []
    for record in records:
        passed = bool(record.get("final_sample_passed", False))
        status = record.get("final_status") or ("SAMPLE_AC" if passed else "FAILED")
        rows.append((
            str(record.get("problem_id", "?")),
            str(record.get("difficulty", "")),
            "YES" if record.get("first_generation_success", False) else "NO",
            str(record.get("repair_attempts", 0)),
            status,
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
    print()
    print(f"Total problems: {count}")
    print(f"First-generation success: {first_pass}/{count}")
    print(f"Final sample success: {final_pass}/{count}")
    print(f"Average repairs: {avg_repairs:.2f}")
    print(f"Average total time: {avg_total:.1f}s")
    print(f"Average model time: {avg_model:.1f}s")


if __name__ == "__main__":
    main()
