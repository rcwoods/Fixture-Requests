#!/usr/bin/env python3
"""
run_fixtures.py  —  one command to schedule a round and understand the result.

What it does, in order:
  1. PRE-FLIGHT: checks your four input files for the mistakes that usually
     cause confusing results (venue names that don't match between files,
     teams in pre_fixtures that aren't in teams.csv, bad Unavailable_Times
     syntax, linked teams that point at nothing, empty timeslot rows,
     duplicate fixtures). Hard errors stop the run; warnings just print.
  2. SCHEDULE: runs your existing fixture_requests_vW26.py unchanged.
  3. SUMMARY: writes a short plain-English report (fixture_summary.txt) and
     prints it, so you don't have to open the CSVs to see what happened,
     what didn't fit and why, and what to change.

Usage:
    python run_fixtures.py

Everything is driven by the files in this folder. Nothing here changes the
scheduling logic — it only checks inputs and explains outputs.
"""

import os
import re
import sys
import subprocess
from collections import defaultdict

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

SCHEDULER = os.path.join(BASE, 'fixture_requests_vW26.py')
TEAMS = os.path.join(BASE, 'teams.csv')
TIMESLOTS = os.path.join(BASE, 'timeslots.csv')
DIVISION_VENUES = os.path.join(BASE, 'division_venues.csv')
PRE_FIXTURES = os.path.join(BASE, 'pre_fixtures.csv')
SCHEDULED = os.path.join(BASE, 'scheduled_fixtures.csv')
UNSCHEDULED = os.path.join(BASE, 'unscheduled_fixtures.csv')
SUMMARY = os.path.join(BASE, 'fixture_summary.txt')

# Young age groups we flag if they get pushed late, and the cutoff for "late".
LATE_FLAGS = {'U8': '12:30 pm', 'U10': '12:30 pm', 'U12': '3:00 pm'}


# ----------------------------------------------------------------------
# small shared helpers (self-contained; do not import the engine)
# ----------------------------------------------------------------------

def _load(path):
    return pd.read_csv(path, dtype=str).fillna('')


def time_to_minutes(t):
    s = str(t).strip().lower().replace(' ', '')
    m = re.match(r'(\d{1,2})(?::(\d{2}))?(am|pm)?', s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2)) if m.group(2) else 0
    ap = m.group(3)
    if ap == 'pm' and h != 12:
        h += 12
    if ap == 'am' and h == 12:
        h = 0
    return h * 60 + mi


def fmt(mins):
    if mins is None:
        return '?'
    h, m = divmod(mins, 60)
    ap = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{ap}"


def age_of(grade):
    m = re.search(r'U(\d+)', str(grade))
    return f"U{m.group(1)}" if m else None


def day_of(grade):
    s = str(grade)
    if s.startswith('Saturday'):
        return 'Saturday'
    if s.startswith('Sunday'):
        return 'Sunday'
    return 'Other'


# ----------------------------------------------------------------------
# 1. PRE-FLIGHT
# ----------------------------------------------------------------------

def preflight():
    """Return (errors, warnings, notes) lists of human-readable strings."""
    errors, warnings, notes = [], [], []

    for label, path in [('teams.csv', TEAMS), ('timeslots.csv', TIMESLOTS),
                        ('division_venues.csv', DIVISION_VENUES),
                        ('pre_fixtures.csv', PRE_FIXTURES)]:
        if not os.path.exists(path):
            errors.append(f"Missing input file: {label}")
    if errors:
        return errors, warnings, notes

    teams = _load(TEAMS)
    slots = _load(TIMESLOTS)
    divven = _load(DIVISION_VENUES)
    pre = _load(PRE_FIXTURES)

    # --- venue+court consistency between the two venue files ---
    def vc(df):
        return set(zip(df['Venue'].str.strip(), df['Playing Surface'].str.strip()))
    slot_courts = vc(slots)
    divven_courts = vc(divven)
    only_slots = slot_courts - divven_courts
    only_divven = divven_courts - slot_courts
    for v, c in sorted(only_divven):
        warnings.append(f"'{v} - {c}' is in division_venues.csv but not timeslots.csv "
                        f"(it has no time slots, so nothing can be placed there).")
    for v, c in sorted(only_slots):
        warnings.append(f"'{v} - {c}' is in timeslots.csv but not division_venues.csv "
                        f"(no division will be steered to it).")

    # --- empty timeslot rows ---
    empty_slot_rows = slots[slots['Time_Slots'].str.strip() == '']
    for _, r in empty_slot_rows.iterrows():
        notes.append(f"'{r['Venue']} - {r['Playing Surface']}' ({r['Day']}) has no time "
                     f"slots listed, so it will be skipped.")

    # --- teams in pre_fixtures missing from teams.csv ---
    team_set = set(teams['Team'].str.strip())
    pre_teams = set()
    for col in ('home team', 'away team'):
        if col in pre.columns:
            pre_teams |= set(pre[col].str.strip())
    pre_teams = {t for t in pre_teams if t and t.upper() != 'BYE'}
    missing = sorted(pre_teams - team_set)
    if missing:
        warnings.append(f"{len(missing)} team(s) appear in pre_fixtures.csv but not in "
                        f"teams.csv, so their linked-team and Unavailable_Times rules "
                        f"won't apply. First few: {', '.join(missing[:5])}"
                        + ('...' if len(missing) > 5 else ''))

    # --- Unavailable_Times syntax ---
    token_re = re.compile(r'^[<>]\s*\d{1,2}(:\d{2})?\s*(am|pm)$', re.I)
    bad_unavail = []
    for _, r in teams.iterrows():
        raw = str(r['Unavailable_Times']).strip()
        if not raw:
            continue
        for tok in raw.split(';'):
            tok = tok.strip()
            if tok and not token_re.match(tok):
                bad_unavail.append((r['Team'], tok))
    for team, tok in bad_unavail:
        warnings.append(f"Unavailable_Times for '{team}' has an entry I can't read: "
                        f"'{tok}'. Use forms like '<11 am' (no games before 11am) "
                        f"or '>9:45 am' (no games after 9:45am), separated by ';'.")

    # --- linked teams pointing at nothing ---
    linked_cols = [c for c in teams.columns if c.startswith('Linked_Team')]
    dangling = set()
    for _, r in teams.iterrows():
        for c in linked_cols:
            v = str(r[c]).strip()
            if v and v not in team_set:
                dangling.add(v)
    if dangling:
        warnings.append(f"{len(dangling)} linked-team name(s) in teams.csv don't match any "
                        f"team in the Team column (likely a typo or trailing space). "
                        f"First few: {', '.join(sorted(dangling)[:5])}"
                        + ('...' if len(dangling) > 5 else ''))

    # --- duplicate fixtures ---
    if {'grade', 'round', 'home team', 'away team'} <= set(pre.columns):
        key = pre[['round', 'grade', 'home team', 'away team']].apply(
            lambda x: tuple(s.strip() for s in x), axis=1)
        dup = key[key.duplicated()].nunique()
        if dup:
            warnings.append(f"{dup} duplicate fixture(s) in pre_fixtures.csv "
                            f"(same round/grade/home/away). The scheduler de-dupes these, "
                            f"but check they aren't a mistake.")

    return errors, warnings, notes


# ----------------------------------------------------------------------
# 2. RUN THE SCHEDULER (unchanged engine)
# ----------------------------------------------------------------------

def run_scheduler():
    print("\nScheduling... (running fixture_requests_vW26.py)\n")
    result = subprocess.run([sys.executable, SCHEDULER], cwd=BASE)
    return result.returncode == 0


# ----------------------------------------------------------------------
# 3. SUMMARY
# ----------------------------------------------------------------------

def _explain_reason(reason):
    """Turn an engine reason into a one-line suggested fix."""
    r = str(reason).lower()
    if 'capacity' in r or 'no valid slot' in r:
        return ("not enough court time on that day for this age group — add a slot or "
                "court in timeslots.csv, or move a fixture to the other day.")
    if 'unavailable' in r or 'request' in r:
        return ("both teams' Unavailable_Times left no legal slot — relax one team's "
                "window in teams.csv.")
    return "see reason; usually fixed by adding a slot or relaxing a constraint."


def build_summary():
    lines = []
    out = lines.append

    sched = _load(SCHEDULED) if os.path.exists(SCHEDULED) else pd.DataFrame()
    unsched = _load(UNSCHEDULED) if os.path.exists(UNSCHEDULED) else pd.DataFrame()
    teams = _load(TEAMS)

    # normalise scheduled columns (template format: lowercase)
    g = 'grade' if 'grade' in sched.columns else 'Grade'
    ht = 'home team' if 'home team' in sched.columns else 'Team 1'
    at = 'away team' if 'away team' in sched.columns else 'Team 2'
    rd = 'round' if 'round' in sched.columns else 'Round'
    tm = 'game time' if 'game time' in sched.columns else 'Time'
    ven = 'venue' if 'venue' in sched.columns else 'Venue'

    games = sched[sched[at].str.upper() != 'BYE'] if not sched.empty else sched
    byes = sched[sched[at].str.upper() == 'BYE'] if not sched.empty else sched

    out("=" * 64)
    out("FIXTURE SUMMARY")
    out("=" * 64)
    rounds = sorted(sched[rd].unique(), key=lambda x: (len(str(x)), str(x))) if not sched.empty else []
    out(f"Rounds processed : {', '.join(map(str, rounds)) or '(none)'}")
    out(f"Games scheduled  : {len(games)}")
    out(f"Byes             : {len(byes)}")
    out(f"Unscheduled      : {len(unsched)}")
    out("")

    # --- per round / per day capacity ---
    if not games.empty:
        out("-" * 64)
        out("Where the games landed (per round, per day)")
        out("-" * 64)
        games = games.copy()
        games['_day'] = games[g].apply(day_of)
        for r in rounds:
            sub = games[games[rd] == r]
            bits = []
            for d in ('Saturday', 'Sunday'):
                n = len(sub[sub['_day'] == d])
                if n:
                    bits.append(f"{d} {n}")
            out(f"  Round {r}: " + ", ".join(bits))
        out("")

    # --- unscheduled with plain fix ---
    if not unsched.empty:
        out("-" * 64)
        out(f"Couldn't be scheduled ({len(unsched)}) — and what to do")
        out("-" * 64)
        for _, r in unsched.iterrows():
            rr = r.get('round', r.get('Round', ''))
            grd = r.get('grade', r.get('Grade', ''))
            h = r.get('home team', r.get('Team 1', ''))
            a = r.get('away team', r.get('Team 2', ''))
            reason = r.get('reason', r.get('Reason', ''))
            out(f"  R{rr} {grd}: {h} v {a}")
            out(f"      Fix: {_explain_reason(reason)}")
        out("")

    # --- Unavailable_Times not honoured ---
    if not games.empty:
        unavail = {row['Team'].strip(): str(row['Unavailable_Times']).strip()
                   for _, row in teams.iterrows() if str(row['Unavailable_Times']).strip()}

        def violates(window, placed_min):
            for tok in window.split(';'):
                tok = tok.strip().lower()
                m = re.match(r'([<>])\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))', tok)
                if not m:
                    continue
                lim = time_to_minutes(m.group(2))
                if lim is None:
                    continue
                if m.group(1) == '<' and placed_min < lim:   # no games before lim
                    return True
                if m.group(1) == '>' and placed_min > lim:    # no games after lim
                    return True
            return False

        viol = []
        for _, r in games.iterrows():
            pm = time_to_minutes(r[tm])
            if pm is None:
                continue
            for col in (ht, at):
                t = str(r[col]).strip()
                if t in unavail and violates(unavail[t], pm):
                    viol.append((r[rd], t, unavail[t], r[tm], r[g]))
        if viol:
            out("-" * 64)
            out(f"Unavailable_Times not fully honoured ({len(viol)})")
            out("(usually means every legal slot was already taken)")
            out("-" * 64)
            for rr, t, w, when, grd in viol[:40]:
                out(f"  R{rr} {t} (wants {w}) -> placed {when}  [{grd}]")
            if len(viol) > 40:
                out(f"  ...and {len(viol) - 40} more")
            out("")

    # --- young-age-late flags ---
    if not games.empty:
        out("-" * 64)
        out("Young age groups starting late (worth a glance)")
        out("-" * 64)
        any_flag = False
        gg = games.copy()
        gg['_age'] = gg[g].apply(age_of)
        gg['_min'] = gg[tm].apply(time_to_minutes)
        for ag, cutoff in LATE_FLAGS.items():
            cut = time_to_minutes(cutoff)
            late = gg[(gg['_age'] == ag) & (gg['_min'].notna()) & (gg['_min'] >= cut)]
            if len(late):
                any_flag = True
                out(f"  {ag} after {cutoff}: {len(late)} game(s)")
                for _, r in late.head(8).iterrows():
                    out(f"      R{r[rd]} {r[tm]:>8}  {r[g]}  {r[ht]} v {r[at]}")
        if not any_flag:
            out("  None — all young age groups are in the morning/early slots. ")
        out("")

    out("=" * 64)
    out("Upload file: scheduled_fixtures.csv  (PlayHQ template format)")
    out("=" * 64)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def main():
    print("=" * 64)
    print("STEP 1 — checking your input files")
    print("=" * 64)
    errors, warnings, notes = preflight()

    for e in errors:
        print(f"  ERROR:   {e}")
    for w in warnings:
        print(f"  WARNING: {w}")
    for n in notes:
        print(f"  note:    {n}")
    if not (errors or warnings or notes):
        print("  All four input files look consistent.")

    if errors:
        print("\nStopping — fix the errors above and run again.")
        sys.exit(1)
    if warnings:
        print("\nWarnings won't stop the run, but check them if the result looks off.")

    print("\n" + "=" * 64)
    print("STEP 2 — scheduling")
    if not run_scheduler():
        print("\nThe scheduler reported a problem. See the messages above.")
        sys.exit(1)

    print("\n" + "=" * 64)
    print("STEP 3 — summary")
    print("=" * 64)
    report = build_summary()
    print(report)
    with open(SUMMARY, 'w') as f:
        f.write(report + "\n")
    print(f"\n(Saved to {os.path.basename(SUMMARY)})")


if __name__ == '__main__':
    main()
