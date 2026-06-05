#!/usr/bin/env python3
"""
MBA Fixture Scheduler — web app
================================

A single local site that combines the whole process:
  • edit Teams, Timeslots and Division-Venues in the browser
  • load the round's matchups (pre_fixtures.csv)
  • check inputs, run the scheduler, read the plain-English summary
  • download the PlayHQ upload file

It does NOT change the scheduling engine. It drives the existing
fixture_requests_vW26.py and reuses the preflight + summary code from
run_fixtures.py, so behaviour is identical to running from the command line.

Run it with:
    streamlit run app.py

Then your browser opens automatically. Everything reads and writes the CSV
files sitting next to this script, so you can still use the command line too.
"""

import os
import sys
import subprocess

import pandas as pd
import streamlit as st

import run_fixtures as rf   # reuse preflight() and build_summary()

BASE = os.path.dirname(os.path.abspath(__file__))

FILES = {
    'teams': os.path.join(BASE, 'teams.csv'),
    'timeslots': os.path.join(BASE, 'timeslots.csv'),
    'division_venues': os.path.join(BASE, 'division_venues.csv'),
    'pre_fixtures': os.path.join(BASE, 'pre_fixtures.csv'),
    'scheduled': os.path.join(BASE, 'scheduled_fixtures.csv'),
    'unscheduled': os.path.join(BASE, 'unscheduled_fixtures.csv'),
}

st.set_page_config(page_title="MBA Fixture Scheduler", page_icon="🏀", layout="wide")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def load_csv(path):
    if os.path.exists(path):
        return pd.read_csv(path, dtype=str).fillna('')
    return None


def save_csv(df, path):
    df.fillna('').to_csv(path, index=False)


def file_status_line(label, path):
    if os.path.exists(path):
        n = len(pd.read_csv(path, dtype=str))
        return f"✓ {label} ({n} rows)"
    return f"✗ {label} — missing"


# ----- timeslot helpers -----------------------------------------------
# Candidate start times offered in the pickers: every 15 minutes from
# 8:00 AM to 9:00 PM. Display form is "8:15 AM"; stored form matches the
# existing file style ("8:15 am", on-the-hour as "9 am").

def _min_to_disp(mins):
    h, m = divmod(mins, 60)
    ap = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}"


def _min_to_file(mins):
    h, m = divmod(mins, 60)
    ap = 'am' if h < 12 else 'pm'
    h12 = h % 12 or 12
    return f"{h12} {ap}" if m == 0 else f"{h12}:{m:02d} {ap}"


def _disp_to_min(s):
    import re
    m = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', str(s).strip(), re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ap == 'PM' and h != 12:
        h += 12
    if ap == 'AM' and h == 12:
        h = 0
    return h * 60 + mi


ALL_TIMES = [_min_to_disp(t) for t in range(8 * 60, 21 * 60 + 1, 5)]
GAP_OPTIONS = [45, 30, 40, 50, 60]


def _parse_slots_to_disp(time_slots_str):
    """Existing 'Time_Slots' cell -> list of canonical display strings."""
    out = []
    for tok in str(time_slots_str).split(','):
        mins = rf.time_to_minutes(tok.strip()) if tok.strip() else None
        if mins is not None and mins >= 0:
            out.append(_min_to_disp(mins))
    # de-dupe, keep sorted by time
    seen = sorted(set(out), key=lambda d: _disp_to_min(d))
    return seen


def _gen_range(first_disp, last_disp, gap):
    a, b = _disp_to_min(first_disp), _disp_to_min(last_disp)
    if a is None or b is None or b < a:
        return []
    return [_min_to_disp(t) for t in range(a, b + 1, gap)]


# ----------------------------------------------------------------------
# sidebar — status + run
# ----------------------------------------------------------------------

st.sidebar.title("🏀 Fixture Scheduler")
st.sidebar.caption("Everything reads/writes the CSV files next to this app.")

st.sidebar.subheader("Input files")
for key, label in [('teams', 'teams.csv'), ('timeslots', 'timeslots.csv'),
                   ('division_venues', 'division_venues.csv'),
                   ('pre_fixtures', 'pre_fixtures.csv')]:
    st.sidebar.write(file_status_line(label, FILES[key]))

st.sidebar.divider()
run_clicked = st.sidebar.button("▶  Generate fixtures", type="primary",
                                use_container_width=True)


# ----------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------

tab_inputs, tab_run, tab_results = st.tabs(
    ["1 · Inputs", "2 · Run", "3 · Results"])


# ---- TAB 1: INPUTS ---------------------------------------------------
with tab_inputs:
    st.header("Inputs")
    st.write("Edit the stable config below and save. Load the round's matchups "
             "at the bottom.")

    # --- Teams and Division Venues: simple editable tables ---
    for key, label, help_text in [
        ('teams', 'Teams',
         "One row per team. Set Linked_Team1..8 for siblings that should be "
         "spaced/clustered, and Unavailable_Times like '<11 am' (no games "
         "before 11am) or '>9:45 am' (no games after 9:45am), separated by ';'."),
        ('division_venues', 'Division Venues',
         "Which divisions each court prefers. Used to steer grades to the right "
         "venues."),
    ]:
        with st.expander(label, expanded=(key == 'teams')):
            st.caption(help_text)
            df = load_csv(FILES[key])
            if df is None:
                up = st.file_uploader(f"Upload {label.lower()} CSV", type="csv",
                                      key=f"up_{key}")
                if up is not None:
                    save_csv(pd.read_csv(up, dtype=str), FILES[key])
                    st.rerun()
            else:
                edited = st.data_editor(df, num_rows="dynamic",
                                        use_container_width=True, key=f"ed_{key}",
                                        height=320)
                if st.button(f"Save {label}", key=f"save_{key}"):
                    save_csv(edited, FILES[key])
                    st.success(f"Saved {os.path.basename(FILES[key])}")

    # --- Timeslots: dedicated picker (one court at a time) ---
    with st.expander("Timeslots", expanded=True):
        ts = load_csv(FILES['timeslots'])
        if ts is None:
            up = st.file_uploader("Upload timeslots CSV", type="csv", key="up_ts")
            if up is not None:
                save_csv(pd.read_csv(up, dtype=str), FILES['timeslots'])
                st.rerun()
        else:
            ts = ts.copy()
            ts['_label'] = (ts['Venue'].str.strip() + "  ·  "
                            + ts['Playing Surface'].str.strip() + "  ·  "
                            + ts['Day'].str.strip())
            st.caption("Pick a court, set its first and last game, and the slots "
                       "fill in automatically. Remove any chip the court can't run, "
                       "or add one. Then Save.")
            choice = st.selectbox("Court to edit", list(ts['_label']))
            row_idx = ts.index[ts['_label'] == choice][0]

            current = _parse_slots_to_disp(ts.loc[row_idx, 'Time_Slots'])
            ms_key = f"slots_{row_idx}"
            if ms_key not in st.session_state:
                st.session_state[ms_key] = current

            c1, c2, c3, c4 = st.columns([2, 2, 1.4, 1.6])
            cur = st.session_state[ms_key]
            first_default = cur[0] if cur else "9:00 AM"
            last_default = cur[-1] if cur else "6:00 PM"
            first = c1.selectbox("First game", ALL_TIMES,
                                 index=ALL_TIMES.index(first_default)
                                 if first_default in ALL_TIMES else 0,
                                 key=f"first_{row_idx}")
            last = c2.selectbox("Last game", ALL_TIMES,
                                index=ALL_TIMES.index(last_default)
                                if last_default in ALL_TIMES else len(ALL_TIMES) - 1,
                                key=f"last_{row_idx}")
            gap = c3.selectbox("Gap", GAP_OPTIONS, key=f"gap_{row_idx}",
                               format_func=lambda g: f"{g} min")
            c4.write("")
            c4.write("")
            if c4.button("Generate", key=f"gen_{row_idx}", use_container_width=True):
                st.session_state[ms_key] = _gen_range(first, last, gap)
                st.rerun()

            b1, b2 = st.columns(2)
            if b1.button("Add this range to slots", key=f"add_{row_idx}"):
                merged = set(st.session_state[ms_key]) | set(_gen_range(first, last, gap))
                st.session_state[ms_key] = sorted(merged, key=lambda d: _disp_to_min(d))
                st.rerun()
            if b2.button("Reset to saved", key=f"reset_{row_idx}"):
                st.session_state[ms_key] = current
                st.rerun()

            # Options must include any current selection so the widget never errors.
            options = sorted(set(ALL_TIMES) | set(st.session_state[ms_key]),
                             key=lambda d: _disp_to_min(d))
            selected = st.multiselect(
                "Slots for this court  (drop the dropdown to add a time; click × to remove)",
                options, key=ms_key)

            n = len(selected)
            st.caption(f"{n} slot{'s' if n != 1 else ''} selected"
                       + (" — second block / midday break is just a gap in the chips."
                          if n else " — this court will be skipped."))

            if st.button("Save this court", key=f"savecourt_{row_idx}", type="primary"):
                ordered = sorted(selected, key=lambda d: _disp_to_min(d))
                file_str = ", ".join(_min_to_file(_disp_to_min(d)) for d in ordered)
                ts_disk = load_csv(FILES['timeslots'])
                ts_disk.loc[row_idx, 'Time_Slots'] = file_str
                save_csv(ts_disk, FILES['timeslots'])
                st.success(f"Saved {choice.replace('  ·  ', ' · ')} "
                           f"({len(ordered)} slots).")

    st.divider()
    st.subheader("Round matchups — pre_fixtures.csv")
    st.caption("This is the per-round file from your matchup generator / PlayHQ "
               "(home vs away per grade, no venue or time yet). Upload it here.")
    up = st.file_uploader("Upload pre_fixtures.csv", type="csv", key="up_pre")
    if up is not None:
        save_csv(pd.read_csv(up, dtype=str), FILES['pre_fixtures'])
        st.success("Loaded pre_fixtures.csv")
    pf = load_csv(FILES['pre_fixtures'])
    if pf is not None:
        rounds = sorted(pf['round'].unique(), key=lambda x: (len(str(x)), str(x))) \
            if 'round' in pf.columns else []
        st.write(f"Loaded **{len(pf)}** matchups"
                 + (f" across rounds {', '.join(map(str, rounds))}." if rounds else "."))
        st.dataframe(pf.head(20), use_container_width=True, height=240)



# ---- TAB 2: RUN ------------------------------------------------------
with tab_run:
    st.header("Run")
    st.write("Press **Generate fixtures** (sidebar) to check the inputs and "
             "schedule the round. Results appear here and in the Results tab.")

    if run_clicked:
        # Step 1: preflight (reuses run_fixtures.preflight)
        st.subheader("Step 1 — input checks")
        errors, warnings, notes = rf.preflight()
        for e in errors:
            st.error(e)
        for w in warnings:
            st.warning(w)
        for n in notes:
            st.caption("• " + n)
        if not (errors or warnings or notes):
            st.success("All four input files look consistent.")

        if errors:
            st.error("Stopping — fix the errors above and run again.")
            st.stop()

        # Step 2: run the engine (unchanged), capturing its log
        st.subheader("Step 2 — scheduling")
        engine_path = os.path.join(BASE, 'fixture_requests_vW26.py')
        with st.spinner("Scheduling all rounds…"):
            proc = subprocess.run([sys.executable, engine_path],
                                  cwd=BASE, capture_output=True, text=True)
        if proc.returncode != 0:
            st.error("The scheduler reported a problem:")
            st.code(proc.stderr or proc.stdout)
            st.stop()
        with st.expander("Engine log"):
            st.code(proc.stdout[-6000:] if proc.stdout else "(no output)")

        # Step 3: summary (reuses run_fixtures.build_summary)
        st.subheader("Step 3 — summary")
        report = rf.build_summary()
        with open(os.path.join(BASE, 'fixture_summary.txt'), 'w') as f:
            f.write(report + "\n")

        sched = load_csv(FILES['scheduled'])
        unsched = load_csv(FILES['unscheduled'])
        games = sched[sched['away team'].str.upper() != 'BYE'] if sched is not None else pd.DataFrame()
        byes = sched[sched['away team'].str.upper() == 'BYE'] if sched is not None else pd.DataFrame()

        m1, m2, m3 = st.columns(3)
        m1.metric("Games scheduled", len(games))
        m2.metric("Byes", len(byes))
        m3.metric("Unscheduled", 0 if unsched is None else len(unsched))

        st.text(report)
        st.session_state['ran'] = True
        st.success("Done. See the Results tab to filter and download.")
    else:
        st.info("Waiting — press **Generate fixtures** in the sidebar.")


# ---- TAB 3: RESULTS --------------------------------------------------
with tab_results:
    st.header("Results")
    sched = load_csv(FILES['scheduled'])
    if sched is None or sched.empty:
        st.info("No results yet. Run the scheduler from the Run tab.")
    else:
        # Filters
        cols = st.columns(3)
        rounds = sorted(sched['round'].unique(), key=lambda x: (len(str(x)), str(x)))
        r_sel = cols[0].selectbox("Round", ["All"] + list(map(str, rounds)))
        venues = sorted(v for v in sched['venue'].unique() if v)
        v_sel = cols[1].selectbox("Venue", ["All"] + venues)
        text = cols[2].text_input("Search team / grade")

        view = sched.copy()
        if r_sel != "All":
            view = view[view['round'].astype(str) == r_sel]
        if v_sel != "All":
            view = view[view['venue'] == v_sel]
        if text:
            t = text.lower()
            mask = (view['grade'].str.lower().str.contains(t)
                    | view['home team'].str.lower().str.contains(t)
                    | view['away team'].str.lower().str.contains(t))
            view = view[mask]

        show_cols = ['round', 'grade', 'venue', 'playing surface', 'game date',
                     'game time', 'home team', 'away team']
        show_cols = [c for c in show_cols if c in view.columns]
        st.dataframe(view[show_cols], use_container_width=True, height=420)

        # Unscheduled
        unsched = load_csv(FILES['unscheduled'])
        if unsched is not None and not unsched.empty:
            st.subheader(f"Couldn't be scheduled ({len(unsched)})")
            cc = [c for c in ['round', 'grade', 'home team', 'away team', 'reason']
                  if c in unsched.columns]
            st.dataframe(unsched[cc], use_container_width=True)

        # Downloads
        st.divider()
        d1, d2 = st.columns(2)
        with open(FILES['scheduled']) as f:
            d1.download_button("⬇  scheduled_fixtures.csv (PlayHQ upload)",
                               f.read(), file_name="scheduled_fixtures.csv",
                               mime="text/csv", use_container_width=True)
        summ_path = os.path.join(BASE, 'fixture_summary.txt')
        if os.path.exists(summ_path):
            with open(summ_path) as f:
                d2.download_button("⬇  fixture_summary.txt", f.read(),
                                   file_name="fixture_summary.txt",
                                   mime="text/plain", use_container_width=True)
