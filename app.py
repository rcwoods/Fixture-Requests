#!/usr/bin/env python3
"""
MBA Fixture Scheduler - web app
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

LOGO = os.path.join(BASE, 'mckinnon_logo.png')
MAROON, GOLD, NAVY = "#881840", "#F8B020", "#082038"

st.set_page_config(
    page_title="MBA Fixture Scheduler",
    page_icon=LOGO if os.path.exists(LOGO) else "🏀",
    layout="wide",
)

# Brand polish: keep it light-touch and readable in any theme.
st.markdown(f"""
<style>
.block-container {{ padding-top: 2.2rem; max-width: 1100px; }}
.mba-title {{ font-size: 1.55rem; font-weight: 600; color: {MAROON};
              line-height: 1.15; margin: 0; }}
.mba-sub {{ font-size: .95rem; color: #6B6B6B; margin: 2px 0 0; }}
.mba-rule {{ border: none; border-top: 3px solid {GOLD}; margin: 6px 0 14px; }}
button[data-baseweb="tab"] {{ font-weight: 600; }}
div[data-testid="stExpander"] details summary p {{ font-weight: 600; }}
</style>
""", unsafe_allow_html=True)

if os.path.exists(LOGO):
    st.logo(LOGO, size="large")


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
        return f"✓ {label} · {n} rows"
    return f"◦ {label} · not loaded"


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


def _norm_round(x):
    """Normalise a round label so '6', 6 and '6.0' all compare equal."""
    if x is None:
        return ''
    s = str(x).strip()
    if s == '' or s.lower() == 'nan':
        return ''
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return s


# ----- division helpers -----------------------------------------------
# The division token is everything in a grade after day/age/gender, e.g.
# "Saturday U10 Boys Div  2" -> "Div 2", "Saturday U8 Mixed BLUE" -> "BLUE".

_COLOURS = ['BLUE', 'RED', 'YELLOW', 'GREEN', 'PINK', 'PURPLE']


def _div_of(grade):
    return ' '.join(str(grade).split()[3:])


def _canon_div(s):
    import re
    return re.sub(r'\s+', ' ', str(s)).strip()


def _div_sort_key(d):
    import re
    u = d.upper()
    if u in _COLOURS:
        return (0, _COLOURS.index(u), '')
    m = re.search(r'(\d+)', d)
    num = int(m.group(1)) if m else 999
    return (1, num, d)


def divisions_from_pre_fixtures():
    pf = load_csv(FILES['pre_fixtures'])
    if pf is None or 'grade' not in pf.columns:
        return []
    divs = {_canon_div(_div_of(g)) for g in pf['grade'] if _div_of(g)}
    return sorted({d for d in divs if d}, key=_div_sort_key)




# ----- blank/example templates + per-section download buttons ---------

TEMPLATES = {
    'teams': (
        "Grade,Team,Linked_Team1,Linked_Team2,Linked_Team3,Linked_Team4,"
        "Linked_Team5,Linked_Team6,Linked_Team7,Linked_Team8,Unavailable_Times\n"
        "Saturday U10 Boys Div  1,Example Team A,Example Team B,,,,,,,,>9 am\n"
        "Saturday U10 Boys Div  1,Example Team B,Example Team A,,,,,,,,\n"
    ),
    'timeslots': (
        "Venue,Playing Surface,Day,Time_Slots,Round\n"
        "Example Venue,Court 1,Saturday,\"8:15 am, 9 am, 9:45 am, 10:30 am\",\n"
        "Example Venue,Court 1,Saturday,\"10:30 am, 11:15 am\",6\n"
    ),
    'division_venues': (
        "Venue,Playing Surface,Preferred_Divisions\n"
        "Example Venue,Court 1,\"Div 1, Div 2, BLUE, RED\"\n"
    ),
    'pre_fixtures': (
        "organisation,competition,season,grade,round date,round,home team,"
        "away team,venue,playing surface,game date,game time,game alias\n"
        "McKinnon Basketball Association,Example Competition,Winter 2026,"
        "Saturday U10 Boys Div  1,13/6/2026,6,Example Home Team,Example Away Team,"
        ",,13/6/2026,,\n"
    ),
}


def _section_downloads(skey, filename):
    """One quiet control for all file in/out: template, current, replace."""
    stem = filename.rsplit('.', 1)[0]
    path = FILES[skey]
    with st.popover("Import / export CSV"):
        c1, c2 = st.columns(2)
        c1.download_button("Template", TEMPLATES[skey],
                           file_name=f"{stem}_template.csv", mime="text/csv",
                           key=f"tmpl_{skey}", use_container_width=True)
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = f.read()
            c2.download_button("Current CSV", data, file_name=filename,
                               mime="text/csv", key=f"cur_{skey}",
                               use_container_width=True)
        up = st.file_uploader(f"Replace {filename}", type="csv",
                              key=f"upio_{skey}")
        if up is not None:
            sig = f"{up.name}:{getattr(up, 'size', '')}"
            if st.session_state.get(f"upsig_{skey}") != sig:
                st.session_state[f"upsig_{skey}"] = sig
                save_csv(pd.read_csv(up, dtype=str), path)
                st.success(f"Loaded {filename}.")
                st.rerun()


# ----------------------------------------------------------------------
# sidebar - status + run
# ----------------------------------------------------------------------

st.sidebar.title("Fixture Scheduler")

_files = [('teams', 'teams.csv'), ('timeslots', 'timeslots.csv'),
          ('division_venues', 'division_venues.csv'),
          ('pre_fixtures', 'pre_fixtures.csv')]
_loaded = sum(os.path.exists(FILES[k]) for k, _ in _files)
st.sidebar.caption(f"Input files · {_loaded} of {len(_files)} loaded")
for key, label in _files:
    st.sidebar.write(file_status_line(label, FILES[key]))

st.sidebar.divider()
run_clicked = st.sidebar.button("▶  Generate fixtures", type="primary",
                                use_container_width=True)


# ----------------------------------------------------------------------
# header
# ----------------------------------------------------------------------

_title_html = ("<p class='mba-title'>McKinnon Basketball Association</p>"
               "<p class='mba-sub'>Fixture Scheduler</p>")
if os.path.exists(LOGO):
    _hc1, _hc2 = st.columns([1, 11], vertical_alignment="center")
    _hc1.image(LOGO, width=72)
    _hc2.markdown(_title_html, unsafe_allow_html=True)
else:
    st.markdown(_title_html, unsafe_allow_html=True)
st.markdown("<hr class='mba-rule'>", unsafe_allow_html=True)


# ----------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------

tab_inputs, tab_run, tab_results = st.tabs(
    ["1 · Inputs", "2 · Run", "3 · Results"])


# ---- TAB 1: INPUTS ---------------------------------------------------
with tab_inputs:
    st.header("Inputs")
    st.caption("Set up teams, venues and timeslots once. Add each round's "
               "matchups at the bottom.")

    # --- Teams: simple editable table ---
    for key, label, help_text in [
        ('teams', 'Teams',
         "One row per team. Linked_Team1..8 keeps siblings spaced or together; "
         "Unavailable_Times blocks early/late games, e.g. '<11 am' or '>9:45 am'."),
    ]:
        with st.expander(label, expanded=(key == 'teams')):
            st.caption(help_text)
            _section_downloads(key, os.path.basename(FILES[key]))
            df = load_csv(FILES[key])
            if df is None:
                st.caption("No teams.csv yet. Use **Import / export CSV** above "
                           "to upload one or start from the template.")
            else:
                edited = st.data_editor(df, num_rows="dynamic",
                                        use_container_width=True, key=f"ed_{key}",
                                        height=320)
                if st.button(f"Save {label}", key=f"save_{key}"):
                    save_csv(edited, FILES[key])
                    st.success(f"Saved {os.path.basename(FILES[key])}")

    # --- Division Venues: pick preferred divisions per court ---
    with st.expander("Division Venues", expanded=False):
        _section_downloads('division_venues', 'division_venues.csv')
        dv = load_csv(FILES['division_venues'])
        if dv is None:
            st.caption("No division_venues.csv yet. Use **Import / export CSV** "
                       "above to upload one or start from the template.")
        else:
            dv = dv.copy()
            dv['_label'] = (dv['Venue'].str.strip() + "  ·  "
                            + dv['Playing Surface'].str.strip())
            all_divs = divisions_from_pre_fixtures()
            if not all_divs:
                st.caption("Tip: load pre_fixtures.csv first and the division list "
                           "will be drawn from the round's actual grades.")
            st.caption("Pick the divisions each court should be preferred for. "
                       "Options come from the divisions in pre_fixtures.csv.")
            dchoice = st.selectbox("Court", list(dv['_label']), key="dv_court")
            d_idx = dv.index[dv['_label'] == dchoice][0]

            current_prefs = [_canon_div(p) for p in
                             str(dv.loc[d_idx, 'Preferred_Divisions']).split(',')
                             if p.strip()]
            # Keep any current pref even if it isn't in this round's grades.
            d_options = sorted(set(all_divs) | set(current_prefs), key=_div_sort_key)

            dv_key = f"divs_{d_idx}"
            if dv_key not in st.session_state:
                st.session_state[dv_key] = [p for p in current_prefs if p in d_options]

            qa1, qa2, qa3 = st.columns(3)
            if qa1.button("Select all", key=f"dall_{d_idx}"):
                st.session_state[dv_key] = list(d_options)
                st.rerun()
            if qa2.button("Clear", key=f"dclr_{d_idx}"):
                st.session_state[dv_key] = []
                st.rerun()
            if qa3.button("Reset", key=f"drst_{d_idx}"):
                st.session_state[dv_key] = [p for p in current_prefs if p in d_options]
                st.rerun()

            selected_divs = st.multiselect(
                "Preferred divisions  (open the dropdown to add; click × to remove)",
                d_options, key=dv_key)

            nd = len(selected_divs)
            st.caption(f"{nd} division{'s' if nd != 1 else ''} preferred at this court."
                       + ("" if nd else " No division will be steered here."))

            if st.button("Save court", key=f"savedv_{d_idx}", type="primary"):
                ordered = sorted(selected_divs, key=_div_sort_key)
                dv_disk = load_csv(FILES['division_venues'])
                dv_disk.loc[d_idx, 'Preferred_Divisions'] = ", ".join(ordered)
                save_csv(dv_disk, FILES['division_venues'])
                st.success(f"Saved {dchoice.replace('  ·  ', ' · ')} "
                           f"({len(ordered)} divisions).")

            # Copy this court's selected divisions to other courts at once.
            other_courts = [lab for lab in dv['_label'] if lab != dchoice]
            if other_courts:
                st.markdown("###### Copy these divisions to other courts")
                targets = st.multiselect("Apply the selection above to:",
                                         other_courts, key=f"dvcopy_{d_idx}")
                if st.button("Copy to selected courts", key=f"dvcopybtn_{d_idx}",
                             disabled=not targets):
                    ordered = sorted(selected_divs, key=_div_sort_key)
                    dv_disk = load_csv(FILES['division_venues'])
                    dv_disk['_label'] = (dv_disk['Venue'].str.strip() + "  ·  "
                                         + dv_disk['Playing Surface'].str.strip())
                    for lab in targets:
                        ix = dv_disk.index[dv_disk['_label'] == lab]
                        if len(ix):
                            dv_disk.loc[ix[0], 'Preferred_Divisions'] = ", ".join(ordered)
                    save_csv(dv_disk.drop(columns=['_label']), FILES['division_venues'])
                    st.success(f"Copied {len(ordered)} divisions to "
                               f"{len(targets)} court(s).")

            # Add a brand-new court to division_venues.
            st.markdown("###### Add a new court")
            ac1, ac2 = st.columns(2)
            new_v = ac1.text_input("Venue", key=f"dvnewv_{d_idx}",
                                   placeholder="e.g. New Sports Centre")
            new_s = ac2.text_input("Playing surface", key=f"dvnews_{d_idx}",
                                   placeholder="e.g. Court 1")
            if st.button("Add court", key=f"dvadd_{d_idx}",
                         disabled=not (new_v.strip() and new_s.strip())):
                dv_disk = load_csv(FILES['division_venues'])
                dup = ((dv_disk['Venue'].astype(str).str.strip() == new_v.strip())
                       & (dv_disk['Playing Surface'].astype(str).str.strip() == new_s.strip())).any()
                if dup:
                    st.warning("That court already exists.")
                else:
                    row = {c: '' for c in dv_disk.columns}
                    row.update({'Venue': new_v.strip(), 'Playing Surface': new_s.strip(),
                                'Preferred_Divisions': ''})
                    dv_disk = pd.concat([dv_disk, pd.DataFrame([row])], ignore_index=True)
                    save_csv(dv_disk, FILES['division_venues'])
                    st.success(f"Added {new_v.strip()} · {new_s.strip()}. "
                               "Add its timeslots below, then set its divisions here.")
                    st.rerun()

    # --- Timeslots: dedicated picker (per court, per round) ---
    with st.expander("Timeslots", expanded=True):
        _section_downloads('timeslots', 'timeslots.csv')
        ts = load_csv(FILES['timeslots'])
        if ts is None:
            st.caption("No timeslots.csv yet. Use **Import / export CSV** above "
                       "to upload one or start from the template.")
        else:
            ts = ts.copy()
            ts['_rnd'] = ts['Round'].map(_norm_round) if 'Round' in ts.columns else ''

            # Which round are we editing? Default = used for every round.
            pf = load_csv(FILES['pre_fixtures'])
            pf_rounds = []
            if pf is not None and 'round' in pf.columns:
                pf_rounds = sorted({_norm_round(r) for r in pf['round']} - {''},
                                   key=lambda x: (len(x), x))
            round_options = ["Default (all rounds)"] + [f"Round {r}" for r in pf_rounds]
            round_sel = st.selectbox(
                "Editing slots for", round_options,
                help="Default applies to every round. Pick a round to override just "
                     "that week - handy for holidays, finals, or a closed court.")
            editing_default = (round_sel == round_options[0])
            rnd_val = '' if editing_default else round_sel.replace("Round ", "")

            st.caption("Pick a court, set its first and last game, and the slots fill "
                       "in. Remove any chip the court can't run, or add one. Then Save.")

            # Court list = every distinct court in the file.
            courts = ts[['Venue', 'Playing Surface', 'Day']].drop_duplicates().copy()
            courts['_label'] = (courts['Venue'].str.strip() + "  ·  "
                                + courts['Playing Surface'].str.strip() + "  ·  "
                                + courts['Day'].str.strip())
            choice = st.selectbox("Court to edit", list(courts['_label']))
            crow = courts[courts['_label'] == choice].iloc[0]
            venue, surface, day = crow['Venue'], crow['Playing Surface'], crow['Day']

            def _find_idx(df, rv):
                m = ((df['Venue'].astype(str).str.strip() == str(venue).strip())
                     & (df['Playing Surface'].astype(str).str.strip() == str(surface).strip())
                     & (df['Day'].astype(str).str.strip() == str(day).strip())
                     & (df['_rnd'] == _norm_round(rv)))
                idx = df.index[m]
                return idx[0] if len(idx) else None

            base_idx = _find_idx(ts, '')
            ov_idx = None if editing_default else _find_idx(ts, rnd_val)
            has_override = ov_idx is not None
            src_idx = ov_idx if has_override else base_idx
            current = _parse_slots_to_disp(ts.loc[src_idx, 'Time_Slots']) \
                if src_idx is not None else []

            if editing_default:
                st.caption("Editing the default slots, used for every round without "
                           "an override.")
            elif has_override:
                st.info(f"This court already has a Round {rnd_val} override - editing it.")
            else:
                st.caption(f"No Round {rnd_val} override yet. Starting from the default; "
                           f"saving will create one for Round {rnd_val} only.")

            ctx = f"{choice}|{rnd_val or 'def'}"
            ms_key = f"slots_{ctx}"
            if ms_key not in st.session_state:
                st.session_state[ms_key] = current

            c1, c2, c3, c4 = st.columns([2, 2, 1.4, 1.6])
            cur = st.session_state[ms_key]
            first_default = cur[0] if cur else "9:00 AM"
            last_default = cur[-1] if cur else "6:00 PM"
            first = c1.selectbox("First game", ALL_TIMES,
                                 index=ALL_TIMES.index(first_default)
                                 if first_default in ALL_TIMES else 0,
                                 key=f"first_{ctx}")
            last = c2.selectbox("Last game", ALL_TIMES,
                                index=ALL_TIMES.index(last_default)
                                if last_default in ALL_TIMES else len(ALL_TIMES) - 1,
                                key=f"last_{ctx}")
            gap = c3.selectbox("Gap", GAP_OPTIONS, key=f"gap_{ctx}",
                               format_func=lambda g: f"{g} min")
            c4.write("")
            c4.write("")
            if c4.button("Generate", key=f"gen_{ctx}", use_container_width=True):
                st.session_state[ms_key] = _gen_range(first, last, gap)
                st.rerun()

            b1, b2 = st.columns(2)
            if b1.button("Add this range to slots", key=f"add_{ctx}"):
                merged = set(st.session_state[ms_key]) | set(_gen_range(first, last, gap))
                st.session_state[ms_key] = sorted(merged, key=lambda d: _disp_to_min(d))
                st.rerun()
            if b2.button("Reset", key=f"reset_{ctx}"):
                st.session_state[ms_key] = current
                st.rerun()

            options = sorted(set(ALL_TIMES) | set(st.session_state[ms_key]),
                             key=lambda d: _disp_to_min(d))
            selected = st.multiselect(
                "Slots  (open the dropdown to add a time; click × to remove)",
                options, key=ms_key)

            n = len(selected)
            st.caption(f"{n} slot{'s' if n != 1 else ''} selected"
                       + ("" if n else " - this court will be skipped for "
                          + ("every round." if editing_default else f"Round {rnd_val}.")))

            def _upsert(rv, time_str):
                disk = load_csv(FILES['timeslots'])
                if 'Round' not in disk.columns:
                    disk['Round'] = ''
                disk['Round'] = disk['Round'].fillna('').astype(str)
                m = ((disk['Venue'].astype(str).str.strip() == str(venue).strip())
                     & (disk['Playing Surface'].astype(str).str.strip() == str(surface).strip())
                     & (disk['Day'].astype(str).str.strip() == str(day).strip())
                     & (disk['Round'].map(_norm_round) == _norm_round(rv)))
                idx = disk.index[m]
                if len(idx):
                    disk.loc[idx[0], 'Time_Slots'] = time_str
                else:
                    row = {c: '' for c in disk.columns}
                    row.update({'Venue': venue, 'Playing Surface': surface,
                                'Day': day, 'Time_Slots': time_str,
                                'Round': _norm_round(rv)})
                    disk = pd.concat([disk, pd.DataFrame([row])], ignore_index=True)
                save_csv(disk, FILES['timeslots'])

            save_label = ("Save default" if editing_default
                          else f"Save Round {rnd_val} override")
            cols_save = st.columns([2, 2]) if has_override else [st]
            if cols_save[0].button(save_label, key=f"save_{ctx}", type="primary"):
                ordered = sorted(selected, key=lambda d: _disp_to_min(d))
                file_str = ", ".join(_min_to_file(_disp_to_min(d)) for d in ordered)
                _upsert(rnd_val, file_str)
                where = "default" if editing_default else f"Round {rnd_val}"
                st.success(f"Saved {where}: {choice.replace('  ·  ', ' · ')} "
                           f"({len(ordered)} slots).")
            if has_override and cols_save[1].button(
                    "Remove override (use default)", key=f"rmov_{ctx}"):
                disk = load_csv(FILES['timeslots'])
                m = ((disk['Venue'].astype(str).str.strip() == str(venue).strip())
                     & (disk['Playing Surface'].astype(str).str.strip() == str(surface).strip())
                     & (disk['Day'].astype(str).str.strip() == str(day).strip())
                     & (disk.get('Round', pd.Series([''] * len(disk))).map(_norm_round)
                        == _norm_round(rnd_val)))
                save_csv(disk[~m], FILES['timeslots'])
                st.session_state.pop(ms_key, None)
                st.success(f"Removed the Round {rnd_val} override for this court.")
                st.rerun()

            # Copy the current slots to other courts at once.
            other_ts = [lab for lab in courts['_label'] if lab != choice]
            if other_ts:
                where_txt = "default" if editing_default else f"Round {rnd_val}"
                st.markdown("###### Copy these slots to other courts")
                ts_targets = st.multiselect(
                    f"Apply the slots above ({where_txt}) to:", other_ts,
                    key=f"tscopy_{ctx}")
                if st.button("Copy to selected courts", key=f"tscopybtn_{ctx}",
                             disabled=not ts_targets):
                    ordered = sorted(selected, key=lambda d: _disp_to_min(d))
                    file_str = ", ".join(_min_to_file(_disp_to_min(d)) for d in ordered)
                    disk = load_csv(FILES['timeslots'])
                    if 'Round' not in disk.columns:
                        disk['Round'] = ''
                    disk['Round'] = disk['Round'].fillna('').astype(str)
                    label_map = {r['_label']: (r['Venue'], r['Playing Surface'], r['Day'])
                                 for _, r in courts.iterrows()}
                    for lab in ts_targets:
                        tv, tsurf, tday = label_map[lab]
                        m = ((disk['Venue'].astype(str).str.strip() == str(tv).strip())
                             & (disk['Playing Surface'].astype(str).str.strip() == str(tsurf).strip())
                             & (disk['Day'].astype(str).str.strip() == str(tday).strip())
                             & (disk['Round'].map(_norm_round) == _norm_round(rnd_val)))
                        ix = disk.index[m]
                        if len(ix):
                            disk.loc[ix[0], 'Time_Slots'] = file_str
                        else:
                            row = {c: '' for c in disk.columns}
                            row.update({'Venue': tv, 'Playing Surface': tsurf,
                                        'Day': tday, 'Time_Slots': file_str,
                                        'Round': _norm_round(rnd_val)})
                            disk = pd.concat([disk, pd.DataFrame([row])], ignore_index=True)
                    save_csv(disk, FILES['timeslots'])
                    st.success(f"Copied {len(ordered)} slots ({where_txt}) to "
                               f"{len(ts_targets)} court(s).")

            # Add a brand-new court (Venue + Surface + Day) to timeslots.
            st.markdown("###### Add a new court")
            nc1, nc2, nc3 = st.columns([2, 1.4, 1])
            tnew_v = nc1.text_input("Venue", key="tsnewv",
                                    placeholder="e.g. New Sports Centre")
            tnew_s = nc2.text_input("Playing surface", key="tsnews",
                                    placeholder="e.g. Court 1")
            tnew_d = nc3.selectbox("Day", ["Saturday", "Sunday"], key="tsnewd")
            if st.button("Add court", key="tsadd",
                         disabled=not (tnew_v.strip() and tnew_s.strip())):
                disk = load_csv(FILES['timeslots'])
                if 'Round' in disk.columns:
                    disk['Round'] = disk['Round'].fillna('').astype(str)
                dup = ((disk['Venue'].astype(str).str.strip() == tnew_v.strip())
                       & (disk['Playing Surface'].astype(str).str.strip() == tnew_s.strip())
                       & (disk['Day'].astype(str).str.strip() == tnew_d)
                       & (disk.get('Round', pd.Series([''] * len(disk))).map(_norm_round) == '')).any()
                if dup:
                    st.warning("That court and day already exists.")
                else:
                    row = {c: '' for c in disk.columns}
                    row.update({'Venue': tnew_v.strip(), 'Playing Surface': tnew_s.strip(),
                                'Day': tnew_d, 'Time_Slots': ''})
                    if 'Round' in disk.columns:
                        row['Round'] = ''
                    disk = pd.concat([disk, pd.DataFrame([row])], ignore_index=True)
                    save_csv(disk, FILES['timeslots'])
                    st.success(f"Added {tnew_v.strip()} · {tnew_s.strip()} · {tnew_d}. "
                               "Select it above to set its slots.")
                    st.rerun()

    st.divider()
    st.subheader("Round matchups")
    st.caption("The per-round file from PlayHQ or your matchup generator: "
               "home vs away per grade, no venue or time yet.")
    _section_downloads('pre_fixtures', 'pre_fixtures.csv')
    pf = load_csv(FILES['pre_fixtures'])
    if pf is None:
        st.caption("No pre_fixtures.csv yet. Use **Import / export CSV** above "
                   "to upload this round's matchups.")
    else:
        rounds = sorted(pf['round'].unique(), key=lambda x: (len(str(x)), str(x))) \
            if 'round' in pf.columns else []
        st.write(f"Loaded **{len(pf)}** matchups"
                 + (f" across rounds {', '.join(map(str, rounds))}." if rounds else "."))
        st.dataframe(pf.head(20), use_container_width=True, height=240)



# ---- TAB 2: RUN ------------------------------------------------------
with tab_run:
    st.header("Run")
    st.caption("Press **Generate fixtures** in the sidebar to check the inputs "
               "and schedule every round.")

    if run_clicked:
        # Step 1: preflight (reuses run_fixtures.preflight)
        st.subheader("Step 1 - input checks")
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
            st.error("Stopping - fix the errors above and run again.")
            st.stop()

        # Step 2: run the engine (unchanged), capturing its log
        st.subheader("Step 2 - scheduling")
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
        st.subheader("Step 3 - summary")
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
        st.info("Waiting - press **Generate fixtures** in the sidebar.")


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