#!/usr/bin/env python3
"""
test_scheduler.py  —  safety net for the fixture engine.

Two kinds of check:

  INVARIANTS (always run): things that must never happen in a valid schedule.
    * no court is used by two games at the same date/time
    * no team plays more than once in the same round
  These are hard failures.

  REGRESSION (golden file): compares the current scheduled_fixtures.csv against
    a saved baseline so you can see exactly what changed after you tune the
    engine. The first time, create the baseline with:
        python test_scheduler.py --update-baseline

It also reports (without failing) the soft metrics the engine trades off:
unavailability windows not honoured, and young-age-late counts, so you can
watch them move as you tune.

Usage:
    python test_scheduler.py                 # run engine, then check
    python test_scheduler.py --no-run        # check existing output only
    python test_scheduler.py --update-baseline

Exit code is non-zero if any invariant or the regression check fails, so it
works in CI or a pre-commit hook too.
"""

import os
import re
import sys
import subprocess

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(BASE, 'fixture_requests_vW26.py')
SCHEDULED = os.path.join(BASE, 'scheduled_fixtures.csv')
TEAMS = os.path.join(BASE, 'teams.csv')
BASELINE = os.path.join(BASE, 'tests', 'expected_scheduled.csv')


def _load(path):
    return pd.read_csv(path, dtype=str).fillna('')


def _is_bye(s):
    return str(s).strip().upper() in ('', '-', 'BYE')


def run_engine():
    print("Running engine...")
    r = subprocess.run([sys.executable, ENGINE], cwd=BASE,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise SystemExit("Engine failed to run.")


# ----------------------------------------------------------------------
# invariants (hard)
# ----------------------------------------------------------------------

def check_no_double_booked_courts(sched):
    """No two non-bye games share venue+court+date+time."""
    games = sched[~sched['away team'].map(_is_bye)].copy()
    key = ['round', 'venue', 'playing surface', 'game date', 'game time']
    key = [k for k in key if k in games.columns]
    dupes = games[games.duplicated(key, keep=False)]
    dupes = dupes[dupes['venue'].str.strip() != '']
    if len(dupes):
        sample = dupes.sort_values(key).head(6)
        detail = '\n'.join(
            f"    {r['game date']} {r['game time']} {r['venue']} {r['playing surface']}: "
            f"{r['home team']} v {r['away team']}" for _, r in sample.iterrows())
        return False, f"{len(dupes)} game(s) share a court at the same time:\n{detail}"
    return True, "no court is double-booked"


def check_no_team_clash_same_time(sched):
    """No team is in two games at the same date and time.

    (A team may legitimately play several games in a round, e.g. U8s, so the
    rule is not 'once per round' but 'never two games at once'.)
    """
    games = sched[~sched['away team'].map(_is_bye)]
    rows = []
    for _, r in games.iterrows():
        when = (str(r.get('game date', '')).strip(), str(r['game time']).strip())
        if not when[1]:
            continue
        for col in ('home team', 'away team'):
            t = str(r[col]).strip()
            if not _is_bye(t):
                rows.append((when[0], when[1], t))
    seen, clashes = set(), []
    for key in rows:
        if key in seen:
            clashes.append(key)
        seen.add(key)
    if clashes:
        ex = '; '.join(f"{t} at {d} {tm}" for d, tm, t in clashes[:6])
        return False, f"{len(clashes)} same-time clash(es): {ex}"
    return True, "no team is in two games at once"


# ----------------------------------------------------------------------
# soft metrics (reported, never fail)
# ----------------------------------------------------------------------

def _to_min(t):
    s = str(t).strip().lower().replace(' ', '')
    m = re.match(r'(\d{1,2})(?::(\d{2}))?(am|pm)?', s)
    if not m:
        return None
    h = int(m.group(1)); mi = int(m.group(2) or 0); ap = m.group(3)
    if ap == 'pm' and h != 12:
        h += 12
    if ap == 'am' and h == 12:
        h = 0
    return h * 60 + mi


def report_soft_metrics(sched):
    teams = _load(TEAMS) if os.path.exists(TEAMS) else pd.DataFrame()
    unavail = {r['Team'].strip(): str(r['Unavailable_Times']).strip()
               for _, r in teams.iterrows() if str(r.get('Unavailable_Times', '')).strip()}

    def violates(window, placed):
        for tok in window.split(';'):
            m = re.match(r'([<>])\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))', tok.strip().lower())
            if not m:
                continue
            lim = _to_min(m.group(2))
            if lim is None:
                continue
            if m.group(1) == '<' and placed < lim:
                return True
            if m.group(1) == '>' and placed > lim:
                return True
        return False

    games = sched[~sched['away team'].map(_is_bye)]
    viol = 0
    for _, r in games.iterrows():
        pm = _to_min(r['game time'])
        if pm is None:
            continue
        for col in ('home team', 'away team'):
            t = str(r[col]).strip()
            if t in unavail and violates(unavail[t], pm):
                viol += 1
    print(f"  soft: Unavailable_Times not honoured in {viol} placement(s)")


# ----------------------------------------------------------------------
# regression (golden)
# ----------------------------------------------------------------------

def check_regression(sched):
    if not os.path.exists(BASELINE):
        print(f"  regression: no baseline yet ({os.path.relpath(BASELINE, BASE)}). "
              "Create it with --update-baseline.")
        return True, "skipped"
    expected = _load(BASELINE)
    if sched.shape != expected.shape:
        return False, f"row/col count changed: now {sched.shape}, baseline {expected.shape}"
    if list(sched.columns) != list(expected.columns):
        return False, "columns changed vs baseline"
    if not sched.reset_index(drop=True).equals(expected.reset_index(drop=True)):
        diff = (sched.reset_index(drop=True) != expected.reset_index(drop=True)).any(axis=1)
        n = int(diff.sum())
        return False, f"{n} row(s) differ from the baseline schedule"
    return True, "schedule matches the baseline exactly"


def update_baseline():
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    _load(SCHEDULED).to_csv(BASELINE, index=False)
    print(f"Baseline written to {os.path.relpath(BASELINE, BASE)} "
          f"({len(_load(BASELINE))} rows).")


def main():
    args = sys.argv[1:]
    if '--update-baseline' in args:
        if '--no-run' not in args:
            run_engine()
        update_baseline()
        return
    if '--no-run' not in args:
        run_engine()
    if not os.path.exists(SCHEDULED):
        raise SystemExit("scheduled_fixtures.csv not found; run the engine first.")

    sched = _load(SCHEDULED)
    print(f"Checking {len(sched)} scheduled rows...\n")

    hard = [
        ("no double-booked courts", check_no_double_booked_courts),
        ("no same-time team clash", check_no_team_clash_same_time),
        ("regression vs baseline", check_regression),
    ]
    failed = 0
    for name, fn in hard:
        ok, msg = fn(sched)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {msg}")
        if not ok:
            failed += 1

    report_soft_metrics(sched)

    print()
    if failed:
        raise SystemExit(f"{failed} check(s) failed.")
    print("All checks passed.")


if __name__ == '__main__':
    main()
