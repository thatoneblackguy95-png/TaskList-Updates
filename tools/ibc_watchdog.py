#!/usr/bin/env python3
"""IBC data watchdog — Task List building-code dataset guardian.

Lives in the TaskList-Updates repo and runs monthly on GitHub
Actions. It needs no Apollo, no Muse account, and no human:
it re-fetches every dataset row's primary source
(`source_url` in ibc_codes.json) and checks the page still
supports what the row claims.

THE RULE (the app's contract): the IBC chip must never state
anything unverified. This script therefore NEVER edits a
row's facts and NEVER restores or advances a `verified`
stamp — passing a mechanical check is not a verification.
Its one data power is the fail-safe: a row whose source
fails the check on TWO consecutive monthly runs is
DOWNGRADED in ibc_codes.json (verified stamp removed,
edition/next nulled, `watchdog_downgraded` date added). The
app (V1.9.53+) reads a missing verified stamp as "state
nothing" — the chip goes quiet rather than stale-wrong.
Possible adoption news (a newer edition year appearing in
code context on a page with no recorded next edition) is
reported, never acted on.

Events are summarised to a GitHub issue (created or updated)
when running under GitHub Actions. Locally it just prints.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zlib
from datetime import date
from pathlib import Path

ROOT = Path(os.environ.get(
    "IBC_WATCHDOG_ROOT",
    Path(__file__).resolve().parent.parent))
DATA_PATH = ROOT / "ibc_codes.json"
STATE_PATH = ROOT / "tools" / "ibc_watchdog_state.json"
UA = {"User-Agent": "TaskList-IBC-Watchdog/1.0"}
EDITIONS = [2009, 2012, 2015, 2018, 2021, 2024, 2027]
CONTEXT = ("international building code", "ibc", "building code")
FAIL_LIMIT = 2  # consecutive failed monthly runs -> downgrade


def fetch(url):
    """(status, raw_bytes, content_type). Never raises. An
    HTTP error keeps its real status code: a 403/429 bot wall
    is NOT a dead source (grade "blocked", never a fail) —
    only connection failures (status 0) and 404/410 count
    toward a downgrade."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read(8_000_000), \
                r.headers.get_content_type() or ""
    except urllib.error.HTTPError as e:
        return e.code, b"", ""
    except Exception as e:  # noqa: BLE001 — recorded, not raised
        return 0, b"", f"error: {e}"


def _pdf_text(raw):
    """Best-effort text from a PDF: inflate streams, pull the
    literal strings. Enough for token proximity checks."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except Exception:
            pass
        for s in re.findall(rb"\((?:[^()\\]|\\.)*\)", chunk):
            try:
                out.append(s[1:-1].decode("latin-1"))
            except Exception:
                pass
    return " ".join(out)


def page_text(raw, content_type, url):
    if "pdf" in content_type or url.split("?")[0].endswith(".pdf") \
            or "/download" in url:
        text = _pdf_text(raw)
        if len(text) > 200:
            return text
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return text


def near(text, year, window=300):
    """True when `year` appears with a code-context token
    within `window` characters on either side."""
    t = text.lower()
    y = str(year)
    for m in re.finditer(re.escape(y), t):
        seg = t[max(0, m.start() - window): m.end() + window]
        if any(tok in seg for tok in CONTEXT):
            return True
    return False


def check_row(text, edition):
    """Grade one row's source: "supports", "alive" or "fail".

    SUPPORTS: the edition year appears in code context (PDFs:
    year + context anywhere — extracted PDF text does not keep
    reading order, so proximity is meaningless there).
    ALIVE: the source loads and is plainly the right authority
    (a council landing page, a statute) but does not itself
    state the edition — the claim rests on its verified stamp.
    FAIL: dead/unreadable source, or the edition year has
    vanished ENTIRELY from a substantive code document (a long
    page that discusses building codes but never the year).
    Only FAIL counts toward a downgrade. A bot wall
    (401/403/429) grades "blocked" — the source exists; it
    merely refuses bots — and never counts as a fail."""
    if not text or len(text) < 100:
        return "fail", "source returned no readable text"
    t = text.lower()
    if edition is None:
        if any(tok in t for tok in
               CONTEXT + ("building", "construction")):
            return "alive", ""
        return "fail", "authority page no longer about construction"
    if near(text, edition) or (
            str(edition) in t and any(tok in t for tok in CONTEXT)):
        return "supports", ""
    if len(text) > 120000 and any(tok in t for tok in CONTEXT):
        return "fail", f"edition {edition} absent from a substantive source"
    return "alive", ""


def possible_change(text, row):
    """A newer edition year in code context, unrecorded."""
    ed = row.get("edition")
    if not ed or row.get("next"):
        return None
    for y in EDITIONS:
        if y > ed and near(text, y):
            return y
    return None


def gh_issue(events, today):
    """Create or update the watchdog issue. Actions only."""
    title = "IBC watchdog: rows need re-verification"
    body = (f"Monthly source re-check {today}:\n\n"
            + "\n".join(f"- {e}" for e in events)
            + "\n\nDowngraded rows state nothing in the app "
              "until a human re-verifies them against the "
              "primary source and restores the row in "
              "ibc_codes.json (edition + verified date).")
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--search", title,
             "--state", "open", "--json", "number"],
            capture_output=True, text=True, timeout=60)
        found = json.loads(out.stdout or "[]")
        if found:
            subprocess.run(
                ["gh", "issue", "comment", str(found[0]["number"]),
                 "--body", body],
                capture_output=True, timeout=60)
        else:
            subprocess.run(
                ["gh", "issue", "create", "--title", title,
                 "--body", body],
                capture_output=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"issue reporting failed: {e}")


def main():
    today = date.today().isoformat()
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    state = {"rows": {}}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    rows_state = state.setdefault("rows", {})
    events = []
    downgrades = 0

    def visit(key, row, edition_ctx):
        nonlocal downgrades
        url = row.get("source_url")
        rec = rows_state.setdefault(key, {"fails": 0})
        rec["last"] = today
        if row.get("watchdog_downgraded"):
            # Never auto-restored; note if the source lives again.
            if url:
                status, _raw, _ct = fetch(url)
                if status == 200 and not rec.get("back"):
                    rec["back"] = today
                    events.append(
                        f"{key}: source reachable again — row stays "
                        "downgraded until a human re-verifies it.")
            return
        if not url:
            rec["note"] = "no source_url — not checked"
            return
        status, raw, ct = fetch(url)
        if status in (401, 403, 429):
            grade, why = "blocked", f"HTTP {status} (bot wall)"
            text = ""
        elif status == 200:
            text = page_text(raw, ct, url)
            grade, why = check_row(text, edition_ctx)
        else:
            grade, why = "fail", f"HTTP {status}"
            text = ""
        rec["grade"] = grade
        if grade != "fail":
            rec["fails"] = 0
            rec.pop("why", None)
            y = possible_change(text, row)
            if y and y not in rec.get("pc", []):
                # Report each (row, year) sighting once, ever —
                # standing mentions (a code's name year, an
                # energy-code edition) must not re-alarm monthly.
                rec.setdefault("pc", []).append(y)
                events.append(
                    f"{key}: possible adoption news — {y} edition "
                    "appears in code context at the source but no "
                    "next edition is recorded. Needs human "
                    "re-verification; data unchanged.")
        else:
            rec["fails"] = int(rec.get("fails", 0)) + 1
            rec["why"] = why
            if rec["fails"] >= FAIL_LIMIT:
                row["verified"] = None
                if "edition" in row:
                    row["edition"] = None
                row["next"] = None
                row["watchdog_downgraded"] = today
                downgrades += 1
                events.append(
                    f"{key}: DOWNGRADED after {rec['fails']} failed "
                    f"checks ({why}). The app now states nothing "
                    "for this row.")
            else:
                events.append(
                    f"{key}: check failed ({why}) — "
                    f"{rec['fails']}/{FAIL_LIMIT} before downgrade.")

    for st, row in (data.get("states") or {}).items():
        visit(st, row, row.get("edition"))
    for key, crow in (data.get("counties") or {}).items():
        srow = (data.get("states") or {}).get(crow.get("state")) or {}
        visit(key, crow, srow.get("edition"))

    if downgrades:
        data["version"] = int(data.get("version", 1)) + 1
        DATA_PATH.write_text(
            json.dumps(data, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"watchdog {today}: {len(rows_state)} rows tracked, "
          f"{downgrades} downgraded, {len(events)} events")
    for e in events:
        print(" -", e)
    if events and os.environ.get("GITHUB_ACTIONS") == "true":
        gh_issue(events, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
