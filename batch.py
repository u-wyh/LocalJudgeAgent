#!/usr/bin/env python3
"""Sequential, resumable LocalJudgeAgent benchmark runner."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODEL = "gpt-oss:20b"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
FINAL_STATUSES = {"SAMPLE_AC", "FAILED"}
MIN_FREE_BYTES = 512 * 1024 * 1024


class BatchSystemError(RuntimeError):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_benchmark(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("platform") != "codeforces" or not isinstance(data.get("problems"), list):
        raise ValueError("benchmark must contain a Codeforces problems list")
    seen = set()
    for index, item in enumerate(data["problems"], 1):
        problem_id = item.get("id", "")
        if not re.fullmatch(r"CF\d+[A-Za-z][A-Za-z0-9]*", problem_id):
            raise ValueError(f"invalid Codeforces ID at position {index}: {problem_id}")
        normalized = problem_id.upper()
        if normalized in seen:
            raise ValueError(f"duplicate problem ID: {problem_id}")
        seen.add(normalized)
        if not isinstance(item.get("expected_rating"), int):
            raise ValueError(f"missing expected_rating for {problem_id}")
    return data


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=5) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise BatchSystemError(f"Ollama is unavailable: {exc}") from exc
    return {item.get("name") or item.get("model") for item in payload.get("models", [])}


def system_preflight():
    if MODEL not in ollama_models():
        raise BatchSystemError(f"required model is not installed: {MODEL}")
    if shutil.which("g++") is None:
        raise BatchSystemError("g++ is unavailable")
    if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
        raise BatchSystemError("less than 512 MiB disk space remains")


def unique_batch_dir(name):
    base = ROOT / "batch_runs" / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}_{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def relative(path):
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def find_run_dir(stdout_text, problem_id, before):
    matches = re.findall(r"^\[Record\]\s+(.+)/record\.json\s*$", stdout_text, re.MULTILINE)
    if matches:
        return Path(matches[-1]).resolve()
    prefix = f"{problem_id.upper()}_"
    after = {path.resolve() for path in (ROOT / "runs").glob(f"{prefix}*") if path.is_dir()}
    created = sorted(after - before, key=lambda path: path.stat().st_mtime)
    return created[-1] if created else None


def result_from_run(order, item, return_code, run_dir, stdout_path, stderr_path):
    result = {
        "order": order, "problem_id": item["id"],
        "expected_rating": item["expected_rating"], "rating": None,
        "status": "FAILED", "first_generation_success": False,
        "repair_attempts": 0, "model_time_sec": 0, "total_time_sec": 0,
        "run_dir": relative(run_dir) if run_dir else None,
        "stdout_path": relative(stdout_path), "stderr_path": relative(stderr_path),
        "agent_return_code": return_code,
    }
    if not run_dir or not (run_dir / "record.json").exists():
        result["failure_reason"] = "AGENT_RECORD_MISSING"
        return result
    try:
        record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["failure_reason"] = f"INVALID_AGENT_RECORD: {exc}"
        return result
    result.update({
        "rating": record.get("rating"),
        "status": "SAMPLE_AC" if record.get("final_sample_passed") else "FAILED",
        "first_generation_success": bool(record.get("first_generation_success")),
        "repair_attempts": int(record.get("repair_attempts", 0)),
        "model_time_sec": float(record.get("model_time_sec", 0)),
        "total_time_sec": float(record.get("total_time_sec", 0)),
        "failure_reason": record.get("failure_reason"),
    })
    if result["rating"] != item["expected_rating"]:
        result["rating_warning"] = (
            f"expected {item['expected_rating']}, Codeforces API returned {result['rating']}")
    return result


def summary_for(record):
    problems = [item for item in record["problems"] if item.get("status") in FINAL_STATUSES]
    count = len(problems)
    sample_ac = sum(item["status"] == "SAMPLE_AC" for item in problems)
    first_pass = sum(item.get("first_generation_success", False) for item in problems)
    average = lambda key: (sum(float(item.get(key, 0)) for item in problems) / count if count else 0)
    ratings = {}
    for item in problems:
        rating = item.get("rating")
        group = ratings.setdefault(str(rating), [])
        group.append(item)
    by_rating = []
    for rating, items in sorted(
            ratings.items(), key=lambda pair: (pair[0] == "None",
                                                int(pair[0]) if pair[0] != "None" else 0)):
        size = len(items)
        by_rating.append({
            "rating": None if rating == "None" else int(rating), "problems": size,
            "first_pass": sum(item.get("first_generation_success", False) for item in items),
            "sample_ac": sum(item["status"] == "SAMPLE_AC" for item in items),
            "average_repairs": sum(item.get("repair_attempts", 0) for item in items) / size,
            "average_model_time_sec": sum(item.get("model_time_sec", 0) for item in items) / size,
        })
    return {
        "benchmark": record["benchmark"], "generated_at": now_iso(),
        "overall": {
            "total": count, "sample_ac": sample_ac, "failed": count - sample_ac,
            "first_generation_success": first_pass,
            "final_sample_success_rate": sample_ac / count if count else 0,
            "average_repairs": average("repair_attempts"),
            "average_model_time_sec": average("model_time_sec"),
            "average_total_time_sec": average("total_time_sec"),
            "total_wall_time_sec": float(record.get("wall_time_sec", 0)),
        },
        "by_rating": by_rating,
    }


def print_summary(summary):
    overall = summary["overall"]
    print("\nBatch summary")
    print(f"Total: {overall['total']}")
    print(f"SAMPLE_AC: {overall['sample_ac']}")
    print(f"FAILED: {overall['failed']}")
    print(f"First-generation success: {overall['first_generation_success']}/{overall['total']}")
    print(f"Final sample success rate: {overall['final_sample_success_rate']:.1%}")
    print(f"Average repairs: {overall['average_repairs']:.2f}")
    print(f"Average model time: {overall['average_model_time_sec']:.1f}s")
    print(f"Average total time: {overall['average_total_time_sec']:.1f}s")
    print(f"Total wall time: {overall['total_wall_time_sec']:.1f}s")
    print("\nRating  Problems  FirstPass  SampleAC  AvgRepairs  AvgModelTime")
    for row in summary["by_rating"]:
        print(f"{str(row['rating']):<7} {row['problems']:<9} {row['first_pass']:<10} "
              f"{row['sample_ac']:<9} {row['average_repairs']:<11.2f} "
              f"{row['average_model_time_sec']:.1f}s")


def checkpoint(batch_dir, record, session_start, base_wall):
    record["wall_time_sec"] = round(base_wall + time.perf_counter() - session_start, 6)
    atomic_json(batch_dir / "batch_record.json", record)


def run_batch(benchmark_path, benchmark, resume_dir=None, limit=None):
    session_start = time.perf_counter()
    if resume_dir:
        batch_dir = resume_dir.resolve()
        record = json.loads((batch_dir / "batch_record.json").read_text(encoding="utf-8"))
        expected_ids = [item["id"] for item in benchmark["problems"]]
        if record.get("benchmark") != benchmark["name"] or record.get("problem_ids") != expected_ids:
            raise ValueError("resume directory does not match this benchmark")
        stored_limit = record.get("limit")
        if limit is not None and stored_limit != limit:
            raise ValueError(f"resume limit must remain {stored_limit}")
        limit = stored_limit if limit is None else limit
        record["status"] = "RUNNING"
        record["resumed_at"] = now_iso()
    else:
        batch_dir = unique_batch_dir(benchmark["name"])
        record = {
            "benchmark": benchmark["name"], "benchmark_file": relative(benchmark_path),
            "problem_ids": [item["id"] for item in benchmark["problems"]],
            "started_at": now_iso(), "finished_at": None, "status": "RUNNING",
            "current_index": 0, "total": len(benchmark["problems"]),
            "limit": limit, "problems": [], "wall_time_sec": 0,
        }
    base_wall = float(record.get("wall_time_sec", 0))
    selected = benchmark["problems"][:limit] if limit is not None else benchmark["problems"]
    log_path = batch_dir / "batch.log"
    checkpoint(batch_dir, record, session_start, base_wall)
    print("=" * 40)
    print("LocalJudgeAgent Batch Benchmark")
    print(benchmark["name"])
    print(f"Batch directory: {relative(batch_dir)}\n")
    completed_orders = {entry["order"] for entry in record["problems"]
                        if entry.get("status") in FINAL_STATUSES}
    if any(order not in completed_orders for order in range(1, len(selected) + 1)):
        try:
            system_preflight()
        except BatchSystemError:
            record["status"] = "SYSTEM_ERROR"
            checkpoint(batch_dir, record, session_start, base_wall)
            raise

    for order, item in enumerate(selected, 1):
        existing = next((entry for entry in record["problems"] if entry["order"] == order), None)
        if existing and existing.get("status") in FINAL_STATUSES:
            print(f"[{order}/{len(selected)}] {item['id']} already completed: {existing['status']}")
            continue
        if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
            record["status"] = "SYSTEM_ERROR"
            checkpoint(batch_dir, record, session_start, base_wall)
            raise BatchSystemError("less than 512 MiB disk space remains")
        print(f"[{order}/{len(selected)}] {item['id']} expected_rating={item['expected_rating']}")
        running = {
            "order": order, "problem_id": item["id"],
            "expected_rating": item["expected_rating"], "status": "RUNNING",
            "started_at": now_iso(),
        }
        if existing:
            record["problems"][record["problems"].index(existing)] = running
        else:
            record["problems"].append(running)
        record["current_index"] = order
        checkpoint(batch_dir, record, session_start, base_wall)
        stdout_path = batch_dir / f"{order:03d}_{item['id']}.stdout.log"
        stderr_path = batch_dir / f"{order:03d}_{item['id']}.stderr.log"
        before = {path.resolve() for path in (ROOT / "runs").glob(f"{item['id'].upper()}_*")}
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, \
                    stderr_path.open("w", encoding="utf-8") as stderr:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "agent.py"), item["id"]],
                    cwd=ROOT, stdout=stdout, stderr=stderr, text=True, check=False)
        except KeyboardInterrupt:
            running.update({"status": "INTERRUPTED", "finished_at": now_iso(),
                            "stdout_path": relative(stdout_path),
                            "stderr_path": relative(stderr_path)})
            record["status"] = "INTERRUPTED"
            checkpoint(batch_dir, record, session_start, base_wall)
            print(f"\n[Interrupted] {item['id']}; resume will rerun this problem.")
            raise
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n===== [{order}] {item['id']} stdout =====\n{stdout_text}")
            log.write(f"\n===== [{order}] {item['id']} stderr =====\n{stderr_text}")
        run_dir = find_run_dir(stdout_text, item["id"], before)
        result = result_from_run(
            order, item, completed.returncode, run_dir, stdout_path, stderr_path)
        result["started_at"] = running["started_at"]
        result["finished_at"] = now_iso()
        record["problems"][record["problems"].index(running)] = result
        checkpoint(batch_dir, record, session_start, base_wall)
        print(f"[Result] {result['status']}")
        print(f"[Model] {result['model_time_sec']:.1f}s")
        print(f"[Repairs] {result['repair_attempts']}")
        if result.get("rating_warning"):
            print(f"[Warning] {result['rating_warning']}")
        done = [entry for entry in record["problems"] if entry.get("status") in FINAL_STATUSES]
        print(f"Progress: {len(done)}/{len(selected)}")
        print(f"SAMPLE_AC: {sum(x['status'] == 'SAMPLE_AC' for x in done)}")
        print(f"FAILED: {sum(x['status'] == 'FAILED' for x in done)}")
        print(f"FirstPass: {sum(x.get('first_generation_success', False) for x in done)}\n")
        if completed.returncode != 0:
            try:
                ollama_models()
            except BatchSystemError:
                record["status"] = "SYSTEM_ERROR"
                checkpoint(batch_dir, record, session_start, base_wall)
                raise

    record["status"] = "COMPLETE"
    record["current_index"] = len(selected)
    record["finished_at"] = now_iso()
    checkpoint(batch_dir, record, session_start, base_wall)
    summary = summary_for(record)
    atomic_json(batch_dir / "batch_summary.json", summary)
    print_summary(summary)
    print(f"\nBatch record: {relative(batch_dir / 'batch_record.json')}")
    return batch_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.dry_run and args.resume:
        parser.error("--dry-run cannot be combined with --resume")
    benchmark_path = args.benchmark.resolve()
    try:
        benchmark = load_benchmark(benchmark_path)
        if args.limit is not None and args.limit > len(benchmark["problems"]):
            parser.error("--limit exceeds benchmark size")
        if args.dry_run:
            print(f"Benchmark: {benchmark['name']}")
            print(f"Platform: {benchmark['platform']}")
            print(f"Problems: {len(benchmark['problems'])}")
            for order, item in enumerate(benchmark["problems"], 1):
                print(f"{order:2d}. {item['id']} expected_rating={item['expected_rating']}")
            return 0
        run_batch(benchmark_path, benchmark, args.resume, args.limit)
        return 0
    except KeyboardInterrupt:
        return 130
    except (BatchSystemError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[Batch] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
