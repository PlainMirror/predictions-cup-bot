"""Scheduled go-live: wait, verify, then start the live engine with day-1 limits. (Used on Oct 11 to start live trading with
small day-1 limits; scaling up was a separate decision after reviewing day 1.)

    py go_live.py --at "2026-10-12 09:00"                 # ET; runs in the background until then
    py go_live.py --at now                                # immediately

At the start time:
  1. the collector feed must be fresh (< 90 s) and no other live engine may be running;
  2. live_check.py runs its one-share convention checks; ANY failure aborts (nothing else is traded);
  3. run_mm.py --live --params data/live_params_day1.json --exit 0.7,0.0,24 starts (output: data/live_console.log).
If the PC was asleep and the start is more than --late-h (3) hours past, it aborts instead of starting late.
Not restarted automatically if it stops (loss limit, error, STOP file): read the log first.
Cancel before the start time: create data/STOP_GOLIVE. Stop the live engine (and the shadow): create data/STOP.
Status: data/go_live_status.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
DATA = ROOT / "data"
ABORT = DATA / "STOP_GOLIVE"
STATUS = DATA / "go_live_status.json"
LOG = DATA / "live_console.log"


def status(state: str, **kw):
    STATUS.write_text(json.dumps({"state": state, "at": dt.datetime.now().isoformat(timespec="seconds"), **kw}, indent=1),
                      encoding="utf-8")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now():%m-%d %H:%M:%S} go_live: {state} {kw if kw else ''}\n")
    print(f"{dt.datetime.now():%H:%M:%S} {state} {kw if kw else ''}", flush=True)


def other_live_engine() -> bool:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pyw?(thonw?)?\\.exe$' -and "
                          "$_.CommandLine -match 'run_mm.py' -and $_.CommandLine -match '--live' } | "
                          "Measure-Object | Select-Object -ExpandProperty Count"],   # python only: this query's own
                                                                                    # command line contains the words
                         capture_output=True, text=True)
    return out.stdout.strip() not in ("", "0")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", required=True, help='"YYYY-MM-DD HH:MM" US Eastern, or "now"')
    ap.add_argument("--params", default=str(DATA / "live_params_day1.json"))
    ap.add_argument("--exit", default="0.7,0.0,24")
    ap.add_argument("--late-h", type=float, default=3.0, help="abort if we reach the start more than this late (PC asleep)")
    a = ap.parse_args()
    sys.path.insert(0, str(BOT))
    from mm.events import et_to_utc
    start = time.time() if a.at == "now" else et_to_utc(a.at.replace(" ", "T")).timestamp()
    status("armed", start_et=a.at, params=a.params, exit=a.exit)
    while time.time() < start:
        if ABORT.exists():
            status("aborted (STOP_GOLIVE found before start)")
            return
        time.sleep(min(30, max(1, start - time.time())))

    if time.time() - start > a.late_h * 3600:
        status("aborted: woke up too late after the scheduled start (PC asleep?)", late_h=round((time.time() - start) / 3600, 1))
        return
    from mm.livefeed import LiveFeedClient
    from sm_client import SuperMarket
    feed = LiveFeedClient(SuperMarket(reads_per_min=20, writes_per_min=1))
    age = feed.feed_age()
    if age > feed.max_feed_age:
        status("aborted: collector feed stale", age_s=round(age))
        return
    if other_live_engine():
        status("aborted: a live engine is already running")
        return
    status("running convention checks (one-share orders)")
    import live_check
    if not live_check.run():
        status("aborted: convention checks FAILED (see data/live_check_*.json); engine NOT started")
        return
    status("checks passed; starting live engine")
    with LOG.open("a", encoding="utf-8") as fh:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.Popen([sys.executable, "-u", str(BOT / "run_mm.py"), "--live", "--params", a.params, "--exit", a.exit],
                             cwd=str(BOT), stdout=fh, stderr=subprocess.STDOUT, env=env)
        status("live engine started", pid=p.pid)
        rc = p.wait()
    status("live engine stopped", returncode=rc)


if __name__ == "__main__":
    main()
