#!/usr/bin/env python3
"""Luogu Open Platform judge adapter."""

import os
import time
import uuid
from datetime import datetime

import requests


BASE_URL = "https://open-v1.lgapi.cn"
LANGUAGE = "cxx/17/gcc"
POLL_INTERVAL_SEC = 1.5
RESULT_TIMEOUT_SEC = 120


class OpenJudgeError(Exception):
    pass


class TokenNotConfigured(OpenJudgeError):
    pass


def get_auth():
    token = os.environ.get("LUOGU_OPEN_TOKEN")
    if not token:
        raise TokenNotConfigured("Open Platform token is not configured.")
    username, separator, password = token.partition(":")
    if not separator or not username or not password:
        raise OpenJudgeError("LUOGU_OPEN_TOKEN must use the username:password format.")
    return username, password


def submit_problem(problem_id, code):
    track_id = f"LocalJudgeAgent-{problem_id}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    response = requests.post(
        f"{BASE_URL}/judge/problem",
        auth=get_auth(),
        json={"pid": problem_id, "lang": LANGUAGE, "o2": True,
              "code": code, "trackId": track_id},
        timeout=(10, 30),
    )
    response.raise_for_status()
    try:
        request_id = response.json()["requestId"]
    except (ValueError, KeyError, TypeError) as exc:
        raise OpenJudgeError("Open Platform submission returned no requestId.") from exc
    if not isinstance(request_id, str) or not request_id:
        raise OpenJudgeError("Open Platform submission returned an invalid requestId.")
    return request_id, time.perf_counter() - started


def normalize_status(raw_status, score=None, compile_success=None):
    if compile_success is False or raw_status == 2:
        return "OJ_CE"
    statuses = {4: "OJ_MLE", 5: "OJ_TLE", 6: "OJ_WA", 7: "OJ_RE", 12: "OJ_AC"}
    if raw_status == 14:
        return "OJ_PC" if score else "OJ_WA"
    return statuses.get(raw_status, "OJ_UNKNOWN")


def get_result(request_id):
    response = requests.get(
        f"{BASE_URL}/judge/result",
        params={"id": request_id},
        auth=get_auth(),
        timeout=(10, 30),
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    try:
        result = response.json()
        data = result["data"]
        compile_result = data.get("compile")
        judge_result = data.get("judge")
    except (ValueError, KeyError, TypeError) as exc:
        raise OpenJudgeError("Open Platform returned an invalid judge result.") from exc

    compile_success = compile_result.get("success") if isinstance(compile_result, dict) else None
    if compile_success is False:
        return {"status": "OJ_CE", "raw_status": None, "score": None}
    if not isinstance(judge_result, dict) or judge_result.get("status") in (0, 1):
        return None
    raw_status = judge_result.get("status")
    score = judge_result.get("score")
    return {"status": normalize_status(raw_status, score, compile_success),
            "raw_status": raw_status, "score": score}


def judge(problem_id, code):
    request_id, submit_time = submit_problem(problem_id, code)
    print(f"[OJ] submitted: {request_id}")
    started = time.perf_counter()
    deadline = started + RESULT_TIMEOUT_SEC
    while time.perf_counter() < deadline:
        try:
            result = get_result(request_id)
        except (requests.RequestException, OpenJudgeError) as exc:
            return {"provider": "luogu-open", "submitted": True,
                    "request_id": request_id, "status": "OJ_UNKNOWN",
                    "raw_status": type(exc).__name__, "score": None,
                    "submit_time_sec": round(submit_time, 6),
                    "judge_time_sec": round(time.perf_counter() - started, 6)}
        if result is not None:
            result.update({"provider": "luogu-open", "submitted": True,
                           "request_id": request_id,
                           "submit_time_sec": round(submit_time, 6),
                           "judge_time_sec": round(time.perf_counter() - started, 6)})
            return result
        time.sleep(POLL_INTERVAL_SEC)
    return {"provider": "luogu-open", "submitted": True, "request_id": request_id,
            "status": "OJ_UNKNOWN", "raw_status": "OJ_RESULT_TIMEOUT", "score": None,
            "submit_time_sec": round(submit_time, 6),
            "judge_time_sec": round(time.perf_counter() - started, 6)}
