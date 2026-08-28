#!/usr/bin/env python3
"""Codeforces public API, problem cache, and verdict helpers."""

import argparse
import fcntl
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
API_BASE = "https://codeforces.com/api"
API_INTERVAL_SEC = 2.1
API_LOCK = Path.home() / ".cache" / "LocalJudgeAgent" / "codeforces-api.lock"
VERDICT_POLL_SEC = 2.5
SUBMISSION_FIND_TIMEOUT_SEC = 30
VERDICT_TIMEOUT_SEC = 180
CF_PENDING_VERDICTS = {None, "", "SUBMITTED", "TESTING", "QUEUED"}
CF_TERMINAL_VERDICTS = {
    "OK": "OJ_AC", "WRONG_ANSWER": "OJ_WA",
    "TIME_LIMIT_EXCEEDED": "OJ_TLE", "MEMORY_LIMIT_EXCEEDED": "OJ_MLE",
    "RUNTIME_ERROR": "OJ_RE", "COMPILATION_ERROR": "OJ_CE",
    "IDLENESS_LIMIT_EXCEEDED": "OJ_IDLE", "PARTIAL": "OJ_PC",
    "CHALLENGED": "OJ_FAILED", "SKIPPED": "OJ_SKIPPED",
    "REJECTED": "OJ_REJECTED", "FAILED": "OJ_FAILED", "CRASHED": "OJ_FAILED",
    "INPUT_PREPARATION_CRASHED": "OJ_FAILED", "SECURITY_VIOLATED": "OJ_FAILED",
}


class CodeforcesError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_problem_id(value):
    match = re.fullmatch(r"(?:CF)?(\d+)([A-Za-z][A-Za-z0-9]*)", value.strip(), re.I)
    if not match:
        raise CodeforcesError("INVALID_PROBLEM_ID", f"invalid Codeforces problem ID: {value}")
    return int(match.group(1)), match.group(2).upper()


def api_get(method, params=None):
    API_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with API_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        try:
            last_call = float(lock.read().strip() or 0)
        except ValueError:
            last_call = 0
        delay = API_INTERVAL_SEC - (time.time() - last_call)
        if delay > 0:
            time.sleep(delay)
        lock.seek(0)
        lock.truncate()
        lock.write(str(time.time()))
        lock.flush()
        try:
            response = requests.get(f"{API_BASE}/{method}", params=params,
                                    headers={"User-Agent": "LocalJudgeAgent/0.5"}, timeout=(10, 30))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CodeforcesError("API_FAILED", str(exc)) from exc
    if payload.get("status") != "OK":
        raise CodeforcesError("API_FAILED", str(payload.get("comment", "unknown API error")))
    return payload["result"]


def fetch_problem_metadata(contest_id, index):
    result = api_get("problemset.problems")
    problem = next((item for item in result["problems"]
                    if item.get("contestId") == contest_id and item.get("index") == index), None)
    if not problem:
        raise CodeforcesError("PROBLEM_NOT_FOUND", f"Codeforces problem {contest_id}{index} not found")
    contests = api_get("contest.list", {"gym": "false"})
    contest = next((item for item in contests if item.get("id") == contest_id), None)
    if not contest:
        raise CodeforcesError("CONTEST_NOT_FOUND", f"Codeforces contest {contest_id} not found")
    if contest.get("phase") != "FINISHED":
        raise CodeforcesError("ACTIVE_CONTEST_NOT_SUPPORTED",
                              f"contest {contest_id} phase is {contest.get('phase')}")
    return {
        "contest_id": contest_id, "index": index, "title": problem["name"],
        "rating": problem.get("rating"), "tags": problem.get("tags", []),
        "contest_phase": contest["phase"],
    }


def cache_path(contest_id, index):
    return ROOT / "problems" / "codeforces" / f"{contest_id}{index}.json"


def save_problem(problem, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(problem, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_problem(value):
    contest_id, index = parse_problem_id(value)
    metadata = fetch_problem_metadata(contest_id, index)
    from codeforces_main import CodeforcesMainError, fetch_problem_statement
    try:
        statement = fetch_problem_statement(contest_id, index)
    except CodeforcesMainError as exc:
        raise CodeforcesError("FETCH_FAILED", str(exc)) from exc
    if not statement.get("samples"):
        raise CodeforcesError("NO_SAMPLES", f"{contest_id}{index} has no samples")
    problem_id = f"{contest_id}{index}"
    return {
        "platform": "codeforces", "problem_id": problem_id,
        "contest_id": contest_id, "index": index, "title": metadata["title"],
        "difficulty": str(metadata["rating"]) if metadata["rating"] is not None else "unknown",
        "rating": metadata["rating"], "tags": metadata["tags"],
        "contest_phase": metadata["contest_phase"],
        "source_url": f"https://codeforces.com/problemset/problem/{contest_id}/{index}",
        "time_limit": statement.get("time_limit"), "memory_limit": statement.get("memory_limit"),
        "statement": statement["statement"], "samples": statement["samples"],
    }


def load_or_fetch(value):
    contest_id, index = parse_problem_id(value)
    path = cache_path(contest_id, index)
    if path.exists():
        print(f"[Problem] cache hit: {path.relative_to(ROOT)}")
        problem = json.loads(path.read_text(encoding="utf-8"))
        if problem.get("contest_phase") != "FINISHED":
            raise CodeforcesError("ACTIVE_CONTEST_NOT_SUPPORTED", "cached contest is not finished")
        return problem, path
    print(f"[Problem] fetching Codeforces {contest_id}{index}")
    problem = fetch_problem(f"{contest_id}{index}")
    save_problem(problem, path)
    print(f"[Problem] cached: {path.relative_to(ROOT)}")
    return problem, path


def get_user_submissions(handle, count=20):
    return api_get("user.status", {"handle": handle, "from": 1, "count": count})


def find_submission(submissions, before_id, contest_id, index):
    matches = [item for item in submissions
               if item.get("id", 0) > before_id
               and item.get("contestId") == contest_id
               and item.get("problem", {}).get("index") == index]
    return max(matches, key=lambda item: item["id"], default=None)


def normalize_verdict(verdict):
    return CF_TERMINAL_VERDICTS.get(verdict, "OJ_UNKNOWN")


def wait_for_submission(handle, before_id, contest_id, index):
    deadline = time.monotonic() + SUBMISSION_FIND_TIMEOUT_SEC
    while time.monotonic() < deadline:
        submission = find_submission(get_user_submissions(handle), before_id, contest_id, index)
        if submission:
            return submission
        time.sleep(VERDICT_POLL_SEC)
    return None


def wait_for_verdict(handle, submission_id):
    started = time.perf_counter()
    deadline = time.monotonic() + VERDICT_TIMEOUT_SEC
    history = []
    unseen = object()
    last_verdict = unseen
    while time.monotonic() < deadline:
        submissions = get_user_submissions(handle)
        submission = next((item for item in submissions if item.get("id") == submission_id), None)
        verdict = submission.get("verdict") if submission else None
        if verdict != last_verdict:
            history.append({"status": verdict,
                            "observed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            last_verdict = verdict
        if not submission or verdict in CF_PENDING_VERDICTS or verdict not in CF_TERMINAL_VERDICTS:
            time.sleep(VERDICT_POLL_SEC)
            continue
        return {"submission_id": submission["id"], "status": normalize_verdict(verdict),
                "raw_status": verdict, "raw_verdict": verdict,
                "timeConsumedMillis": submission.get("timeConsumedMillis"),
                "memoryConsumedBytes": submission.get("memoryConsumedBytes"),
                "verdict_history": history,
                "judge_time_sec": round(time.perf_counter() - started, 6)}
    raw_verdict = None if last_verdict is unseen else last_verdict
    return {"submission_id": submission_id, "status": "OJ_UNKNOWN",
            "raw_status": raw_verdict, "raw_verdict": raw_verdict,
            "timeout_reason": "OJ_RESULT_TIMEOUT", "verdict_history": history,
            "judge_time_sec": round(time.perf_counter() - started, 6)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_id", help="for example 4A or CF4A")
    args = parser.parse_args()
    try:
        problem, path = load_or_fetch(args.problem_id)
    except CodeforcesError as exc:
        print(f"[Codeforces] {exc.code}: {exc}")
        return 1
    print(f"[Codeforces] {problem['problem_id']} {problem['title']}")
    print(f"[Rating] {problem['rating']}")
    print(f"[Tags] {', '.join(problem['tags'])}")
    print(f"[Samples] {len(problem['samples'])}")
    print(f"[File] {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
