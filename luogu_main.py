#!/usr/bin/env python3
"""Luogu main-site adapter using a real persistent Playwright browser session."""

import argparse
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path


HOME_URL = "https://www.luogu.com.cn/"
PROFILE_DIR = Path.home() / ".local" / "share" / "LocalJudgeAgent" / "luogu-profile"
JUDGE_TIMEOUT_SEC = 180
POLL_INTERVAL_SEC = 1.5


class MainSiteError(Exception):
    pass


def require_gui():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise MainSiteError("GUI session unavailable")


def playwright_api():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MainSiteError(
            "Playwright is not installed. Install it and its Chromium runtime first."
        ) from exc
    return sync_playwright


def open_context(playwright):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900}
        )
    except Exception as exc:
        raise MainSiteError(f"Unable to start headed Chromium: {exc}") from exc


def login_detected(page):
    login_controls = page.get_by_text(re.compile(r"^(登录|Login)$"))
    if login_controls.count() and login_controls.first.is_visible():
        return False
    user_links = page.locator('header a[href^="/user/"], nav a[href^="/user/"]')
    return any(user_links.nth(index).is_visible() for index in range(user_links.count()))


def login():
    require_gui()
    sync_playwright = playwright_api()
    with sync_playwright() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not login_detected(page):
                print("[Luogu] Complete login in the opened browser.")
                input("Press Enter after login is completed...")
                page.reload(wait_until="domcontentloaded", timeout=30000)
            if not login_detected(page):
                raise MainSiteError("Login could not be confirmed.")
            print("[Luogu] Login session saved.")
        finally:
            context.close()


def check_login():
    require_gui()
    sync_playwright = playwright_api()
    with sync_playwright() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not login_detected(page):
                raise MainSiteError("Luogu login session is not available.")
            print("[Luogu] Persistent login session confirmed.")
        finally:
            context.close()


def save_debug(page, debug_dir, name):
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def visible(locator):
    return locator.count() > 0 and locator.first.is_visible()


def last_visible(locator):
    for index in range(locator.count() - 1, -1, -1):
        if locator.nth(index).is_visible():
            return locator.nth(index)
    return None


def open_submission_form(page, problem_id):
    page.goto(f"https://www.luogu.com.cn/problem/{problem_id}",
              wait_until="domcontentloaded", timeout=30000)
    if not re.search(rf"/problem/{re.escape(problem_id)}(?:$|[?#])", page.url):
        raise MainSiteError("PROBLEM_ID_MISMATCH")
    candidates = [
        page.get_by_role("button", name=re.compile(r"^(提交答案|提交|提交代码|Submit)$", re.I)),
        page.get_by_role("link", name=re.compile(r"^(提交答案|提交|提交代码|Submit)$", re.I)),
        page.get_by_text(re.compile(r"^(提交答案|提交|提交代码)$")),
    ]
    for candidate in candidates:
        entry = last_visible(candidate)
        if entry is not None:
            name = " ".join(entry.inner_text().split())
            entry.click()
            page.wait_for_timeout(800)
            if "#submit" not in page.url:
                raise MainSiteError("Submission entry did not open the submit area.")
            return name
    raise MainSiteError("Could not locate the problem submission entry.")


def ensure_code_mode(page):
    tab = page.get_by_text("提交代码", exact=True)
    if not visible(tab):
        raise MainSiteError("Code submission mode is unavailable.")
    tab.first.click()
    page.wait_for_timeout(500)
    return True


def locate_language(page):
    labels = re.compile(r"(GNU\s*C\+\+\s*17|C\+\+\s*17)", re.I)
    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        options = select.locator("option")
        for option_index in range(options.count()):
            text = options.nth(option_index).inner_text()
            if labels.search(text):
                return ("select", select, options.nth(option_index).get_attribute("value"))
    named_combos = page.get_by_role("combobox", name=re.compile(r"(语言|language)", re.I))
    if visible(named_combos):
        return ("combobox", named_combos.first, labels)
    combos = page.get_by_role("combobox")
    visible_combos = [combos.nth(index) for index in range(combos.count())
                      if combos.nth(index).is_visible()]
    if len(visible_combos) == 1:
        return ("combobox", visible_combos[0], labels)
    current = page.get_by_text(re.compile(r"^C\+\+\d+(?:\s*\([^)]*\))?$", re.I))
    current_control = last_visible(current)
    if current_control is not None:
        return ("custom", current_control, labels)
    raise MainSiteError("Could not locate the language selector.")


def cpp17_option(page, language):
    kind, control, value = language
    if kind == "select":
        return True
    control.click()
    page.wait_for_timeout(300)
    option = page.get_by_role("option", name=value)
    if not visible(option):
        option = page.get_by_text(re.compile(r"^C\+\+17(?:\s*\([^)]*\))?$", re.I))
    if not visible(option):
        page.keyboard.press("Escape")
        raise MainSiteError("GNU C++17 is not available in the language selector.")
    return option.first


def choose_cpp17(page, language):
    kind, control, value = language
    if kind == "select":
        control.select_option(value=value)
    else:
        try:
            option = cpp17_option(page, language)
        except MainSiteError as exc:
            raise MainSiteError("LANGUAGE_SELECTION_FAILED") from exc
        option.click()
    page.wait_for_timeout(300)
    selected = page.get_by_text(re.compile(r"^C\+\+17(?:\s*\([^)]*\))?$", re.I))
    if not visible(selected):
        raise MainSiteError("LANGUAGE_SELECTION_FAILED")


def locate_editor(page):
    candidates = [
        page.locator('[contenteditable="true"][role="textbox"]'),
        page.locator(".monaco-editor textarea"),
        page.locator(".CodeMirror textarea"),
        page.get_by_role("textbox", name=re.compile(r"(代码|code)", re.I)),
        page.locator("textarea"),
    ]
    for candidate in candidates:
        if visible(candidate):
            return candidate.first
    raise MainSiteError("Could not locate the code editor.")


def locate_final_submit(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"^(提交评测|Submit)$", re.I)),
        page.locator('button[type="submit"]'),
    ]
    for candidate in candidates:
        matches = [candidate.nth(index) for index in range(candidate.count())
                   if candidate.nth(index).is_visible()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise MainSiteError("Submit button is not uniquely identified.")
    raise MainSiteError("Could not locate the final submit button.")


def normalized_code(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def editor_text(editor):
    lines = editor.locator(".cm-line")
    if lines.count():
        return "\n".join(lines.nth(index).text_content() or "" for index in range(lines.count()))
    return editor.inner_text()


def fill_editor(editor, code):
    editor.click()
    editor.press("Control+A")
    editor.page.keyboard.insert_text(code)
    editor.page.wait_for_timeout(300)
    actual = editor_text(editor)
    if normalized_code(actual) != normalized_code(code):
        raise MainSiteError("CODE_EDITOR_FILL_FAILED")
    return hashlib.sha256(normalized_code(actual).encode("utf-8")).hexdigest()


def inspect_submission_form(problem_id, code_path, debug_dir, dry_fill=False):
    require_gui()
    sync_playwright = playwright_api()
    with sync_playwright() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not login_detected(page):
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not login_detected(page):
                raise MainSiteError("Luogu login session is not available.")
            entry_name = open_submission_form(page, problem_id)
            ensure_code_mode(page)
            language = locate_language(page)
            option = cpp17_option(page, language)
            page.keyboard.press("Escape")
            editor = locate_editor(page)
            submit_button = locate_final_submit(page)
            print("[Luogu] Problem page: OK")
            print(f"[Luogu] Submission entry: {entry_name}")
            print("[Luogu] Code submission mode: OK")
            print("[Luogu] Language control: OK")
            print("[Luogu] C++17 option: OK")
            print("[Luogu] CodeMirror 6 editor: OK")
            print(f"[Luogu] Submit button: {' '.join(submit_button.inner_text().split())}")
            if dry_fill:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(500)
                ensure_code_mode(page)
                choose_cpp17(page, locate_language(page))
                editor = locate_editor(page)
                locate_final_submit(page)
                code = code_path.read_text(encoding="utf-8")
                fill_editor(editor, code)
                print("[Luogu] Dry fill language: C++17")
                print("[Luogu] Dry fill code verification: PASS")
            print("[Luogu] Inspection PASS")
            print("[Luogu] No submission performed.")
        except Exception as exc:
            save_debug(page, debug_dir, "inspect_failed")
            if isinstance(exc, MainSiteError):
                raise
            raise MainSiteError(f"Submission-form inspection failed: {exc}") from exc
        finally:
            context.close()


def record_ids(page):
    ids = set()
    links = page.locator('a[href*="/record/"]')
    for index in range(links.count()):
        match = re.search(r"/record/(\d+)", links.nth(index).get_attribute("href") or "")
        if match:
            ids.add(match.group(1))
    match = re.search(r"/record/(\d+)", page.url)
    if match:
        ids.add(match.group(1))
    return ids


def captcha_visible(page):
    text = page.get_by_text(re.compile(r"(验证码|安全验证|captcha|challenge)", re.I))
    frames = page.locator('iframe[src*="captcha"], iframe[src*="challenge"]')
    return visible(text) or frames.count() > 0


def wait_for_record(page, previous_ids):
    deadline = time.monotonic() + 60
    prompted = False
    while time.monotonic() < deadline:
        if captcha_visible(page) and not prompted:
            print("[Luogu] CAPTCHA requires manual completion.")
            print("[Luogu] Complete it in the opened browser.")
            input("Press Enter after CAPTCHA/submission is completed...")
            prompted = True
        match = re.search(r"/record/(\d+)", page.url)
        if match and match.group(1) not in previous_ids:
            return match.group(1)
        page.wait_for_timeout(1000)
    raise MainSiteError("MAIN_SUBMIT_FAILED: no new judge record was confirmed.")


def page_status(page):
    labels = (
        ("OJ_AC", "Accepted"), ("OJ_AC", "AC"), ("OJ_AC", "答案正确"),
        ("OJ_WA", "Wrong Answer"), ("OJ_WA", "WA"), ("OJ_WA", "答案错误"),
        ("OJ_TLE", "Time Limit Exceeded"), ("OJ_TLE", "TLE"), ("OJ_TLE", "运行时间超限"),
        ("OJ_MLE", "Memory Limit Exceeded"), ("OJ_MLE", "MLE"), ("OJ_MLE", "内存超限"),
        ("OJ_RE", "Runtime Error"), ("OJ_RE", "RE"), ("OJ_RE", "运行时错误"),
        ("OJ_CE", "Compile Error"), ("OJ_CE", "CE"), ("OJ_CE", "编译失败"),
        ("OJ_PC", "Partially Correct"), ("OJ_PC", "PC"), ("OJ_PC", "部分正确"),
        (None, "Waiting"), (None, "Judging"), (None, "等待"), (None, "评测中"),
    )
    visible_labels = []
    for normalized, name in labels:
        locator = page.get_by_text(name, exact=True)
        for index in range(locator.count()):
            item = locator.nth(index)
            box = item.bounding_box() if item.is_visible() else None
            if box:
                visible_labels.append((box["y"], normalized, name))
    if visible_labels:
        _, normalized, raw = min(visible_labels, key=lambda item: item[0])
        return None if normalized is None else (normalized, raw)
    return "OJ_UNKNOWN", "UNKNOWN"


def wait_for_result(page, record_id):
    url = f"https://www.luogu.com.cn/record/{record_id}"
    started = time.perf_counter()
    deadline = time.monotonic() + JUDGE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = page_status(page)
        if status is not None and status[0] != "OJ_UNKNOWN":
            normalized, raw = status
            score_match = re.search(r"(?:得分|Score)\s*[:：]?\s*(\d+(?:\.\d+)?)", page.inner_text("body"), re.I)
            score = float(score_match.group(1)) if score_match else (100 if normalized == "OJ_AC" else None)
            return normalized, raw, score, time.perf_counter() - started
        time.sleep(POLL_INTERVAL_SEC)
    return "OJ_UNKNOWN", "OJ_RESULT_TIMEOUT", None, time.perf_counter() - started


def submit_and_wait(problem_id, code, debug_dir):
    require_gui()
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    sync_playwright = playwright_api()
    with sync_playwright() as playwright:
        context = open_context(playwright)
        submitted_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not login_detected(page):
                raise MainSiteError("Luogu login session is not available.")
            open_submission_form(page, problem_id)
            ensure_code_mode(page)
            if not re.search(rf"/problem/{re.escape(problem_id)}(?:$|[?#])", page.url):
                raise MainSiteError("PROBLEM_ID_MISMATCH")
            previous_ids = record_ids(page)
            language = locate_language(page)
            editor = locate_editor(page)
            submit_button = locate_final_submit(page)
            choose_cpp17(page, language)
            fill_editor(editor, code)
            submit_button.click()
            record_id = wait_for_record(page, previous_ids)
            status, raw_status, score, judge_time = wait_for_result(page, record_id)
            return {
                "provider": "luogu-main", "submitted": True, "record_id": record_id,
                "status": status, "raw_status": raw_status, "score": score,
                "submission_sha256": digest, "submitted_at": submitted_at,
                "judge_time_sec": round(judge_time, 6),
            }
        except Exception as exc:
            save_debug(page, debug_dir, "submit_failed")
            if isinstance(exc, MainSiteError):
                raise
            raise MainSiteError(f"Browser submission failed: {exc}") from exc
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--login", action="store_true")
    modes.add_argument("--check-login", action="store_true")
    modes.add_argument("--inspect", metavar="PROBLEM_ID")
    parser.add_argument("--code", type=Path)
    parser.add_argument("--dry-fill", action="store_true",
                        help="select C++17 and verify editor fill without submitting")
    parser.add_argument("--debug-dir", type=Path, default=Path("browser_debug"))
    args = parser.parse_args()
    if args.dry_fill and not args.inspect:
        parser.error("--dry-fill requires --inspect")
    try:
        if args.login:
            login()
        elif args.check_login:
            check_login()
        else:
            if not args.code or not args.code.is_file():
                parser.error("--inspect requires an existing --code file")
            inspect_submission_form(args.inspect, args.code, args.debug_dir, args.dry_fill)
    except MainSiteError as exc:
        print(f"[Luogu] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
