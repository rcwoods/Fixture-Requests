#!/usr/bin/env python3
"""
Fixture scheduler with age priority (per day), exact timeslot adherence,
linked-team constraints, and multi-pass filling (including a final force-pass).

Key behaviour:
- Younger age groups (U8 → U20) are generally given earlier timeslots on a given day
  whenever they CAN legally fit there (availability + linked constraints).
- Timeslots from timeslots.csv are strictly respected.
- Byes handled as "BYE" for Team 2 with no venue/time, and DO NOT consume timeslots.
- Prevents double-booking of timeslots.
- Supports up to 8 linked teams per team (Linked_Team1..Linked_Team8).
- Linked teams:
    * Cannot play at the same time.
    * If at different venues on the same day, must be at least MIN_GAP_DIFFERENT_VENUE minutes apart.
    * Same venue: back-to-back is allowed; only exact same start time is banned.
    * Back-to-backs are *preferred* for linked teams when age bands are at most 1 apart
      (e.g. U10 & U12), and *discouraged* when the age gap is 2+ bands (e.g. U10 & U14).

- Cluster feel:
    * If >=2 linked teams, we prefer venues already used by those linked teams that day
      (to reduce travel) when choosing slots. Clustering is used as a tie-breaker.

- Special preferred-venue rule:
    The following grades strongly prefer venues where their Division appears
    in Preferred_Divisions, but can now fall back to non-preferred venues if needed:
        - Any U8 with a colour division (Mixed: BLUE, RED, YELLOW / Girls: PINK, PURPLE)
        - Saturday U10 Boys Div 9 Modified, Div 10 Modified
        - Sunday U10 Girls Div 1, 2, 3, 4, 5

- Lower-rings rules (hard venue filters):
    - Any U8: must be at Bentleigh Sec Court 1 or 2.
    - Any U10 Girls: must be at any of:
        Bentleigh Sec 1/2, Cheltenham Sec 1/2, GESAC 1/2/3, McKinnon Main, Moorabbin PS, RSEA Park, South Oakleigh.
    - Any U10 "Modified": must be at one of:
        Cheltenham Sec 1/2, McKinnon Main, Moorabbin PS, RSEA Park, South Oakleigh.

- Age windows (normal passes):

  Minimum start times:

    - U8   → earliest 8:00am
    - U10  → earliest 8:00am
    - U12  → earliest 8:00am
    - U14  → earliest 10:00am  (can be pushed before 10:30am if needed)
    - U16  → earliest 11:00am
    - U18  → earliest 1:30pm
    - U20  → earliest 1:30pm
    - Open/Seniors (contains "SENIOR" or "OPEN"): earliest 1:30pm

  Maximum start times (normal window):

    - U8   → latest 12:00pm
    - U10  → latest 12:00pm
    - U12  → latest 3:00pm
    - U14  → latest 5:00pm
    - U16  → latest 7:00pm
    - U18  → latest 7:00pm
    - U20  → latest 9:00pm
    - Open/Seniors → latest 9:00pm

- Seniors/Open:
    * Treated like U20 for ordering and time windows (1:30pm–9:00pm target).
    * In force-pass, if no slot exists 1:30–9:00pm, they may use any free slot
      from 1:30pm up to 9:00pm.

- Venue priority:

    * Within already-legal slots, times are always filled from **earliest to latest**.
    * Among slots at the same time:
        - Prefer **core venues** over overflow.
        - Then prefer cluster venues already used by linked teams.
        - Then prefer venues where the Division is listed as preferred.
            · For high-priority grades (Div 1 / Champ / Premier), if there are
              any preferred-venue slots available, we ONLY use those.
            · Otherwise, preferred venues act as a soft tie-breaker only.
        - Then prefer back-to-backs for linked teams when age bands are close.

    * NEW:
        - Any grade containing "MEN" in Grade/Division will try to use
          **core venues only** if any core slots are available.
        - Any U20 grade will ALSO try to use **core venues only** if any
          core slots are available.
      Overflow for these is used only once core is exhausted for that fixture.

- Fixture requests (Unavailable_Times):
    * We always try to respect both teams' requests.
    * If that is impossible, we always prefer to honour **Team 1** over Team 2.
    * In primary & fallback passes we NEVER violate Team 1's request.
    * In force-pass (last resort), if the only way to schedule a game is to
      violate Team 1's request, we will do so (still respecting age windows,
      lower-rings rules, and no double-booking).
    * For each day, we schedule all fixtures with any Unavailable_Times
      (either team) FIRST, then schedule all remaining fixtures.
      Within each group we go:
         1) Younger age groups first,
         2) Then fixtures with the tightest usable window (fewest valid slots),
         3) Then a deterministic random order within the age band
            (treating men and women the same).

- Multi-pass scheduling:
    * Primary pass: "requests first" ordering per day, then younger → older,
      with tightest window first inside each age band; honours age-minimums
      & maximums, travel gap, unavailabilities, strict venues, lower-rings.
    * Fallback pass: same as primary (still respects min & max).
    * Relaxed-linked pass (Stage 2.5): a last chance to honour Team 1's
      Unavailable_Times before force-pass gets permission to violate it.
      Ignores the 90-minute cross-venue spacing between linked teams, but
      keeps all other rules intact (age window, lower-rings, Team 1 request).
    * Force-pass: last resort – fills remaining free slots:
        - Ignores travel and linked-team spacing,
        - Still prefers to honour fixture requests / Unavailable_Times
          (Team 1 over Team 2),
        - Still respects lower-rings venue filters,
        - Tries preferred venues first for special grades, then can relax if none exist,
        - Prefers slots within age window; for juniors can relax max (and then min as last resort),
          for Seniors/Open will NOT go earlier than their minimum, but can extend to 9:00pm if needed.

- Post-scheduling junior rebalancing:
    * After scheduling a round, for each day:
        - Any U8/U10/U12 fixture that ended up after its normal max start
          will try to swap to an earlier timeslot with an older-age game
          on the same day, as long as:
              · lower-rings rules still hold,
              · Team 1 requests for both fixtures are not violated (unless
                they were already in violation),
              · and no team is double-booked.

- Removes duplicate fixtures from input and output.
- NOW SUPPORTS MULTIPLE ROUNDS:
    * If pre_fixtures.csv has a 'Round' column, each round is scheduled separately
      with a fresh copy of the day's timeslots.
    * If there is no 'Round' column, everything is treated as Round 1 (same as before).

- Outputs:
    * scheduled_fixtures.csv  (includes Round)
    * unscheduled_fixtures.csv (includes Round)
"""

import os
import re
import hashlib
from collections import defaultdict

import pandas as pd

# ---------------------------
# CONFIG
# ---------------------------

MIN_GAP_DIFFERENT_VENUE = 90

AGE_ORDER = ['U8', 'U10', 'U12', 'U14', 'U16', 'U18', 'U20']

LINKED_COLS = [
    'Linked_Team1',
    'Linked_Team2',
    'Linked_Team3',
    'Linked_Team4',
    'Linked_Team5',
    'Linked_Team6',
    'Linked_Team7',
    'Linked_Team8',
]

# U8 colour divisions that trigger the strict preferred-venue rule.
# Mixed U8s use BLUE, RED, YELLOW, GREEN. U8 Girls use PINK, PURPLE.
U8_COLOUR_DIVISIONS = ['BLUE', 'RED', 'YELLOW', 'GREEN', 'PINK', 'PURPLE']

# ===============================
# ADDED SECTION (Near CORE_VENUES)
# ===============================

OPEN_MEN_ALLOWED_VENUES = {
    'Bentleigh Secondary College - Court 1',
    'Bentleigh Secondary College - Court 2',
    'Bentleigh Secondary College - Court 3',
    'Bentleigh Secondary College - Court 4',
    'Brighton Secondary College - Court 1',
    'Brighton Secondary College - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 1',
    'Glen Eira Sports & Aquatic Centre - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 3',
    'McKinnon Secondary College - Court 1 - Main Campus',
}

# Extra venues unlocked for LOWER-division Open Men only (Div 4 and below).
# Top-tier Open Men (Div 1/2/3, Championship, Premier) stay restricted to the
# premium-court list above. Cheltenham itself lists Div 4-9 in its preferred
# divisions, so this aligns with the venue's own intended use.
OPEN_MEN_LOWER_DIV_EXTRA_VENUES = {
    'Cheltenham Secondary College - Court 1',
    'Cheltenham Secondary College - Court 2',
}

def is_open_men_grade(grade_text: str) -> bool:
    s = str(grade_text).upper()
    return 'OPEN' in s and 'MEN' in s

def is_top_tier_open_men(grade_text: str) -> bool:
    """
    True if this Open Men grade is top-tier (Div 1/2/3 or Championship/Premier).
    Top-tier games stay on the premium-court allowed list. Lower-division Open
    Men also get Cheltenham 1 & 2 added to their allowed venues.
    """
    div_u = _norm_div_name(grade_text)
    if 'CHAMP' in div_u or 'PREMIER' in div_u:
        return True
    # Whole-word match so "Div 10/11/12/13" don't accidentally match "Div 1"
    return bool(re.search(r'\bDIV [123]\b', div_u))

def get_open_men_allowed_venues(grade_text: str) -> set[str]:
    """
    Open Men venue allowlist depends on division:
      - Top-tier (Div 1-3, Champ, Premier): premium-court list only
      - Lower divisions (Div 4+): premium-court list PLUS Cheltenham 1 & 2
    """
    if is_top_tier_open_men(grade_text):
        return OPEN_MEN_ALLOWED_VENUES
    return OPEN_MEN_ALLOWED_VENUES | OPEN_MEN_LOWER_DIV_EXTRA_VENUES


# Core vs overflow venues
CORE_VENUES = {
    'Bentleigh Secondary College - Court 1',
    'Bentleigh Secondary College - Court 2',
    'Bentleigh Secondary College - Court 3',
    'Bentleigh Secondary College - Court 4',
    'Brighton Secondary College - Court 1',
    'Brighton Secondary College - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 1',
    'Glen Eira Sports & Aquatic Centre - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 3',
    'Glen Huntly Primary School - Court 1',
    'McKinnon Secondary College - Court 1 - Main Campus',
    'McKinnon Secondary College - Court 2 - Cnr of Walnut & Bewdley st',
    'Moorabbin Primary School - Court 1',
    'RSEA Park - St Kilda Football Club - Court 1',
}

def is_core_venue(venue: str) -> bool:
    return str(venue).strip() in CORE_VENUES

# Heavy-linked-cluster venues — fixtures involving teams with many linked
# teams (e.g. St Kilda Warriors with 7 sibling teams) prefer these courts.
# Concentrating these clubs at a small number of courts makes their
# spacing/clustering constraints far easier to satisfy without back-to-back
# limits getting in the way.
#
# Two tiers:
#   PRIMARY  → Bent 1-4 + McKinnon Court 1 (Main Campus). All core, all
#              suitable for the usual heavy-linked clubs (e.g. St Kilda
#              Warriors). Picked first.
#   FALLBACK → Cheltenham 1 & 2. Overflow venues but acceptable for
#              heavy-linked clubs when primary is full. Picked before
#              other overflow but after primary.
LINKED_CLUSTER_VENUES_PRIMARY = {
    'Bentleigh Secondary College - Court 1',
    'Bentleigh Secondary College - Court 2',
    'Bentleigh Secondary College - Court 3',
    'Bentleigh Secondary College - Court 4',
    'McKinnon Secondary College - Court 1 - Main Campus',
}

LINKED_CLUSTER_VENUES_FALLBACK = {
    'Cheltenham Secondary College - Court 1',
    'Cheltenham Secondary College - Court 2',
}

# Combined set used in the rest of the codebase where any cluster venue is
# acceptable (e.g. for dispatch/filtering).
LINKED_CLUSTER_VENUES = LINKED_CLUSTER_VENUES_PRIMARY | LINKED_CLUSTER_VENUES_FALLBACK

# A fixture qualifies for the heavy-linked-cluster bias if EITHER team has
# this many linked teams. Set to 4 to capture St Kilda Warriors (7 each)
# and a handful of other heavily-linked clubs without sweeping in casual
# 1-2 link fixtures (which are common and would crowd Bent 3/4).
HEAVY_LINKED_THRESHOLD = 3

# Cache for link counts to avoid recomputing per-slot
_LINK_COUNT_CACHE: dict[str, int] = {}

def _count_links(team_name) -> int:
    """Return the number of non-empty linked teams for this team."""
    if not team_name:
        return 0
    if pd.isna(team_name):
        return 0
    key = str(team_name).strip()
    if key in _LINK_COUNT_CACHE:
        return _LINK_COUNT_CACHE[key]
    n = len(get_linked_teams(team_name))
    _LINK_COUNT_CACHE[key] = n
    return n

# Lower-ring venue sets
# Base U8 venue list — applies to U8 Girls (PINK, PURPLE) and as the default.
# U8 Mixed/Boys colour divisions (BLUE, RED, YELLOW, GREEN) get an EXTRA venue
# (McKinnon Main Campus). See LOWER_U8_BOYS_VENUES below and the routing in
# get_lower_ring_allowed_venues().
LOWER_U8_VENUES = {
    'Bentleigh Secondary College - Court 1',
    'Bentleigh Secondary College - Court 2',
    'Monash Sport - Clayton - Stadium Court 1',
    'Monash Sport - Clayton - Stadium Court 2',
}

# U8 Mixed/Boys colour divisions (BLUE, RED, YELLOW, GREEN). Same as the base
# list plus McKinnon Court 1 - Main Campus. NOT used for PINK/PURPLE (Girls).
LOWER_U8_BOYS_VENUES = LOWER_U8_VENUES | {
    'McKinnon Secondary College - Court 1 - Main Campus',
}

# Boys colours (used to identify U8 Mixed/Boys games, distinct from PINK/PURPLE)
U8_BOYS_COLOUR_DIVISIONS = ['BLUE', 'RED', 'YELLOW', 'GREEN']

LOWER_U10_GIRLS_VENUES = {
    'Bentleigh Secondary College - Court 1',
    'Bentleigh Secondary College - Court 2',
    'Cheltenham Secondary College - Court 1',
    'Cheltenham Secondary College - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 1',
    'Glen Eira Sports & Aquatic Centre - Court 2',
    'Glen Eira Sports & Aquatic Centre - Court 3',
    'McKinnon Secondary College - Court 1 - Main Campus',
    'Monash Sport - Clayton - Stadium Court 1',
    'Monash Sport - Clayton - Stadium Court 2',
    'Moorabbin Primary School - Court 1',
    'RSEA Park - St Kilda Football Club - Court 1',
    'South Oakleigh College - Court 1',
}

LOWER_U10_MOD_VENUES = {
    'Cheltenham Secondary College - Court 1',
    'Cheltenham Secondary College - Court 2',
    'McKinnon Secondary College - Court 1 - Main Campus',
    'Monash Sport - Clayton - Stadium Court 1',
    'Monash Sport - Clayton - Stadium Court 2',
    'Moorabbin Primary School - Court 1',
    'RSEA Park - St Kilda Football Club - Court 1',
    'South Oakleigh College - Court 1',
}

# ---------------------------
# Paths & CSV loading
# ---------------------------

BASE = os.path.dirname(os.path.abspath(__file__))

def load_csv(name: str) -> pd.DataFrame:
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required CSV not found: {path}")
    return pd.read_csv(path)

teams_df = load_csv('teams.csv')
pre_fixtures_df = load_csv('pre_fixtures.csv')
timeslots_df = load_csv('timeslots.csv')
division_venues_df = load_csv('division_venues.csv')

# ---------------------------------------------------------------
# pre_fixtures column normalization
# ---------------------------------------------------------------
# pre_fixtures.csv may arrive in either of two layouts:
#   (a) Legacy internal layout: 'Team 1', 'Team 2', 'Grade', 'Round'
#   (b) PlayHQ-style upload template (lowercase headers):
#       organisation, competition, season, grade, round date, round,
#       home team, away team, venue, playing surface, game date,
#       game time, game alias
# We normalize layout (b) into the internal column names the rest of the
# scheduler expects, and remember which input columns were present so the
# export can reproduce the same template (carrying through organisation,
# competition, season, etc. for each row).
PREFIX_TEMPLATE_COLUMNS = [
    'organisation', 'competition', 'season', 'grade', 'round date', 'round',
    'home team', 'away team', 'venue', 'playing surface', 'game date',
    'game time', 'game alias',
]

# Will hold the per-(round, home, away, grade) passthrough metadata from the
# upload template (organisation/competition/season/round date/game date/
# game alias) so we can write them back out unchanged. Empty when the input
# was the legacy layout.
PREFIX_TEMPLATE_PASSTHROUGH: dict[tuple, dict] = {}
INPUT_WAS_TEMPLATE = False

def _normalize_pre_fixtures(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detect the upload-template layout (lowercase headers with 'home team' /
    'away team') and rename to the internal layout. Records passthrough
    metadata for the export step. Returns a df with at least
    'Team 1', 'Team 2', 'Grade', 'Round'.
    """
    global INPUT_WAS_TEMPLATE
    cols_lower = {c.lower().strip(): c for c in df.columns}

    # Template layout is identified by 'home team' + 'away team' columns.
    if 'home team' in cols_lower and 'away team' in cols_lower:
        INPUT_WAS_TEMPLATE = True
        rename = {
            cols_lower['home team']: 'Team 1',
            cols_lower['away team']: 'Team 2',
        }
        if 'grade' in cols_lower:
            rename[cols_lower['grade']] = 'Grade'
        if 'round' in cols_lower:
            rename[cols_lower['round']] = 'Round'
        df = df.rename(columns=rename)

        # Capture passthrough template fields per fixture so we can write them
        # back out. Key is (round, home, away, grade) using stripped strings.
        passthrough_fields = [
            'organisation', 'competition', 'season', 'round date',
            'game date', 'game alias',
        ]
        for _, row in df.iterrows():
            key = (
                str(row.get('Round', '')).strip(),
                str(row.get('Team 1', '')).strip(),
                str(row.get('Team 2', '')).strip(),
                str(row.get('Grade', '')).strip(),
            )
            meta = {}
            for f in passthrough_fields:
                if f in cols_lower:
                    val = row.get(cols_lower[f], '')
                    meta[f] = '' if pd.isna(val) else val
            PREFIX_TEMPLATE_PASSTHROUGH[key] = meta
        return df

    # Legacy layout: ensure the expected columns exist as-is.
    return df

pre_fixtures_df = _normalize_pre_fixtures(pre_fixtures_df)

def _merge_venue_surface(df: pd.DataFrame) -> pd.DataFrame:
    """
    timeslots.csv and division_venues.csv now come with separate 'Venue' and
    'Playing Surface' columns. Internally the scheduler still uses a single
    composite identifier (e.g. 'Bentleigh Secondary College - Court 1') so that
    all hardcoded venue sets (CORE_VENUES, LOWER_U8_VENUES, etc.) keep working.

    This helper combines the two columns back into 'Venue' in-place. Safe to
    call on CSVs that already use the old single-column layout — in that case
    it's a no-op.

    As it merges, it also populates VENUE_SURFACE_LOOKUP so we can split the
    composite identifier back into two columns when writing the scheduled
    fixtures CSV.
    """
    if 'Playing Surface' in df.columns:
        venue_clean = df['Venue'].astype(str).str.strip()
        surface_clean = df['Playing Surface'].astype(str).str.strip()
        composites = []
        for v, s in zip(venue_clean, surface_clean):
            if s == '' or s.lower() == 'nan':
                composite = v
                VENUE_SURFACE_LOOKUP[composite] = (v, '')
            else:
                composite = f"{v} - {s}"
                VENUE_SURFACE_LOOKUP[composite] = (v, s)
            composites.append(composite)
        df['Venue'] = composites
    return df

# Maps composite "Venue - Playing Surface" strings back to ('Venue', 'Playing Surface')
# tuples. Populated by _merge_venue_surface and used when writing output CSVs.
VENUE_SURFACE_LOOKUP: dict[str, tuple[str, str]] = {}

timeslots_df = _merge_venue_surface(timeslots_df)
division_venues_df = _merge_venue_surface(division_venues_df)

# Add Round if missing so single-round behaviour is unchanged
if 'Round' not in pre_fixtures_df.columns:
    pre_fixtures_df['Round'] = 1

# Remove duplicate fixtures, now including Round so different rounds stay separate
pre_fixtures_df = pre_fixtures_df.drop_duplicates(
    subset=['Round', 'Team 1', 'Team 2', 'Grade']
).reset_index(drop=True)

# ---------------------------
# Grade parsing
# ---------------------------

def parse_grade(grade_str):
    parts = str(grade_str).split()
    if len(parts) < 4:
        day = parts[0] if parts else ''
        age = parts[1] if len(parts) > 1 else ''
        gender = parts[2] if len(parts) > 2 else ''
        division = ' '.join(parts[3:]) if len(parts) > 3 else ''
        return day, age, gender, division

    day, age, gender = parts[:3]
    division = ' '.join(parts[3:])
    return day, age, gender, division

teams_df[['Day', 'Age', 'Gender', 'Division']] = teams_df['Grade'].apply(
    lambda x: pd.Series(parse_grade(x))
)
pre_fixtures_df[['Day', 'Age', 'Gender', 'Division']] = pre_fixtures_df['Grade'].apply(
    lambda x: pd.Series(parse_grade(x))
)

# ---------------------------
# Robust age ordering
# ---------------------------

def extract_age_token_from_grade(grade_str: str) -> str:
    s = str(grade_str).upper()
    m = re.search(r'U\d+', s)
    if not m:
        return ''
    return m.group(0)

def is_senior_grade(grade_str: str) -> bool:
    s = str(grade_str).upper()
    return 'SENIOR' in s or 'OPEN' in s

def age_sort_key_from_grade(grade_str: str) -> float:
    """
    Determines scheduling order priority by age.

    - Open Men are scheduled AFTER U20 Boys (adult men can play the latest
      timeslots, so let younger boys grab the earlier ones first).
    - Other Seniors/Open (e.g. Open Women) sit between U16 and U18 to protect
      the adult window (1:30pm–9:00pm).
    """

    # Open Men → after U20
    if is_open_men_grade(grade_str):
        try:
            return AGE_ORDER.index('U20') + 0.5
        except ValueError:
            return 100

    # Other Seniors/Open (e.g. Open Women, Senior Men if present) → between U16 and U18
    if is_senior_grade(grade_str):
        try:
            return AGE_ORDER.index('U16') + 0.5
        except ValueError:
            return 100

    token = extract_age_token_from_grade(grade_str)

    try:
        return AGE_ORDER.index(token)
    except ValueError:
        return 100

pre_fixtures_df['Age_Order'] = pre_fixtures_df['Grade'].apply(age_sort_key_from_grade)

# ---------------------------
# Deterministic "random" order within age band
# ---------------------------

def _deterministic_random_value(round_val, day, grade, team1, team2) -> int:
    """
    Produce a stable pseudo-random integer for a fixture based on its identity.
    This breaks any incidental ordering by division/gender while staying
    reproducible run-to-run.
    """
    seed = f"{round_val}|{day}|{grade}|{team1}|{team2}"
    h = hashlib.md5(seed.encode('utf-8')).hexdigest()
    # Use first 8 hex digits as an int
    return int(h[:8], 16)

pre_fixtures_df['Random_Order'] = pre_fixtures_df.apply(
    lambda row: _deterministic_random_value(
        row.get('Round', 1),
        row.get('Day', ''),
        row.get('Grade', ''),
        row.get('Team 1', ''),
        row.get('Team 2', ''),
    ),
    axis=1,
)

# ---------------------------
# Time helpers
# ---------------------------

def time_to_minutes(t):
    if t is None or str(t).strip() == '':
        return -1
    s = str(t).strip().lower()
    m = re.match(r'(\d+)(?::(\d+))?\s*(am|pm)?', s)
    if not m:
        return 0
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    period = m.group(3)

    if period == 'pm' and hour != 12:
        hour += 12
    if period == 'am' and hour == 12:
        hour = 0
    return hour * 60 + minute

def format_game_time(t) -> str:
    """
    Format a time string for the upload-template output as zero-padded
    12-hour time with uppercase meridiem and no space, e.g.:
        '8:15 am'  -> '08:15AM'
        '12 pm'    -> '12:00PM'
        '8:30 pm'  -> '08:30PM'
        '11:25 am' -> '11:25AM'
    Blank/BYE times pass through as an empty string.
    """
    if t is None:
        return ''
    s = str(t).strip()
    if s == '' or s.lower() == 'nan':
        return ''
    mins = time_to_minutes(s)
    if mins < 0:
        return ''
    hour24 = mins // 60
    minute = mins % 60
    period = 'AM' if hour24 < 12 else 'PM'
    hour12 = hour24 % 12
    if hour12 == 0:
        hour12 = 12
    return f"{hour12:02d}:{minute:02d}{period}"

def min_start_minutes_for_age(grade_text: str, day: str | None = None) -> int:
    """
    Minimum allowed start times:

      - U8   → 8:00am
      - U10  → 8:00am
      - U12  → 8:00am
      - U14  → 10:00am  (can be pushed before 10:30am if needed)
      - U16  → 11:00am
      - U18  → 1:30pm
      - U20  → 1:30pm
      - Open/Seniors (any 'SENIOR' or 'OPEN') → 1:30pm
    """
    age_token = extract_age_token_from_grade(grade_text)
    s = grade_text.upper()

    # Seniors / Opens: fixed 1:30pm
    if 'SENIOR' in s or 'OPEN' in s:
        return 13 * 60 + 30  # 1:30pm

    if age_token == 'U8':
        return 8 * 60   # 8:00am
    if age_token == 'U10':
        return 8 * 60   # 8:00am
    if age_token == 'U12':
        return 8 * 60   # 8:00am
    if age_token == 'U14':
        return 10 * 60  # 10:00am
    if age_token == 'U16':
        # Saturday U16 starts at 1pm. Rounds 2-5 reference shows U16 peak at
        # 3pm (20.5/round); the reference only has 1.2/round at 12pm. With my
        # previous 12pm floor, games piled up at 12pm (12.5/round) — too early.
        # 1pm floor matches the reference earliest more closely and pushes U16
        # into the proper afternoon block. Sunday U16 keeps 11am.
        if day == 'Saturday':
            return 13 * 60  # 1:00pm
        return 11 * 60  # 11:00am
    if age_token == 'U18':
        # Saturday U18 starts at 3pm. Reference data shows U18 peaks at 4pm
        # (11/round) with only 0.5/round before 3pm. My previous 2pm floor put
        # 6/round at 2pm — much too early. 3pm matches the reference floor.
        if day == 'Saturday':
            return 15 * 60  # 3:00pm
        return 13 * 60 + 30  # 1:30pm
    if age_token == 'U20':
        # Same logic as U18 — Saturday U20 at 3pm.
        if day == 'Saturday':
            return 15 * 60  # 3:00pm
        return 13 * 60 + 30  # 1:30pm

    # Unknown → no minimum
    return 0

def max_start_minutes_for_age(grade_text: str) -> int:
    """
    Maximum start times (normal window):

      - U8   → latest 12:00pm
      - U10  → latest 12:00pm
      - U12  → latest 3:00pm
      - U14  → latest 5:00pm
      - U16  → latest 7:00pm
      - U18  → latest 7:00pm
      - U20  → latest 9:00pm
      - Open/Seniors → latest 9:00pm
    """
    age_token = extract_age_token_from_grade(grade_text)
    s = grade_text.upper()

    # Seniors / Open – allow up to 9:00pm
    if 'SENIOR' in s or 'OPEN' in s:
        return 21 * 60   # 9:00pm

    if age_token == 'U8':
        return 12 * 60   # 12:00pm
    if age_token == 'U10':
        return 12 * 60   # 12:00pm
    if age_token == 'U12':
        return 15 * 60   # 3:00pm
    if age_token == 'U14':
        return 17 * 60   # 5:00pm
    if age_token == 'U16':
        return 19 * 60   # 7:00pm
    if age_token == 'U18':
        return 19 * 60   # 7:00pm
    if age_token == 'U20':
        return 21 * 60   # 9:00pm

    # Default for unknown grades
    return 21 * 60       # 9:00pm

def force_pass_max_start_minutes_for_age(grade_text: str) -> int:
    """
    Absolute latest start time force-pass is allowed to use for a given age.
    Gives force-pass a little grace beyond the normal max (so minor capacity
    bumps still get scheduled) but stops it producing nonsensical placements
    like a U8 game at 5:15pm.

    If no slot can be found at or before this cap, the fixture is left
    unscheduled and surfaces in unscheduled_fixtures.csv with a clear reason,
    so the user can decide how to resolve (add a venue/timeslot, move to the
    other day, cut a game, etc.).

    Caps:
      - U8   → 2:00pm
      - U10  → 2:00pm
      - U12  → 5:00pm  (bumped from 4pm to absorb linked-cluster overflow)
      - U14  → 6:00pm
      - U16  → 8:00pm
      - U18/U20/Seniors/Open → unchanged (9:00pm)
    """
    age_token = extract_age_token_from_grade(grade_text)
    s = grade_text.upper()

    if 'SENIOR' in s or 'OPEN' in s:
        return 21 * 60   # 9:00pm (unchanged)

    if age_token == 'U8':
        return 14 * 60        # 2:00pm
    if age_token == 'U10':
        return 14 * 60        # 2:00pm
    if age_token == 'U12':
        return 17 * 60        # 5:00pm
    if age_token == 'U14':
        return 18 * 60        # 6:00pm
    if age_token == 'U16':
        return 20 * 60        # 8:00pm
    if age_token in ('U18', 'U20'):
        return 21 * 60        # 9:00pm (unchanged)

    return 21 * 60            # Unknown → 9:00pm

# ---------------------------
# Unavailable times (fixture requests)
# ---------------------------

def is_time_blocked(time_str, unavailable_text):
    """
    Returns True if `time_str` falls inside any of the blocked intervals in
    `unavailable_text`.

    Syntax supported in teams.csv:

      - Ranges: "10am-1pm"   → block any time t with 10:00 <= t <= 13:00
      - Single '>' rule: ">1:30pm"  → block any t > 13:30
      - Single '<' rule: "<1:30pm"  → block any t < 13:30
      - Exact: "7pm"        → block t == 19:00
      - Multiple rules separated by ";" or ",":
            "<10am; 2pm-4pm; >9:30pm"

    Notes:
      - "<1:30pm" means "no games before 1:30pm" (1:30 itself is allowed).
      - ">7pm" means "no games after 7pm" (7:00 itself is allowed).
    """
    if pd.isna(unavailable_text):
        return False

    slot_min = time_to_minutes(time_str)
    if slot_min < 0:
        return False

    text = str(unavailable_text).strip()
    if not text:
        return False

    # Allow separators ";", "," for multiple rules
    raw_rules = re.split(r'[;,]', text)

    for raw in raw_rules:
        rule = raw.strip()
        if not rule:
            continue

        # Range like "10am-1pm"
        if "-" in rule and not rule.startswith('<') and not rule.startswith('>'):
            start_s, end_s = [p.strip() for p in rule.split('-', 1)]
            start_min = time_to_minutes(start_s)
            end_min   = time_to_minutes(end_s)
            if start_min < 0 or end_min < 0:
                continue
            if start_min <= slot_min <= end_min:
                return True
            continue

        # Single < or > rule
        op = None
        body = rule
        if rule[0] in ('<', '>'):
            op = rule[0]
            body = rule[1:].strip()

        bound_min = time_to_minutes(body)
        if bound_min < 0:
            continue

        if op == '<':
            # Block times strictly BEFORE the boundary
            if slot_min < bound_min:
                return True
        elif op == '>':
            # Block times strictly AFTER the boundary
            if slot_min > bound_min:
                return True
        else:
            # Exact match
            if slot_min == bound_min:
                return True

    return False

# ---------------------------
# Build master slots (by day)
# ---------------------------

def build_master_slots():
    """
    Build a fresh copy of all timeslots for one round.
    Called separately for each Round so each round reuses the same day/venue slots.

    Rows in timeslots.csv with an empty Time_Slots cell are skipped entirely,
    so any venue/playing-surface combo without listed times simply goes unused.
    """
    master_slots = defaultdict(list)
    for _, row in timeslots_df.iterrows():
        day = row['Day']
        venue = row['Venue']
        raw_slots = row.get('Time_Slots', '')

        # Skip unused venue/surface combos (blank Time_Slots cell).
        if pd.isna(raw_slots) or not str(raw_slots).strip():
            continue

        slot_str = str(raw_slots)
        slots = [t.strip() for t in slot_str.split(',') if t.strip()]
        for t in slots:
            master_slots[day].append({'time': t, 'venue': venue, 'occupied': False})

    for d in master_slots:
        master_slots[d].sort(key=lambda s: time_to_minutes(s['time']))

    return master_slots

# ---------------------------
# Venue preferences & quality
# ---------------------------

def _norm_div_name(name: str) -> str:
    s = str(name).strip().strip('"').strip("'")
    s = re.sub(r'\s+', ' ', s)
    return s.upper()

venue_pref_map = {}
for _, row in division_venues_df.iterrows():
    venue = row['Venue']
    prefs = []
    raw = row.get('Preferred_Divisions', '')
    if not pd.isna(raw):
        prefs = [p.strip() for p in str(raw).split(',') if p.strip()]
    venue_pref_map[venue] = prefs

def is_pref_venue_for_division(division: str, venue: str) -> bool:
    target = _norm_div_name(division)
    prefs = venue_pref_map.get(venue, [])

    for p in prefs:
        norm_p = _norm_div_name(p)
        if norm_p == target:
            return True
        # U8 colour divisions: match if the preferred entry colour appears
        # anywhere in the target division string (e.g. "U8 GIRLS PINK" matches
        # a venue listing "PINK" as a preferred division).
        for colour in U8_COLOUR_DIVISIONS:
            if norm_p == colour and re.search(rf'\b{colour}\b', target):
                return True

    return False

def venue_quality_score(venue):
    prefs = venue_pref_map.get(venue, [])
    best = 999
    for p in prefs:
        m = re.search(r'\d+', str(p))
        if m:
            best = min(best, int(m.group(0)))
    return best if best != 999 else 500

# ---------------------------
# Preferred-venue strength helpers
# ---------------------------

def requires_strict_preferred_venue(day, age, gender, division, grade_text):
    """
    Strict preferred-venue rule for specific lower grades/divisions.
    """
    day_u = str(day).strip().upper()
    age_u = str(age).strip().upper()
    gender_u = str(gender).strip().upper()
    div_u = _norm_div_name(division)
    grade_u = _norm_div_name(grade_text)

    age_token = extract_age_token_from_grade(grade_text)

    # U8 colour divisions (Mixed: BLUE, RED, YELLOW / Girls: PINK, PURPLE)
    if age_token == 'U8' or age_u == 'U8':
        for colour in U8_COLOUR_DIVISIONS:
            # Use word boundaries so 'RED' doesn't accidentally match other tokens.
            if re.search(rf'\b{colour}\b', div_u) or re.search(rf'\b{colour}\b', grade_u):
                return True

    # Saturday U10 Boys Div 9/10 Modified
    if (age_token == 'U10' or age_u == 'U10') and day_u.startswith('SAT'):
        if 'BOY' in gender_u or gender_u.startswith('B'):
            if ('DIV 9 MODIFIED' in div_u or 'DIV 10 MODIFIED' in div_u or
                'DIV 9 MODIFIED' in grade_u or 'DIV 10 MODIFIED' in grade_u):
                return True

    # Sunday U10 Girls Div 1–5
    if (age_token == 'U10' or age_u == 'U10') and day_u.startswith('SUN'):
        if 'GIRL' in gender_u or gender_u.startswith('G'):
            for n in ['1', '2', '3', '4', '5']:
                if f'DIV {n}' in div_u or f'DIV {n}' in grade_u:
                    return True

    return False

def is_high_priority_division(division: str, grade_text: str) -> bool:
    """
    Treat Div 1 / Championship / Premier style grades as 'high priority'
    for preferred-venue placement.
    """
    div_u = _norm_div_name(division)
    grade_u = _norm_div_name(grade_text)
    tokens = ['DIV 1', 'CHAMP', 'CHAMPIONSHIP', 'PREMIER']
    return any(tok in div_u or tok in grade_u for tok in tokens)

def requires_men_core_bias(grade_text: str, division: str) -> bool:
    """
    Return True if this looks like a Men's grade (Open Men, Senior Men, etc.).
    Any grade/division containing 'MEN' will try to use core venues first,
    only spilling to overflow if no core slots are available.
    """
    g = _norm_div_name(grade_text)
    d = _norm_div_name(division)
    return 'MEN' in g or 'MEN' in d

def requires_u20_core_bias(grade_text: str) -> bool:
    """
    Return True if this is a U20 grade. Any U20 will prefer core venues first,
    only spilling to overflow if no core slots are available.
    """
    token = extract_age_token_from_grade(grade_text)
    if token == 'U20':
        return True
    g = _norm_div_name(grade_text)
    return 'U20' in g

# ---------------------------
# Lower-rings venue overrides
# ---------------------------

def get_lower_ring_allowed_venues(day, age, gender, division, grade_text):
    """
    Priority of rules:
      1) U10 Modified (any gender) → LOWER_U10_MOD_VENUES
      2) U8 boys colour divisions (BLUE/RED/YELLOW/GREEN) → LOWER_U8_BOYS_VENUES
         (includes McKinnon Court 1 in addition to the base U8 list)
      3) U8 girls + any other U8 → LOWER_U8_VENUES
      4) U10 Girls → LOWER_U10_GIRLS_VENUES
    """
    age_token = extract_age_token_from_grade(grade_text)
    age_u = str(age).strip().upper()
    gender_u = str(gender).strip().upper()
    div_u = _norm_div_name(division)
    grade_u = _norm_div_name(grade_text)

    is_u10 = (age_token == 'U10' or age_u == 'U10')
    is_u8 = (age_token == 'U8' or age_u == 'U8')

    if is_u10 and ('MODIFIED' in div_u or 'MODIFIED' in grade_u):
        return LOWER_U10_MOD_VENUES

    if is_u8:
        # Boys colour divisions (BLUE, RED, YELLOW, GREEN) get McKinnon as well
        for colour in U8_BOYS_COLOUR_DIVISIONS:
            if re.search(rf'\b{colour}\b', div_u) or re.search(rf'\b{colour}\b', grade_u):
                return LOWER_U8_BOYS_VENUES
        # U8 Girls (PINK, PURPLE) and any unmatched U8 → base list (no McKinnon)
        return LOWER_U8_VENUES

    if is_u10 and (
        'GIRL' in gender_u or 'GIRL' in div_u or 'GIRL' in grade_u
    ):
        return LOWER_U10_GIRLS_VENUES

    return None

# ---------------------------
# Linked & self constraints
# ---------------------------

# Whitespace-tolerant team lookup. The CSV files preserve their original
# whitespace (no auto-stripping at load), but a trailing space in teams.csv
# would silently cause exact-string lookups like get_unavailable(team_name)
# to return None — meaning the team's Unavailable_Times rule would be ignored.
# We build a stripped-name index here so 'Footlocker Employees' (from
# pre_fixtures) successfully matches 'Footlocker Employees ' (from teams.csv).
def _build_team_lookup() -> dict[str, int]:
    """Map stripped team name → row index in teams_df. Last-write-wins on
    duplicate stripped names (rare; would require two teams with the same
    name modulo whitespace, which is itself a data error)."""
    out: dict[str, int] = {}
    for idx, name in teams_df['Team'].items():
        if pd.isna(name):
            continue
        out[str(name).strip()] = idx
    return out

_TEAM_LOOKUP = _build_team_lookup()

def _find_team_row(team_name):
    """Return the teams_df row for a name, tolerating leading/trailing
    whitespace on either side. Returns None if not found."""
    if team_name is None:
        return None
    if pd.isna(team_name):
        return None
    key = str(team_name).strip()
    idx = _TEAM_LOOKUP.get(key)
    if idx is None:
        return None
    return teams_df.loc[idx]

def get_unavailable(team_name):
    row = _find_team_row(team_name)
    if row is None:
        return None
    return row.get('Unavailable_Times', None)

def get_linked_teams(team_name):
    row = _find_team_row(team_name)
    if row is None:
        return []
    linked = []
    for col in LINKED_COLS:
        val = row.get(col, '')
        if pd.notna(val) and str(val).strip():
            linked.append(str(val).strip())
    return linked

def get_team_grade(team_name):
    row = _find_team_row(team_name)
    if row is None:
        return ''
    return row.get('Grade', '')

def violates_linked_constraints(day, time_str, venue, linked_list, assigned_times):
    slot_min = time_to_minutes(time_str)

    for lt in linked_list:
        if lt not in assigned_times:
            continue

        for lt_day, lt_time, lt_venue in assigned_times[lt]:
            if lt_day != day or lt_time == 'BYE':
                continue

            lt_min = time_to_minutes(lt_time)
            if lt_min == slot_min:
                # Same start-time forbidden
                return True

            if lt_venue != venue:
                # Different venues must be spaced out by MIN_GAP_DIFFERENT_VENUE
                if abs(slot_min - lt_min) < MIN_GAP_DIFFERENT_VENUE:
                    return True

    return False

def violates_self_constraints(team, day, time_str, assigned_times):
    if team is None or team not in assigned_times:
        return False

    slot_min = time_to_minutes(time_str)
    for d, t, v in assigned_times[team]:
        if d != day or t == 'BYE':
            continue
        if time_to_minutes(t) == slot_min:
            return True

    return False

# ---------------------------
# Slot choice helpers
# ---------------------------

def _slot_priority_base(slot, division, linked_list, assigned_times, day, grade_text=None,
                        team1=None, team2=None):
    """
    Priority key for choosing among legal slots (lower = better).

    Order:
      1. heavy_linked_score — clubs with ≥4 linked teams (e.g. St Kilda
         Warriors) prefer LINKED_CLUSTER_VENUES (Bent 1-4) → other core →
         overflow.
      2. cluster_score — if any linked sibling is already placed today,
         prefer the same venue. **This ranks AHEAD of raw time.** Without
         this, the scheduler greedily fills earliest slots wherever they
         happen to be, and parents end up at multiple venues per day.
      3. time_min — within equal cluster status, earliest first.
      4. back_to_back_score — within the cluster venue, tightly-spaced
         (≤ 90min) sibling slots get a bonus.
      5. core_score — core venues before overflow.
      6. pref_score — division-preferred venues last.

    Back-to-back bonuses (tier 4):
      -2 = sibling at same venue, gap 30-90 min, age gap ≤ 1 (ideal)
      -1 = sibling at same venue, gap up to 90 min, any age
       0 = no sibling context
      +1 = sibling at same venue but age gap ≥ 2 and gap ≤ 60 (avoid)
    """
    venue = slot['venue']
    time_min = time_to_minutes(slot['time'])

    # Heavy-linked cluster bias
    # Teams with many linked siblings (e.g. St Kilda Warriors with 7 linked
    # teams) are far easier to pack when their fixtures concentrate at a
    # small number of courts. We tier the bias so:
    #   0 = at LINKED_CLUSTER_VENUES_PRIMARY (Bent 1-4, McKinnon Main) — ideal
    #   1 = at LINKED_CLUSTER_VENUES_FALLBACK (Cheltenham 1/2) — acceptable
    #   2 = at any other CORE venue — spillover
    #   3 = at an OVERFLOW venue (other than Cheltenham) — last resort
    heavy_linked_score = 0
    is_heavy = False
    if team1 or team2:
        max_links = max(
            _count_links(team1) if team1 else 0,
            _count_links(team2) if team2 else 0,
        )
        if max_links >= HEAVY_LINKED_THRESHOLD:
            is_heavy = True
            if venue in LINKED_CLUSTER_VENUES_PRIMARY:
                heavy_linked_score = 0
            elif venue in LINKED_CLUSTER_VENUES_FALLBACK:
                heavy_linked_score = 1
            elif is_core_venue(venue):
                heavy_linked_score = 2
            else:
                heavy_linked_score = 3

    # Home-court bias — the single strongest clustering signal.
    # Once a heavy-linked cluster places its first game of the day, that court
    # becomes the cluster's "home court". All later games for the cluster on
    # the same day strongly prefer that exact court, funnelling a whole club's
    # teams onto one court in a continuous block (matching real-world fixtures
    # where one club shares a court all day). 0 = this IS the home court,
    # 1 = a different court (or no home court assigned yet).
    home_court_score = 1
    if is_heavy:
        cid = _fixture_cluster_id(team1, team2)
        home = CLUSTER_HOME_COURT.get((cid, day))
        if home is not None and venue == home:
            home_court_score = 0
        elif home is None:
            # No home court yet — don't penalise any venue, let the normal
            # tiers decide; the chosen venue becomes the home court on placement.
            home_court_score = 0

    # Gather info about already-placed linked teams on this day
    linked_same_day = []  # list of (lt_name, lt_time_min, lt_venue, age_diff)
    if linked_list and grade_text:
        current_age_idx = age_sort_key_from_grade(grade_text)
        for lt in linked_list:
            for lt_day, lt_time, lt_venue in assigned_times.get(lt, []):
                if lt_day != day or lt_time in (None, '', 'BYE') or not lt_venue:
                    continue
                lt_grade = get_team_grade(lt)
                lt_age_idx = age_sort_key_from_grade(lt_grade) if lt_grade else current_age_idx
                age_diff = abs(current_age_idx - lt_age_idx)
                linked_same_day.append((lt, time_to_minutes(lt_time), lt_venue, age_diff))

    # CLUSTER VENUE — if any linked sibling is already placed today and we
    # can land at the same venue, do it. This is the strongest cohesion
    # signal: it ranks AHEAD of raw time so a 12pm slot at the sibling's
    # venue beats a 9am slot somewhere else. Without this, the scheduler
    # greedily fills earliest slots at any venue and parents end up
    # criss-crossing the suburb between sibling games.
    cluster_venues = {lt_venue for _, _, lt_venue, _ in linked_same_day}
    cluster_score = 0  # default — no linked sibling placed yet, no preference
    if cluster_venues:
        cluster_score = 0 if venue in cluster_venues else 1

    # 4) Back-to-back tightness (within cluster venue, prefer slots near siblings)
    back_to_back_score = 0
    if linked_same_day:
        best_b2b = 0
        for _, lt_min, lt_venue, age_diff in linked_same_day:
            if lt_venue != venue:
                continue
            gap = abs(lt_min - time_min)
            if gap == 0:
                continue
            if gap <= 90:
                if age_diff <= 1:
                    best_b2b = min(best_b2b, -2)
                elif age_diff >= 2 and gap <= 60:
                    best_b2b = max(best_b2b, 1)
                else:
                    best_b2b = min(best_b2b, -1)
        back_to_back_score = best_b2b

    # 5) Core vs overflow
    core_score = 0 if is_core_venue(venue) else 1

    # 6) Preferred-division venues
    pref_match = is_pref_venue_for_division(division, venue)
    pref_score = 0 if pref_match else 1

    # Priority ordering:
    #   1. home_court_score (cluster's assigned court for the day — strongest)
    #   2. heavy_linked_score (Bent 1-4 + McKinnon first for heavy-linked clubs)
    #   3. cluster_score (same venue as already-placed sibling — beats time)
    #   4. back_to_back_score (within cluster venue, prefer tight ≤90min gaps)
    #   5. time_min (earliest first, when above are tied)
    #   6. core_score (core before overflow)
    #   7. pref_score (preferred division venue)
    #
    # home_court_score is first so that once a club's home court is set for the
    # day, every later sibling game is funnelled to that exact court before any
    # other consideration — producing the one-court-per-club blocks seen in the
    # real fixture data. b2b is placed before time so siblings pack tightly.
    return (home_court_score, heavy_linked_score, cluster_score,
            back_to_back_score, time_min, core_score, pref_score)

def _apply_soft_preferred_bias(filtered_slots, division, grade_text):
    """
    Preferred-venue behaviour:

      - For 'high-priority' grades (Div 1 / Champ / Premier):
          * If any slots exist at preferred venues for this division,
            we use ONLY those slots.
          * If none exist, we fall back to all filtered_slots.

      - For all other grades:
          * We keep all filtered_slots; preferred venues are handled as a
            tie-breaker via _slot_priority_base.
    """
    if not filtered_slots:
        return filtered_slots

    if is_high_priority_division(division, grade_text):
        preferred_only = [
            s for s in filtered_slots
            if is_pref_venue_for_division(division, s['venue'])
        ]
        if preferred_only:
            return preferred_only

    # Everyone else just keeps the full set
    return filtered_slots

# ---------------------------
# Junior venue reservation
# ---------------------------

# Bentleigh Courts 1 & 2 (and Monash Stadium 1 & 2 if added) are the only venues
# U8s are permitted to use. On a busy day they fill up quickly with U8 games —
# and U12+ games can steal morning slots even though they could legitimately
# play anywhere else. The reservation is DEMAND-BOUNDED per day: we reserve
# exactly enough slots (earliest first) to cover that day's actual U8 demand,
# and leave the rest free for any age. This stops paid core slots from sitting
# empty just because a "junior reservation" is too aggressive.
JUNIOR_RESERVED_VENUES = LOWER_U8_BOYS_VENUES  # Bent 1&2 + Monash Stadium 1&2 + McKinnon Main

# Built lazily — set of (day, venue, time) tuples that are reserved for U8.
# Populated the first time _avoid_junior_reserved runs, then cached.
_JUNIOR_RESERVED_SLOTS: set[tuple[str, str, str]] | None = None

def _build_junior_reserved_slots() -> set[tuple[str, str, str]]:
    """
    Per day, count actual U8 fixtures and reserve that many earliest slots
    (plus a small buffer) at JUNIOR_RESERVED_VENUES. Returns a set of
    (day, venue, time) keys that U12+ fixtures should skip during
    primary/fallback, unless no alternative exists.

    The buffer pushes non-junior fixtures away from the early-afternoon Bent
    1 & 2 slots (e.g. 1:30pm, 2:15pm) even if those aren't needed for U8
    themselves. This keeps headroom for linked-constraint overflow like
    "U12 game with 7 linked teams that can only fit in one slot once all
    other venues are taken" — those games can still use these slots via the
    filter's fall-through when genuinely out of alternatives, without
    starving the 13th U8.

    HARD CAP: no slot beyond the U8 normal max start (12pm) is ever reserved.
    In practice all U8 games are placed by 11:15am, so reservation is sized
    to cover only morning slots. Slots from 12pm onwards at Bent 1&2 +
    McKinnon Main are open to all ages, recovering paid core capacity that
    was previously sitting idle.
    """
    # Count U8 demand per day
    u8_demand: dict[str, int] = defaultdict(int)
    for _, fx in pre_fixtures_df.iterrows():
        t2 = fx.get('Team 2', '')
        if pd.isna(t2) or str(t2).strip().upper() in ('', '-', 'BYE'):
            continue
        if extract_age_token_from_grade(fx['Grade']) == 'U8':
            u8_demand[fx['Day']] += 1

    reserved: set[tuple[str, str, str]] = set()
    # No buffer — reserve only as many slots as U8 demand needs. With the
    # 12pm cap (slots ≥ 12pm are never reserved), U8 demand fits comfortably
    # in morning-only Bent 1&2 + McKinnon Main slots (8:15am-11:15am).
    # Adding a buffer would over-reserve and push U12+ overflow off the day.
    BUFFER = 0

    # Reservation never extends past 12pm (the U8 normal max start). Any slot
    # at JUNIOR_RESERVED_VENUES later than this is fair game for any age. In
    # practice U8s always finish by ~11:15am, so a noon cap is safe and
    # recovers Bent 1&2 + McKinnon Main 12pm-1:30pm slots for older grades.
    U8_RESERVATION_CAP = 12 * 60  # 12:00pm

    # Group timeslots by day and pick earliest (N + BUFFER) slots WITHIN the cap
    for day, count in u8_demand.items():
        if count <= 0:
            continue
        day_slots = []
        for _, r in timeslots_df.iterrows():
            if r['Day'] != day:
                continue
            if r['Venue'] not in JUNIOR_RESERVED_VENUES:
                continue
            raw = r.get('Time_Slots', '')
            if pd.isna(raw) or not str(raw).strip():
                continue
            for t in str(raw).split(','):
                t = t.strip()
                if not t:
                    continue
                tm = time_to_minutes(t)
                if tm >= U8_RESERVATION_CAP:
                    continue  # Never reserve 12pm or later — U8 finishes by 11:15am
                day_slots.append((tm, r['Venue'], t))

        day_slots.sort(key=lambda x: x[0])
        reserve_n = count + BUFFER
        for _, venue, t in day_slots[:reserve_n]:
            reserved.add((day, venue, t))

    return reserved

def _avoid_junior_reserved(base_filtered: list[dict], grade_text: str, day: str = '') -> list[dict]:
    """
    If the fixture is not a junior that REQUIRES a junior-reserved venue, and
    non-reserved alternatives exist, filter out slots reserved for U8 demand.

    Exempt (can use reserved slots freely):
      - U8 (Bent 1&2 are their only option)
      - U10 Girls (their lower-rings set includes Bent 1&2)

    Not exempt (skip reserved slots when possible):
      - U10 Boys (no lower-rings restriction — has full venue access)
      - U10 Modified (different lower-rings set; can't use Bent 1&2 anyway)
      - U12+ (no lower-rings restriction)
    """
    global _JUNIOR_RESERVED_SLOTS
    if _JUNIOR_RESERVED_SLOTS is None:
        _JUNIOR_RESERVED_SLOTS = _build_junior_reserved_slots()

    age_token = extract_age_token_from_grade(grade_text)

    # U8: always exempt
    if age_token == 'U8':
        return base_filtered

    # U10 Girls: exempt (their lower-rings includes Bent 1&2)
    if age_token == 'U10':
        grade_u = _norm_div_name(grade_text)
        if 'GIRL' in grade_u:
            return base_filtered
        # U10 Boys / U10 Modified fall through — reservation applies

    non_reserved = [
        s for s in base_filtered
        if (day, s['venue'], s['time']) not in _JUNIOR_RESERVED_SLOTS
    ]
    return non_reserved if non_reserved else base_filtered

# ---------------------------
# Open Men venue reservation
# ---------------------------

# Open Men have a tight venue allowlist (OPEN_MEN_ALLOWED_VENUES + Cheltenham
# for Div 4+). On Sunday afternoon, U16/U18/U20 Girls/Boys grades all qualify
# for these same premium venues AND also qualify for Carnegie/Coatesville/
# Glen Huntly/Moorabbin/Tucker/South Oakleigh. Without a reservation, those
# non-Open grades fill the OM-allowlisted venues during primary pass and Open
# Men fixtures spill to unscheduled.
#
# We reserve enough Sunday-afternoon OM-allowlisted slots to cover Open Men
# demand. Non-Open grades that have alternatives skip the reserved slots
# during primary/fallback. Open Men itself (and Open Women, since they share
# the venue list) are exempt. The fall-through still allows non-Open grades
# to use these slots if no alternative exists.

_OPEN_MEN_RESERVED_SLOTS: set[tuple[str, str, str]] | None = None

def _build_open_men_reserved_slots() -> set[tuple[str, str, str]]:
    """
    Per Sunday, count actual Open Men fixtures (NOT Open Women) and reserve
    that many Sunday-afternoon (1:30pm-9pm) slots at the union of all
    Open-Men-allowed venues (top tier + Div 4+ extras).

    Why Open Men only and not Open Women? Open Men are HARD-restricted to a
    small allowlist (10 premium venues, +2 Cheltenham for Div 4+). Open Women
    have no venue restriction — they can play anywhere. So we only need to
    protect slots from teams that have nowhere else to go.

    Returns a set of (day, venue, time) tuples.
    """
    # Count Open Men demand per day
    open_demand: dict[str, int] = defaultdict(int)
    for _, fx in pre_fixtures_df.iterrows():
        t2 = fx.get('Team 2', '')
        if pd.isna(t2) or str(t2).strip().upper() in ('', '-', 'BYE'):
            continue
        grade = str(fx['Grade'])
        if is_open_men_grade(grade):
            open_demand[fx['Day']] += 1

    reserved: set[tuple[str, str, str]] = set()

    # Union of all Open-Men-allowed venues (top tier + Div 4+ extras)
    all_om_venues = OPEN_MEN_ALLOWED_VENUES | OPEN_MEN_LOWER_DIV_EXTRA_VENUES

    # Reservation window: 1:30pm onwards (Open min start) up to 9pm
    OPEN_WINDOW_START = 13 * 60 + 30
    OPEN_WINDOW_END = 21 * 60

    for day, count in open_demand.items():
        if count <= 0:
            continue
        day_slots = []
        for _, r in timeslots_df.iterrows():
            if r['Day'] != day:
                continue
            if r['Venue'] not in all_om_venues:
                continue
            raw = r.get('Time_Slots', '')
            if pd.isna(raw) or not str(raw).strip():
                continue
            for t in str(raw).split(','):
                t = t.strip()
                if not t:
                    continue
                tm = time_to_minutes(t)
                if OPEN_WINDOW_START <= tm <= OPEN_WINDOW_END:
                    day_slots.append((tm, r['Venue'], t))

        # Reserve count + small buffer to absorb churn
        BUFFER = 2
        reserve_n = min(count + BUFFER, len(day_slots))

        # Sort earliest-first; reserve the EARLIEST slots in the Open window.
        # Rationale: Open games have min start 1:30pm. Non-Open grades on
        # Sunday afternoon (U16/U18/U20) can play later anyway. Reserving
        # the earliest 1:30pm-3pm slots leaves U18/U20 to use later slots,
        # which they're happy to do (they often need 1:30pm-9pm range).
        day_slots.sort(key=lambda x: x[0])
        for _, venue, t in day_slots[:reserve_n]:
            reserved.add((day, venue, t))

    return reserved

def _avoid_open_men_reserved(base_filtered: list[dict], grade_text: str, day: str = '',
                              team1: str = '', team2: str = '') -> list[dict]:
    """
    For non-Open grades, filter out Sunday slots reserved for Open Men/Women
    if non-reserved alternatives exist. Falls through to original list when no
    alternative is available, so this never causes an unscheduled fixture.

    Exempt (always allowed reserved slots):
      - Open Men (these slots are FOR them — they have no other venue options)
      - Fixtures where either team has an unavailability rule that requires
        afternoon slots (a `<X pm` rule, where X is in the afternoon). This
        catches teams like Toorak Trailblazers whose `<1 pm` constraint means
        their only viable slots ARE the OM-restricted afternoon ones.

    NOT exempt (will skip reserved slots when alternatives exist):
      - Open Women / other Seniors — they CAN play at non-OM venues, so they
        should let Open Men have first pick of OM venues. They'll fall through
        to the unreserved set anyway.
    """
    global _OPEN_MEN_RESERVED_SLOTS
    if _OPEN_MEN_RESERVED_SLOTS is None:
        _OPEN_MEN_RESERVED_SLOTS = _build_open_men_reserved_slots()

    # Only Open Men bypass — they have no alternative venues
    if is_open_men_grade(grade_text):
        return base_filtered

    # Exempt fixtures where either team has a "no morning games" rule.
    # Their narrow window often only intersects the afternoon, so we'd rather
    # let them compete for OM-reserved slots than push them outside their rule.
    for team in (team1, team2):
        if not team:
            continue
        rule = get_unavailable(team)
        if pd.notna(rule) and str(rule).strip() and _has_afternoon_only_rule(str(rule)):
            return base_filtered

    non_reserved = [
        s for s in base_filtered
        if (day, s['venue'], s['time']) not in _OPEN_MEN_RESERVED_SLOTS
    ]
    return non_reserved if non_reserved else base_filtered

def _has_afternoon_only_rule(rule: str) -> bool:
    """
    Return True if the unavailability rule effectively confines the team to
    afternoon (≥ 12pm). Currently triggers on any `<X` rule where X is at or
    after 12pm — those teams want games at X or later.
    """
    if not rule:
        return False
    parts = re.split(r'[;,]', rule)
    for p in parts:
        p = p.strip()
        if not p or not p.startswith('<'):
            continue
        body = p[1:].strip()
        bound = time_to_minutes(body)
        if bound >= 12 * 60:  # noon or later → team plays afternoon only
            return True
    return False

def find_best_slots_for_fixture(day, division, strict_pref, linked_list,
                                slots_today, assigned_times, grade_text,
                                allowed_venues, team1: str = '', team2: str = ''):
    free_slots = [s for s in slots_today if not s['occupied']]
    if not free_slots:
        return []

    min_start = min_start_minutes_for_age(grade_text, day)
    max_start = max_start_minutes_for_age(grade_text)

    base_filtered = []
    for s in free_slots:
        tm = time_to_minutes(s['time'])
        if tm < min_start or tm > max_start:
            continue
        venue = s['venue']

        if allowed_venues is not None and venue not in allowed_venues:
            continue

        base_filtered.append(s)

    if not base_filtered:
        return []

    # Reserve Bent 1&2 / Monash Stadium for U8/U10 demand — U12+ skip past them
    # unless there's no alternative
    base_filtered = _avoid_junior_reserved(base_filtered, grade_text, day)
    base_filtered = _avoid_open_men_reserved(base_filtered, grade_text, day, team1, team2)

    # Men & U20 → core venues first if possible
    if requires_men_core_bias(grade_text, division) or requires_u20_core_bias(grade_text):
        core_only = [s for s in base_filtered if is_core_venue(s['venue'])]
        if core_only:
            base_filtered = core_only

    filtered = _apply_soft_preferred_bias(base_filtered, division, grade_text)

    filtered.sort(
        key=lambda s: _slot_priority_base(
            s, division, linked_list, assigned_times, day, grade_text,
            team1=team1, team2=team2 if team2 else None
        )
    )
    return filtered

def find_fallback_slots_for_fixture(day, division, strict_pref, linked_list,
                                    slots_today, assigned_times, grade_text,
                                    allowed_venues, team1: str = '', team2: str = ''):
    free_slots = [s for s in slots_today if not s['occupied']]
    if not free_slots:
        return []

    min_start = min_start_minutes_for_age(grade_text, day)
    max_start = max_start_minutes_for_age(grade_text)

    base_filtered = []
    for s in free_slots:
        tm = time_to_minutes(s['time'])
        if tm < min_start or tm > max_start:
            continue
        venue = s['venue']

        if allowed_venues is not None and venue not in allowed_venues:
            continue

        base_filtered.append(s)

    if not base_filtered:
        return []

    # Reserve Bent 1&2 / Monash Stadium for U8/U10 demand — U12+ skip past them
    # unless there's no alternative
    base_filtered = _avoid_junior_reserved(base_filtered, grade_text, day)
    base_filtered = _avoid_open_men_reserved(base_filtered, grade_text, day, team1, team2)

    # Men & U20 → core venues first if possible (also in fallback)
    if requires_men_core_bias(grade_text, division) or requires_u20_core_bias(grade_text):
        core_only = [s for s in base_filtered if is_core_venue(s['venue'])]
        if core_only:
            base_filtered = core_only

    filtered = _apply_soft_preferred_bias(base_filtered, division, grade_text)

    filtered.sort(
        key=lambda s: _slot_priority_base(
            s, division, linked_list, assigned_times, day, grade_text,
            team1=team1, team2=team2 if team2 else None
        )
    )
    return filtered

# ---------------------------
# Fixture helpers: has unavailability
# ---------------------------

def fixture_has_unavailability(pre_fixtures_round: pd.DataFrame, idx: int) -> bool:
    """
    Return True if either team in this fixture has a non-empty Unavailable_Times.
    Used to group 'request' fixtures to be scheduled first per day.
    """
    row = pre_fixtures_round.loc[idx]
    team1 = row['Team 1']
    team2_raw = row['Team 2']

    # Resolve Team 2 (skip BYEs)
    team2 = None
    if pd.notna(team2_raw):
        t2s = str(team2_raw).strip().upper()
        if t2s not in ('', '-', 'BYE'):
            team2 = row['Team 2']

    u1 = get_unavailable(team1)
    if pd.notna(u1) and str(u1).strip():
        return True

    if team2 is not None:
        u2 = get_unavailable(team2)
        if pd.notna(u2) and str(u2).strip():
            return True

    return False

# ---------------------------
# Basic sort key (age → idx)
# ---------------------------

def fixture_sort_key(pre_fixtures_round: pd.DataFrame, idx: int):
    """
    Basic sort key: Age_Order + original index.
    (Not used in main passes, kept for reference/compatibility.)
    """
    row = pre_fixtures_round.loc[idx]
    age_order = row['Age_Order']
    return (age_order, idx)

# ---------------------------
# Flexibility / tightness metric
# ---------------------------

def fixture_flexibility_score(
    pre_fixtures_round: pd.DataFrame,
    idx: int,
    day: str,
    slots_today: list[dict],
) -> int:
    """
    Estimate how 'tight' this fixture's usable window is for the given day.

    We count how many *currently-free* slots:
      - fall within the age min/max window,
      - satisfy lower-ring venue rules,
      - do NOT violate either team's Unavailable_Times.

    Lower counts = tighter window = scheduled earlier (within the same age band).

    Note: we factor in BOTH teams' unavailability, even though the scheduler
    treats Team 1 as priority. Reason: the IDEAL placement satisfies both. If
    Team 2 has a narrow window (e.g. 'no games before 7pm'), this fixture
    should still be scheduled before others that have looser windows — even if
    Team 1 has no constraints — because the joint window is still narrow and
    those late slots disappear quickly.
    """
    row = pre_fixtures_round.loc[idx]
    team1 = row['Team 1']
    team2_raw = row.get('Team 2', None)
    team2 = None
    if team2_raw is not None and not pd.isna(team2_raw):
        t2s = str(team2_raw).strip().upper()
        if t2s not in ('', '-', 'BYE'):
            team2 = row['Team 2']

    grade_text = row['Grade']
    age = row['Age']
    gender = row['Gender']
    division = row['Division']

    allowed_venues = get_lower_ring_allowed_venues(day, age, gender, division, grade_text)

    if is_open_men_grade(grade_text):
        allowed_venues = get_open_men_allowed_venues(grade_text)
    
    unavail1 = get_unavailable(team1)
    unavail2 = get_unavailable(team2) if team2 is not None else None

    min_start = min_start_minutes_for_age(grade_text, day)
    max_start = max_start_minutes_for_age(grade_text)

    free_slots = [s for s in slots_today if not s.get('occupied')]
    if not free_slots:
        # No capacity left anyway; treat as very flexible to avoid skew
        return 10_000

    usable = 0
    for s in free_slots:
        tm = time_to_minutes(s['time'])
        if tm < min_start or tm > max_start:
            continue
        venue = s['venue']
        if allowed_venues is not None and venue not in allowed_venues:
            continue
        if is_time_blocked(s['time'], unavail1):
            continue
        if unavail2 is not None and is_time_blocked(s['time'], unavail2):
            continue
        usable += 1

    if usable == 0:
        # No legal slot under current constraints → they will likely need force-pass.
        # Put them after games that still *can* be placed legally.
        return len(free_slots) + 1_000

    return usable

# ---------------------------
# Linked-cluster mapping (union-find)
# ---------------------------

def _build_linked_clusters() -> dict[str, int]:
    """
    Build a map from team name → cluster id, where all teams connected via the
    Linked_Team1..Linked_Team8 graph share the same cluster id.

    Used during fixture sorting so that fixtures belonging to the same linked
    family are processed near each other on a given day. The first fixture
    anchors a venue/time, and subsequent linked fixtures can cluster and
    back-to-back off it.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for _, row in teams_df.iterrows():
        t = str(row['Team']).strip()
        if not t:
            continue
        parent.setdefault(t, t)
        for col in LINKED_COLS:
            v = row.get(col, '')
            if pd.notna(v) and str(v).strip():
                lt = str(v).strip()
                parent.setdefault(lt, lt)
                union(t, lt)

    # Assign a stable integer id per root. Teams not in any cluster keep their
    # own unique id (large number) so they don't coincidentally group together.
    roots = {}
    next_id = 0
    out = {}
    for team in parent:
        r = find(team)
        if r not in roots:
            roots[r] = next_id
            next_id += 1
        out[team] = roots[r]
    return out

TEAM_TO_CLUSTER: dict[str, int] = _build_linked_clusters()

# Runtime assignment of a "home court" per (cluster_id, day). The first time a
# heavy-linked cluster places a game on a given day, we record the court it
# landed on. Subsequent games for that cluster on the same day strongly prefer
# that exact court, so a multi-team club (e.g. St Kilda Warriors) ends up with
# all its siblings funnelled onto one or two courts in a continuous block,
# rather than scattered across many venues. This mirrors the real-world
# fixture patterns where one club's teams share a single court for the day.
# Keyed by (cluster_id, day) → composite venue string. Reset each round so a
# cluster can use a different court on Saturday vs Sunday or in another round.
CLUSTER_HOME_COURT: dict[tuple[int, str], str] = {}

def _reset_cluster_home_courts():
    CLUSTER_HOME_COURT.clear()

def _record_cluster_home_court(team1, team2, day, venue):
    """
    If this fixture belongs to a heavy-linked cluster and that cluster has no
    home court yet for this day, set it to the venue just used. Called at every
    placement commit so the first placed game of a club fixes its home court.
    """
    if not venue:
        return
    max_links = max(
        _count_links(team1) if team1 else 0,
        _count_links(team2) if team2 is not None and not pd.isna(team2) else 0,
    )
    if max_links < HEAVY_LINKED_THRESHOLD:
        return
    cid = _fixture_cluster_id(team1, team2)
    key = (cid, day)
    if key not in CLUSTER_HOME_COURT:
        CLUSTER_HOME_COURT[key] = venue

def _fixture_cluster_id(team1: str, team2) -> int:
    """
    Return a cluster id for a fixture. When both teams are linked, prefer the
    cluster of the team with MORE links — this ensures that a cross-club fixture
    between (e.g.) a 7-linked St Kilda Warrior and a 2-linked JETS opponent
    gets tagged with the SKW cluster, so SKW's home-court bias and clustering
    logic apply. Without this, the JETS-side cluster would 'own' the fixture
    and SKW's home court would never see this game.

    Falls back to Team 1's cluster if both are unlinked (impossible in practice
    since unlinked teams use the sentinel below).
    """
    t1s = str(team1).strip() if team1 is not None and not pd.isna(team1) else ''
    t2s = str(team2).strip() if team2 is not None and not pd.isna(team2) else ''

    cid1 = TEAM_TO_CLUSTER.get(t1s)
    cid2 = TEAM_TO_CLUSTER.get(t2s) if t2s else None

    # Both teams linked → pick the cluster of the more-linked team
    if cid1 is not None and cid2 is not None:
        n1 = _count_links(t1s)
        n2 = _count_links(t2s)
        return cid1 if n1 >= n2 else cid2

    if cid1 is not None:
        return cid1
    if cid2 is not None:
        return cid2

    # Sentinel: unique per unlinked fixture.
    return 10_000_000 + hash(t1s) % 1_000_000

def sort_indices_for_day(indices, pre_fixtures_round, day, slots_today):
    """
    Sort fixture indices for a given day by:
      0) Heavy-linked clubs FIRST (≥ HEAVY_LINKED_THRESHOLD linked teams),
         and within that, sort by link count descending — so clubs with
         many siblings (e.g. St Kilda Warriors, 7 linked teams) get processed
         before clubs with fewer (e.g. MM, 4 linked teams). This matters
         because heavily-tied clubs need a longer continuous block at one
         court; processing them first lets them claim the full block before
         other heavy-linked clubs take individual slots within that block.
      1) Age (younger ages first within the heavy-linked / normal buckets),
      2) Flexibility (fewest usable slots first within each age),
      3) Linked-cluster id so linked teams are processed consecutively,
      4) Deterministic random order,
      5) Original index (stable tie-breaker).
    """
    def link_count(i):
        t1 = pre_fixtures_round.loc[i, 'Team 1']
        t2 = pre_fixtures_round.loc[i, 'Team 2']
        try:
            n1 = _count_links(t1) if pd.notna(t1) else 0
        except Exception:
            n1 = 0
        try:
            n2 = _count_links(t2) if pd.notna(t2) else 0
        except Exception:
            n2 = 0
        return max(n1, n2)

    def heavy_linked_tier(i):
        # 0 = heavy-linked (≥ threshold), 1 = normal
        return 0 if link_count(i) >= HEAVY_LINKED_THRESHOLD else 1

    return sorted(
        indices,
        key=lambda i: (
            heavy_linked_tier(i),
            -link_count(i),                # most-linked first within tier
            pre_fixtures_round.loc[i, 'Age_Order'],
            fixture_flexibility_score(pre_fixtures_round, i, day, slots_today),
            _fixture_cluster_id(
                pre_fixtures_round.loc[i, 'Team 1'],
                pre_fixtures_round.loc[i, 'Team 2'],
            ),
            pre_fixtures_round.loc[i, 'Random_Order'],
            i,
        )
    )

# ---------------------------
# Slot picker with Team 1 priority
# ---------------------------

def pick_slot_respecting_team1(
    day,
    candidate_slots,
    team1,
    team2,
    linked_list,
    assigned_times,
    unavailable1,
    unavailable2,
    respect_linked: bool = True,
    allow_violate_team1: bool = False,
):
    """
    Given a sorted list of candidate_slots for one fixture, pick a slot with:
      1) Prefer slots that satisfy BOTH teams' Unavailable_Times.
      2) Next, prefer slots that satisfy Team 1 but violate Team 2.
      3) If allow_violate_team1=True and still nothing, allow slots that
         violate Team 1 as a last resort (but still never double-book teams
         or break lower-rings / age-window filters).
    """
    best_team1_and_team2 = None
    best_team1_only = None
    best_violating_team1 = None

    for slot in candidate_slots:
        time_str = slot['time']
        venue = slot['venue']

        # Linked spacing (skipped in force-pass if respect_linked=False)
        if respect_linked and violates_linked_constraints(day, time_str, venue, linked_list, assigned_times):
            continue

        # Don't double-book a team at the same time
        if violates_self_constraints(team1, day, time_str, assigned_times):
            continue
        if team2 is not None and violates_self_constraints(team2, day, time_str, assigned_times):
            continue

        blocked1 = is_time_blocked(time_str, unavailable1)
        blocked2 = is_time_blocked(time_str, unavailable2)

        # If this slot violates Team 1's request
        if blocked1:
            if allow_violate_team1 and best_violating_team1 is None:
                best_violating_team1 = slot
            continue

        # At this point, Team 1 is happy with this slot.

        # Ideal: satisfy both teams
        if not blocked2:
            best_team1_and_team2 = slot
            break  # candidate_slots already sorted by priority
        else:
            # Works for Team 1 but conflicts with Team 2's request.
            if best_team1_only is None:
                best_team1_only = slot

    if best_team1_and_team2 is not None:
        return best_team1_and_team2
    if best_team1_only is not None:
        return best_team1_only
    if allow_violate_team1:
        return best_violating_team1

    return None

# ---------------------------
# Junior rebalancing helpers
# ---------------------------

def is_junior_grade(grade_text: str) -> bool:
    """
    Treat U8, U10, U12 as 'junior' for the purpose of time-window rebalancing.
    """
    token = extract_age_token_from_grade(grade_text)
    return token in ('U8', 'U10', 'U12')

def _would_create_self_double_book(
    final_fixtures,
    day,
    day_indices,
    idx_j,
    idx_o,
    new_time_j,
    new_time_o,
):
    """
    Check if swapping the times for fixtures idx_j and idx_o would cause any team
    to appear in two fixtures at the same time on this day.
    """
    times_by_team = {}

    for idx in day_indices:
        fx = final_fixtures[idx]

        # Determine which time applies under the hypothetical swap
        if idx == idx_j:
            time = new_time_j
        elif idx == idx_o:
            time = new_time_o
        else:
            time = fx.get('Time', '')

        if not time:
            continue

        for team_key in ('Team 1', 'Team 2'):
            team = fx.get(team_key, '')
            if team is None or str(team).strip().upper() in ('', '-', 'BYE'):
                continue

            if team not in times_by_team:
                times_by_team[team] = set()

            if time in times_by_team[team]:
                return True
            times_by_team[team].add(time)

    return False

def _can_swap_junior_pair(
    final_fixtures,
    day,
    day_indices,
    idx_j,
    idx_o,
):
    """
    Decide if we can swap (Time, Venue) between:
      - idx_j: a junior (U8/U10/U12) fixture that is currently after its max,
      - idx_o: an older-age fixture on the same day in an earlier timeslot.

    Conditions:
      - Junior's new time must be within its normal min/max window.
      - Older fixture's new time must be >= its minimum age start, and <= 9:00pm.
      - Lower-rings venue rules must still be satisfied for both fixtures.
      - Team 1's request for both fixtures must not be violated at the new times
        (unless they were already in violation – we don't make it worse).
      - No team may be double-booked at the same time after the swap.
    """
    fj = final_fixtures[idx_j]
    fo = final_fixtures[idx_o]

    t_j_old = fj['Time']
    v_j_old = fj['Venue']
    g_j = fj['Grade']
    team1_j = fj['Team 1']

    t_o_old = fo['Time']
    v_o_old = fo['Venue']
    g_o = fo['Grade']
    team1_o = fo['Team 1']

    if not t_j_old or not t_o_old:
        return False

    # Proposed new assignments
    t_j_new = t_o_old
    v_j_new = v_o_old
    t_o_new = t_j_old
    v_o_new = v_j_old

    # Parse both grades
    day_j_str, age_j, gender_j, div_j = parse_grade(g_j)
    day_o_str, age_o, gender_o, div_o = parse_grade(g_o)

    # Lower-rings: juniors must still be on allowed venue after swap
    allowed_j = get_lower_ring_allowed_venues(day, age_j, gender_j, div_j, g_j)
    if allowed_j is not None and v_j_new not in allowed_j:
        return False

    allowed_o = get_lower_ring_allowed_venues(day, age_o, gender_o, div_o, g_o)
    if allowed_o is not None and v_o_new not in allowed_o:
        return False

    # Age windows
    min_j = min_start_minutes_for_age(g_j, day)
    max_j = max_start_minutes_for_age(g_j)
    t_j_new_min = time_to_minutes(t_j_new)

    # Junior must be fully inside its normal window after swap
    if not (min_j <= t_j_new_min <= max_j):
        return False

    min_o = min_start_minutes_for_age(g_o, day)
    t_o_new_min = time_to_minutes(t_o_new)

    # Older fixture can't go earlier than its minimum and not later than 9pm
    if t_o_new_min < min_o:
        return False
    if t_o_new_min > 21 * 60:  # 9:00pm hard cap
        return False

    # Team 1 unavailability: don't introduce new violations
    unavail_j1 = get_unavailable(team1_j)
    if unavail_j1 is not None:
        cur_blocked_j = is_time_blocked(t_j_old, unavail_j1)
        new_blocked_j = is_time_blocked(t_j_new, unavail_j1)
        if not cur_blocked_j and new_blocked_j:
            return False

    unavail_o1 = get_unavailable(team1_o)
    if unavail_o1 is not None:
        cur_blocked_o = is_time_blocked(t_o_old, unavail_o1)
        new_blocked_o = is_time_blocked(t_o_new, unavail_o1)
        if not cur_blocked_o and new_blocked_o:
            return False

    # Avoid creating double-bookings for any team on this day
    if _would_create_self_double_book(
        final_fixtures,
        day,
        day_indices,
        idx_j,
        idx_o,
        t_j_new,
        t_o_new,
    ):
        return False

    return True

def repack_unscheduled(
    pre_fixtures_round,
    processed_indices,
    final_fixtures,
    master_slots,
    assigned_times,
):
    """
    Last-chance pass to rescue fixtures that couldn't be placed.

    For each unplaced fixture, we look for a swap target: a currently-placed
    fixture whose slot the unplaced fixture COULD use, AND for which we can
    find a legal alternative slot to move the placed fixture into.

    We only commit a swap if both halves succeed — never break a valid
    placement to enable an invalid one. The unplaced fixture's slot
    requirements are evaluated under the same rules used during normal
    placement (allowed venues, age windows, lower-rings, team-1
    unavailability, linked spacing, no team double-booking).

    This typically rescues 0-10 fixtures per round in tight scenarios.
    """
    # Index final_fixtures by (Team 1, Team 2) for fast lookup
    placed_by_teams: dict[tuple[str, str], int] = {}
    for i, f in enumerate(final_fixtures):
        if f.get('Team 2') == 'BYE' or not f.get('Time'):
            continue
        placed_by_teams[(str(f['Team 1']), str(f['Team 2']))] = i

    # Iterate unplaced fixtures
    for idx, fixture in pre_fixtures_round.iterrows():
        if idx in processed_indices:
            continue
        team2_raw = fixture['Team 2']
        if pd.isna(team2_raw) or str(team2_raw).strip().upper() in ('', '-', 'BYE'):
            continue

        team1 = fixture['Team 1']
        team2 = fixture['Team 2']
        grade_text = fixture['Grade']
        day = fixture['Day']
        division = fixture['Division']

        unavail1 = get_unavailable(team1)
        unavail2 = get_unavailable(team2)

        # Find legal venues + age window for this unplaced fixture
        if is_open_men_grade(grade_text):
            allowed_venues = get_open_men_allowed_venues(grade_text)
        else:
            allowed_venues = get_lower_ring_allowed_venues(
                day, fixture['Age'], fixture['Gender'], division, grade_text
            )
        min_start = min_start_minutes_for_age(grade_text, day)
        max_start = force_pass_max_start_minutes_for_age(grade_text)

        linked_list = get_linked_teams(team1)

        slots_today = master_slots.get(day, [])

        # Candidate slots that this unplaced fixture COULD legally use
        # (regardless of current occupied state — we'll check swaps below)
        candidate_keys = []
        for s in slots_today:
            tm = time_to_minutes(s['time'])
            if tm < min_start or tm > max_start:
                continue
            if allowed_venues is not None and s['venue'] not in allowed_venues:
                continue
            if is_time_blocked(s['time'], unavail1):
                continue
            if violates_self_constraints(team1, day, s['time'], assigned_times):
                continue
            if violates_self_constraints(team2, day, s['time'], assigned_times):
                continue
            candidate_keys.append(s)

        if not candidate_keys:
            continue  # Nothing could rescue this fixture

        # Try each candidate slot. If it's free, place directly.
        # Otherwise see if we can displace its current occupant.
        rescued = False
        for s in candidate_keys:
            if not s.get('occupied'):
                # Should not happen normally (force-pass would have caught it),
                # but protect against it just in case.
                if violates_linked_constraints(day, s['time'], s['venue'], linked_list, assigned_times):
                    continue
                # Place
                final_fixtures.append({
                    'Team 1': team1,
                    'Team 2': team2,
                    'Grade': grade_text,
                    'Venue': s['venue'],
                    'Day': day,
                    'Time': s['time'],
                })
                assigned_times.setdefault(team1, []).append((day, s['time'], s['venue']))
                assigned_times.setdefault(team2, []).append((day, s['time'], s['venue']))
                _record_cluster_home_court(team1, team2, day, s['venue'])
                s['occupied'] = True
                processed_indices.add(idx)
                rescued = True
                break

            # Slot is taken. Find what's there.
            occupant_idx = None
            occupant_fix = None
            for i, f in enumerate(final_fixtures):
                if (f.get('Day') == day and f.get('Time') == s['time']
                        and f.get('Venue') == s['venue']
                        and f.get('Team 2') != 'BYE'):
                    occupant_idx = i
                    occupant_fix = f
                    break
            if occupant_fix is None:
                continue

            # Don't try to swap if occupant shares a team with us
            if occupant_fix['Team 1'] in (team1, team2) or occupant_fix['Team 2'] in (team1, team2):
                continue

            occ_t1 = occupant_fix['Team 1']
            occ_t2 = occupant_fix['Team 2']
            occ_grade = occupant_fix['Grade']
            occ_day = occupant_fix['Day']

            occ_age = ''
            occ_gender = ''
            occ_division = ''
            for _, fxr in pre_fixtures_round.iterrows():
                if fxr['Team 1'] == occ_t1 and fxr['Team 2'] == occ_t2 and fxr['Grade'] == occ_grade:
                    occ_age = fxr['Age']
                    occ_gender = fxr['Gender']
                    occ_division = fxr['Division']
                    break

            occ_unavail1 = get_unavailable(occ_t1)
            occ_unavail2 = get_unavailable(occ_t2)
            occ_linked = get_linked_teams(occ_t1)

            if is_open_men_grade(occ_grade):
                occ_allowed = get_open_men_allowed_venues(occ_grade)
            else:
                occ_allowed = get_lower_ring_allowed_venues(
                    occ_day, occ_age, occ_gender, occ_division, occ_grade
                )
            occ_min = min_start_minutes_for_age(occ_grade, occ_day)
            occ_max = force_pass_max_start_minutes_for_age(occ_grade)

            # Temporarily remove occupant from assigned_times so we can
            # search for an alternative slot for them.
            for tm_team in (occ_t1, occ_t2):
                lst = assigned_times.get(tm_team, [])
                key = (occ_day, s['time'], s['venue'])
                if key in lst:
                    lst.remove(key)

            # Look for an alternative free slot that can host the occupant.
            alt_slot = None
            for cand in slots_today:
                if cand.get('occupied'):
                    continue
                if cand['venue'] == s['venue'] and cand['time'] == s['time']:
                    continue
                tm = time_to_minutes(cand['time'])
                if tm < occ_min or tm > occ_max:
                    continue
                if occ_allowed is not None and cand['venue'] not in occ_allowed:
                    continue
                if is_time_blocked(cand['time'], occ_unavail1):
                    continue
                if violates_self_constraints(occ_t1, occ_day, cand['time'], assigned_times):
                    continue
                if violates_self_constraints(occ_t2, occ_day, cand['time'], assigned_times):
                    continue
                if violates_linked_constraints(occ_day, cand['time'], cand['venue'], occ_linked, assigned_times):
                    continue
                alt_slot = cand
                break

            if alt_slot is None:
                # Restore the occupant's assignment record and skip.
                assigned_times.setdefault(occ_t1, []).append((occ_day, s['time'], s['venue']))
                assigned_times.setdefault(occ_t2, []).append((occ_day, s['time'], s['venue']))
                continue

            # Verify our linked spacing into the freed slot is OK
            if violates_linked_constraints(day, s['time'], s['venue'], linked_list, assigned_times):
                # Restore and skip
                assigned_times.setdefault(occ_t1, []).append((occ_day, s['time'], s['venue']))
                assigned_times.setdefault(occ_t2, []).append((occ_day, s['time'], s['venue']))
                continue

            # Commit the swap.
            occupant_fix['Venue'] = alt_slot['venue']
            occupant_fix['Time'] = alt_slot['time']
            assigned_times.setdefault(occ_t1, []).append((occ_day, alt_slot['time'], alt_slot['venue']))
            assigned_times.setdefault(occ_t2, []).append((occ_day, alt_slot['time'], alt_slot['venue']))
            alt_slot['occupied'] = True

            final_fixtures.append({
                'Team 1': team1,
                'Team 2': team2,
                'Grade': grade_text,
                'Venue': s['venue'],
                'Day': day,
                'Time': s['time'],
            })
            assigned_times.setdefault(team1, []).append((day, s['time'], s['venue']))
            assigned_times.setdefault(team2, []).append((day, s['time'], s['venue']))
            # Note: s['occupied'] stays True (we replaced the occupant, not vacated)
            processed_indices.add(idx)
            rescued = True
            break

def rebalance_junior_lates(final_fixtures):
    """
    Post-processing step per round:
      - For each day, find junior fixtures (U8/U10/U12) that are scheduled
        after their normal max start time.
      - For each such fixture, attempt to swap its (Time, Venue) with an
        older-age fixture on the same day in an earlier timeslot, subject to:
            · lower-rings constraints,
            · age min/max rules,
            · Team 1's unavailability (no new violations),
            · and no team double-bookings at the same time.
    """
    # Build mapping from day -> indices in final_fixtures
    day_to_indices = defaultdict(list)
    for idx, fx in enumerate(final_fixtures):
        day = fx.get('Day', None)
        t = fx.get('Time', '')
        if day is None or not t:
            continue
        day_to_indices[day].append(idx)

    for day, indices in day_to_indices.items():
        if not indices:
            continue

        # Safety: cap iterations to avoid any weird cycles
        max_iterations = 1000
        iterations = 0

        # Keep trying swaps until no more improvements can be found
        while True:
            changed = False

            # Sort day's fixtures by current time
            indices_sorted = sorted(
                indices,
                key=lambda i: time_to_minutes(final_fixtures[i]['Time'])
            )

            for idx_j in indices_sorted:
                fj = final_fixtures[idx_j]
                grade_j = fj['Grade']
                if not is_junior_grade(grade_j):
                    continue

                t_j = fj['Time']
                if not t_j:
                    continue

                t_j_min = time_to_minutes(t_j)
                max_j = max_start_minutes_for_age(grade_j)

                # Only consider juniors that are currently after their max window
                if t_j_min <= max_j:
                    continue

                # Look for an older-age fixture in an earlier timeslot to swap with
                for idx_o in indices_sorted:
                    if idx_o == idx_j:
                        continue

                    fo = final_fixtures[idx_o]
                    t_o = fo['Time']
                    if not t_o:
                        continue

                    t_o_min = time_to_minutes(t_o)
                    if t_o_min >= t_j_min:
                        # Only earlier games can help move the junior earlier
                        break

                    # Only swap with a strictly older age band
                    if age_sort_key_from_grade(fo['Grade']) <= age_sort_key_from_grade(grade_j):
                        continue

                    if not _can_swap_junior_pair(
                        final_fixtures,
                        day,
                        indices,
                        idx_j,
                        idx_o,
                    ):
                        continue

                    # Perform the swap
                    t_j_old = fj['Time']
                    v_j_old = fj['Venue']
                    t_o_old = fo['Time']
                    v_o_old = fo['Venue']

                    fj['Time'] = t_o_old
                    fj['Venue'] = v_o_old
                    fo['Time'] = t_j_old
                    fo['Venue'] = v_j_old

                    changed = True
                    break  # Stop searching for this junior; move to next

                if changed:
                    break  # restart the while-loop with updated times

            iterations += 1
            if not changed or iterations >= max_iterations:
                break  # no more beneficial swaps (or too many iterations)

# ---------------------------
# Per-round scheduling
# ---------------------------

def schedule_round(pre_fixtures_round: pd.DataFrame, round_label) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Schedule all fixtures for a single Round (e.g. Round 1, 2, 3...).
    Returns (scheduled_df_round, unscheduled_df_round).
    """
    master_slots = build_master_slots()

    # Each round gets a fresh set of cluster→home-court assignments so a club
    # can use a different court in a different round (and Saturday vs Sunday).
    _reset_cluster_home_courts()

    assigned_times: dict[str, list[tuple[str, str, str]]] = {}
    final_fixtures: list[dict] = []
    unscheduled: list[dict] = []
    processed_indices: set[int] = set()

    # Stage A: collect BYE rows
    bye_rows = []
    for idx, fixture in pre_fixtures_round.iterrows():
        team2_raw = fixture['Team 2']
        if pd.isna(team2_raw):
            team2_raw = ''
        team2_str = str(team2_raw).strip().upper()
        is_bye = team2_str in ('', '-', 'BYE')
        if is_bye:
            bye_rows.append((idx, fixture))

    days = list(pre_fixtures_round['Day'].dropna().unique())

    # Stage 1: primary pass (requests first, then others, per day)
    for day in days:
        slots_today = master_slots.get(day, [])
        if not slots_today:
            continue

        day_mask = pre_fixtures_round['Day'] == day
        day_rows = pre_fixtures_round[day_mask]

        req_indices = []
        no_req_indices = []
        for idx, _ in day_rows.iterrows():
            if idx in processed_indices:
                continue
            if fixture_has_unavailability(pre_fixtures_round, idx):
                req_indices.append(idx)
            else:
                no_req_indices.append(idx)

        req_sorted = sort_indices_for_day(req_indices, pre_fixtures_round, day, slots_today)
        no_req_sorted = sort_indices_for_day(no_req_indices, pre_fixtures_round, day, slots_today)
        day_indices_sorted = req_sorted + no_req_sorted

        for idx in day_indices_sorted:
            if idx in processed_indices:
                continue

            fixture = pre_fixtures_round.loc[idx]
            team1 = fixture['Team 1']
            team2_raw = fixture['Team 2']

            if pd.isna(team2_raw):
                team2_raw = ''
            team2_str = str(team2_raw).strip().upper()
            team2 = None if team2_str in ('', '-', 'BYE') else fixture['Team 2']

            if team2 is None:
                continue

            division = fixture['Division']
            age = fixture['Age']
            gender = fixture['Gender']
            grade_text = fixture['Grade']

            strict_pref = requires_strict_preferred_venue(day, age, gender, division, grade_text)
            allowed_venues = get_lower_ring_allowed_venues(day, age, gender, division, grade_text)

            if is_open_men_grade(grade_text):
                allowed_venues = get_open_men_allowed_venues(grade_text)

            linked_list = get_linked_teams(team1) + get_linked_teams(team2)
            linked_list = list(dict.fromkeys(linked_list))

            unavailable1 = get_unavailable(team1)
            unavailable2 = get_unavailable(team2)

            candidate_slots = find_best_slots_for_fixture(
                day=day,
                division=division,
                strict_pref=strict_pref,
                linked_list=linked_list,
                slots_today=slots_today,
                assigned_times=assigned_times,
                grade_text=grade_text,
                allowed_venues=allowed_venues,
                team1=team1,
                team2=team2 if team2 is not None else '',
            )

            if not candidate_slots:
                continue

            chosen = pick_slot_respecting_team1(
                day=day,
                candidate_slots=candidate_slots,
                team1=team1,
                team2=team2,
                linked_list=linked_list,
                assigned_times=assigned_times,
                unavailable1=unavailable1,
                unavailable2=unavailable2,
                respect_linked=True,
                allow_violate_team1=False,  # never violate Team 1 in primary pass
            )

            if not chosen:
                continue

            time_str = chosen['time']
            venue = chosen['venue']

            final_fixtures.append({
                'Team 1': team1,
                'Team 2': team2,
                'Grade': grade_text,
                'Venue': venue,
                'Day': day,
                'Time': time_str
            })
            assigned_times.setdefault(team1, []).append((day, time_str, venue))
            assigned_times.setdefault(team2, []).append((day, time_str, venue))
            _record_cluster_home_court(team1, team2, day, venue)
            chosen['occupied'] = True
            processed_indices.add(idx)

    # Stage 2: fallback pass (same requests-first ordering)
    for day in days:
        slots_today = master_slots.get(day, [])
        if not slots_today:
            continue

        if not any(not s['occupied'] for s in slots_today):
            continue

        day_mask = pre_fixtures_round['Day'] == day
        day_rows = pre_fixtures_round[day_mask]

        req_indices = []
        no_req_indices = []
        for idx, _ in day_rows.iterrows():
            if idx in processed_indices:
                continue
            if fixture_has_unavailability(pre_fixtures_round, idx):
                req_indices.append(idx)
            else:
                no_req_indices.append(idx)

        leftover_sorted = (
            sort_indices_for_day(req_indices, pre_fixtures_round, day, slots_today) +
            sort_indices_for_day(no_req_indices, pre_fixtures_round, day, slots_today)
        )

        if not leftover_sorted:
            continue

        for idx in leftover_sorted:
            if idx in processed_indices:
                continue

            fixture = pre_fixtures_round.loc[idx]
            team1 = fixture['Team 1']
            team2_raw = fixture['Team 2']

            if pd.isna(team2_raw):
                team2_raw = ''
            team2_str = str(team2_raw).strip().upper()
            team2 = None if team2_str in ('', '-', 'BYE') else fixture['Team 2']

            if team2 is None:
                continue

            division = fixture['Division']
            age = fixture['Age']
            gender = fixture['Gender']
            grade_text = fixture['Grade']

            strict_pref = requires_strict_preferred_venue(day, age, gender, division, grade_text)
            allowed_venues = get_lower_ring_allowed_venues(day, age, gender, division, grade_text)

            if is_open_men_grade(grade_text):
                allowed_venues = get_open_men_allowed_venues(grade_text)

            linked_list = get_linked_teams(team1) + get_linked_teams(team2)
            linked_list = list(dict.fromkeys(linked_list))

            unavailable1 = get_unavailable(team1)
            unavailable2 = get_unavailable(team2)

            candidate_slots = find_fallback_slots_for_fixture(
                day=day,
                division=division,
                strict_pref=strict_pref,
                linked_list=linked_list,
                slots_today=slots_today,
                assigned_times=assigned_times,
                grade_text=grade_text,
                allowed_venues=allowed_venues,
                team1=team1,
                team2=team2 if team2 is not None else '',
            )

            if not candidate_slots:
                continue

            chosen = pick_slot_respecting_team1(
                day=day,
                candidate_slots=candidate_slots,
                team1=team1,
                team2=team2,
                linked_list=linked_list,
                assigned_times=assigned_times,
                unavailable1=unavailable1,
                unavailable2=unavailable2,
                respect_linked=True,
                allow_violate_team1=False,  # still never violate Team 1 here
            )

            if not chosen:
                continue

            time_str = chosen['time']
            venue = chosen['venue']

            final_fixtures.append({
                'Team 1': team1,
                'Team 2': team2,
                'Grade': grade_text,
                'Venue': venue,
                'Day': day,
                'Time': time_str
            })
            assigned_times.setdefault(team1, []).append((day, time_str, venue))
            assigned_times.setdefault(team2, []).append((day, time_str, venue))
            _record_cluster_home_court(team1, team2, day, venue)
            chosen['occupied'] = True
            processed_indices.add(idx)

    # Stage 2.5: relaxed-linked pass
    #
    # Last chance to honour Team 1's Unavailable_Times BEFORE force-pass gets
    # permission to violate it. Same ordering as fallback, but we ignore the
    # 90-minute cross-venue spacing between linked teams. All other rules
    # (age window, lower-rings, Team 1 request, no double-booking) still hold.
    #
    # This catches the common case where a tight Team 1 window (e.g. 10:00-
    # 11:30am) only had a slot at a venue that was blocked by linked-team
    # spacing. Better to place the game at that slot than dump it into
    # force-pass where it might land hours outside the requested window.
    for day in days:
        slots_today = master_slots.get(day, [])
        if not slots_today:
            continue
        if not any(not s['occupied'] for s in slots_today):
            continue

        day_mask = pre_fixtures_round['Day'] == day
        day_rows = pre_fixtures_round[day_mask]

        req_indices = []
        no_req_indices = []
        for idx, _ in day_rows.iterrows():
            if idx in processed_indices:
                continue
            if fixture_has_unavailability(pre_fixtures_round, idx):
                req_indices.append(idx)
            else:
                no_req_indices.append(idx)

        leftover_sorted = (
            sort_indices_for_day(req_indices, pre_fixtures_round, day, slots_today) +
            sort_indices_for_day(no_req_indices, pre_fixtures_round, day, slots_today)
        )

        if not leftover_sorted:
            continue

        for idx in leftover_sorted:
            if idx in processed_indices:
                continue

            fixture = pre_fixtures_round.loc[idx]
            team1 = fixture['Team 1']
            team2_raw = fixture['Team 2']

            if pd.isna(team2_raw):
                team2_raw = ''
            team2_str = str(team2_raw).strip().upper()
            team2 = None if team2_str in ('', '-', 'BYE') else fixture['Team 2']

            if team2 is None:
                continue

            division = fixture['Division']
            age = fixture['Age']
            gender = fixture['Gender']
            grade_text = fixture['Grade']

            strict_pref = requires_strict_preferred_venue(day, age, gender, division, grade_text)
            allowed_venues = get_lower_ring_allowed_venues(day, age, gender, division, grade_text)

            if is_open_men_grade(grade_text):
                allowed_venues = get_open_men_allowed_venues(grade_text)

            linked_list = get_linked_teams(team1) + get_linked_teams(team2)
            linked_list = list(dict.fromkeys(linked_list))

            unavailable1 = get_unavailable(team1)
            unavailable2 = get_unavailable(team2)

            candidate_slots = find_fallback_slots_for_fixture(
                day=day,
                division=division,
                strict_pref=strict_pref,
                linked_list=linked_list,
                slots_today=slots_today,
                assigned_times=assigned_times,
                grade_text=grade_text,
                allowed_venues=allowed_venues,
                team1=team1,
                team2=team2 if team2 is not None else '',
            )

            if not candidate_slots:
                continue

            # Key change from fallback: respect_linked=False. We'll accept a
            # linked-spacing clash in order to keep Team 1's request intact.
            chosen = pick_slot_respecting_team1(
                day=day,
                candidate_slots=candidate_slots,
                team1=team1,
                team2=team2,
                linked_list=linked_list,
                assigned_times=assigned_times,
                unavailable1=unavailable1,
                unavailable2=unavailable2,
                respect_linked=False,
                allow_violate_team1=False,  # still never violate Team 1 here
            )

            if not chosen:
                continue

            time_str = chosen['time']
            venue = chosen['venue']

            final_fixtures.append({
                'Team 1': team1,
                'Team 2': team2,
                'Grade': grade_text,
                'Venue': venue,
                'Day': day,
                'Time': time_str
            })
            assigned_times.setdefault(team1, []).append((day, time_str, venue))
            assigned_times.setdefault(team2, []).append((day, time_str, venue))
            _record_cluster_home_court(team1, team2, day, venue)
            chosen['occupied'] = True
            processed_indices.add(idx)

    # Stage 3: force-pass (same requests-first ordering, but can violate Team 1 as last resort)
    for day in days:
        slots_today = master_slots.get(day, [])
        if not slots_today:
            continue

        if not any(not s['occupied'] for s in slots_today):
            continue

        day_mask = pre_fixtures_round['Day'] == day
        day_rows = pre_fixtures_round[day_mask]

        req_indices = []
        no_req_indices = []
        for idx, _ in day_rows.iterrows():
            if idx in processed_indices:
                continue
            if fixture_has_unavailability(pre_fixtures_round, idx):
                req_indices.append(idx)
            else:
                no_req_indices.append(idx)

        leftovers_sorted = (
            sort_indices_for_day(req_indices, pre_fixtures_round, day, slots_today) +
            sort_indices_for_day(no_req_indices, pre_fixtures_round, day, slots_today)
        )

        if not leftovers_sorted:
            continue

        for idx in leftovers_sorted:
            if idx in processed_indices:
                continue

            fixture = pre_fixtures_round.loc[idx]
            team1 = fixture['Team 1']
            team2_raw = fixture['Team 2'] if pd.notna(fixture['Team 2']) else ''
            team2_str = str(team2_raw).strip().upper()
            team2 = None if team2_str in ('', '-', 'BYE') else fixture['Team 2']

            if team2 is None:
                continue

            division = fixture['Division']
            age = fixture['Age']
            gender = fixture['Gender']
            grade_text = fixture['Grade']

            strict_pref = requires_strict_preferred_venue(day, age, gender, division, grade_text)
            allowed_venues = get_lower_ring_allowed_venues(day, age, gender, division, grade_text)

            if is_open_men_grade(grade_text):
                allowed_venues = get_open_men_allowed_venues(grade_text)

            linked_list = get_linked_teams(team1) + get_linked_teams(team2)
            linked_list = list(dict.fromkeys(linked_list))

            unavailable1 = get_unavailable(team1)
            unavailable2 = get_unavailable(team2)

            free_slots = [s for s in slots_today if not s['occupied']]
            if not free_slots:
                continue

            if allowed_venues is not None:
                free_slots = [s for s in free_slots if s['venue'] in allowed_venues]

            if not free_slots:
                continue

            # Try preferred venues first for strict grades, but fall back if none
            if strict_pref:
                pref_slots = [
                    s for s in free_slots
                    if is_pref_venue_for_division(division, s['venue'])
                ]
                if pref_slots:
                    free_slots = pref_slots

            if not free_slots:
                continue

            # Men & U20 → core venues first even in force-pass (if any core slots left)
            if requires_men_core_bias(grade_text, division) or requires_u20_core_bias(grade_text):
                core_only = [s for s in free_slots if is_core_venue(s['venue'])]
                if core_only:
                    free_slots = core_only

            # Respect junior reservation even in force-pass: non-junior fixtures
            # skip slots reserved for U8 demand if alternatives exist. This
            # guarantees U8 force-pass (which runs first by age order) has its
            # reserved slots available. Fall through if no alternative exists.
            free_slots = _avoid_junior_reserved(free_slots, grade_text, day)
            free_slots = _avoid_open_men_reserved(free_slots, grade_text, day, team1, team2 if team2 is not None else '')

            min_start = min_start_minutes_for_age(grade_text, day)
            max_start = max_start_minutes_for_age(grade_text)

            within_window = [
                s for s in free_slots
                if min_start <= time_to_minutes(s['time']) <= max_start
            ]

            if is_senior_grade(grade_text):
                # Seniors/Open: try ideal 1:30–9pm window first
                if within_window:
                    candidate_slots = within_window
                else:
                    # Last resort: any slot from 1:30pm up to 9:00pm
                    extended = [
                        s for s in free_slots
                        if time_to_minutes(s['time']) >= min_start
                        and time_to_minutes(s['time']) <= 21 * 60  # 9:00pm
                    ]
                    candidate_slots = extended
            else:
                # Juniors: if no slot within window, relax max up to the
                # age-specific force-pass cap (e.g. U8 won't go past 1pm,
                # U10 won't go past 1pm, U12 past 4pm, etc.) — prevents
                # nonsensical placements like a U8 game at 5:15pm.
                if within_window:
                    candidate_slots = within_window
                else:
                    force_cap = force_pass_max_start_minutes_for_age(grade_text)
                    extended = [
                        s for s in free_slots
                        if min_start <= time_to_minutes(s['time']) <= force_cap
                    ]
                    if extended:
                        candidate_slots = extended
                    else:
                        # Every slot up to the force-cap is taken — leave the
                        # fixture unscheduled so the user sees it in the
                        # "Unscheduled fixtures" report with a clear reason,
                        # rather than silently dumping it at a silly time.
                        candidate_slots = []

            if not candidate_slots:
                continue

            candidate_slots_sorted = sorted(
                candidate_slots,
                key=lambda s: _slot_priority_base(
                    s, division, linked_list, assigned_times, day, grade_text,
                    team1=team1, team2=team2
                )
            )

            # In force-pass we ignore linked spacing but can now violate Team 1's request
            # as an absolute last resort.
            chosen = pick_slot_respecting_team1(
                day=day,
                candidate_slots=candidate_slots_sorted,
                team1=team1,
                team2=team2,
                linked_list=linked_list,
                assigned_times=assigned_times,
                unavailable1=unavailable1,
                unavailable2=unavailable2,
                respect_linked=False,
                allow_violate_team1=True,
            )

            if not chosen:
                continue

            time_str = chosen['time']
            venue = chosen['venue']

            final_fixtures.append({
                'Team 1': team1,
                'Team 2': team2,
                'Grade': grade_text,
                'Venue': venue,
                'Day': day,
                'Time': time_str
            })
            assigned_times.setdefault(team1, []).append((day, time_str, venue))
            assigned_times.setdefault(team2, []).append((day, time_str, venue))
            _record_cluster_home_court(team1, team2, day, venue)
            chosen['occupied'] = True
            processed_indices.add(idx)

    # Stage B: add BYEs for this round  (Team 2 shown as 'BYE' in output)
    for idx, fixture in bye_rows:
        team1 = fixture['Team 1']
        day = fixture['Day']

        final_fixtures.append({
            'Team 1': team1,
            'Team 2': 'BYE',
            'Grade': fixture['Grade'],
            'Venue': '',
            'Day': day,
            'Time': ''
        })
        processed_indices.add(idx)

    # Rebalance: move any junior fixtures that ended up after their normal max
    # start earlier by swapping with older-age fixtures where possible.
    rebalance_junior_lates(final_fixtures)

    # Re-pack pass: for each fixture that couldn't be placed, see if we can
    # swap it into a slot currently held by a less-constrained placed fixture.
    # The displaced fixture then gets re-placed in any other free legal slot.
    #
    # This rescues genuinely-blocked fixtures whose only legal slots were
    # claimed by fixtures that had alternatives. We only swap when the
    # displaced fixture has at least one legal alternative — otherwise the
    # swap would just create a new unscheduled fixture in place of the old.
    repack_unscheduled(
        pre_fixtures_round, processed_indices, final_fixtures, master_slots,
        assigned_times,
    )

    # Unscheduled for this round
    for idx, fixture in pre_fixtures_round.iterrows():
        if idx in processed_indices:
            continue

        team1 = fixture['Team 1']
        team2_raw = fixture['Team 2']

        if pd.isna(team2_raw):
            team2_raw = ''
        team2_str = str(team2_raw).strip().upper()
        team2 = None if team2_str in ('', '-', 'BYE') else fixture['Team 2']

        if team2 is None:
            continue

        grade = fixture['Grade']
        cap_min = force_pass_max_start_minutes_for_age(grade)
        cap_h = cap_min // 60
        cap_m = cap_min % 60
        suffix = 'pm' if cap_h >= 12 else 'am'
        disp_h = cap_h - 12 if cap_h > 12 else (12 if cap_h == 0 else cap_h)
        cap_str = f"{disp_h}:{cap_m:02d}{suffix}" if cap_m else f"{disp_h}{suffix}"

        reason = (
            f"Capacity shortfall: no valid slot found up to the force-pass cap "
            f"of {cap_str} for this age group. Try adding a timeslot/venue or "
            f"reducing fixtures on this day."
        )

        unscheduled.append({
            'Team 1': team1,
            'Team 2': team2,
            'Grade': grade,
            'Reason': reason
        })

    # Build DataFrames for this round
    scheduled_df_round = pd.DataFrame(final_fixtures)
    if not scheduled_df_round.empty:
        scheduled_df_round = scheduled_df_round.drop_duplicates(
            subset=['Team 1', 'Team 2', 'Grade', 'Day']
        )
        scheduled_df_round['Time_Sort'] = scheduled_df_round['Time'].apply(time_to_minutes)
        scheduled_df_round = scheduled_df_round.sort_values(
            ['Day', 'Time_Sort']
        ).drop(columns=['Time_Sort'])

    unscheduled_df_round = pd.DataFrame(unscheduled)

    # Logging for this round
    print(f"\n=== Capacity vs demand by day — Round {round_label} ===")
    for day in days:
        mask = (pre_fixtures_round['Day'] == day)
        non_bye = pre_fixtures_round[mask & pre_fixtures_round['Team 2'].notna()]
        non_bye = non_bye[
            ~non_bye['Team 2'].astype(str).str.strip().str.upper().isin(['', '-', 'BYE'])
        ]
        num_fixtures = len(non_bye)

        slots_today = master_slots.get(day, [])
        total_slots = len(slots_today)
        used_slots = sum(1 for s in slots_today if s['occupied'])

        print(f"{day}: fixtures={num_fixtures}, slots={total_slots}, used={used_slots}")

    if len(unscheduled_df_round):
        print(f"\n=== Unscheduled fixtures (day inferred from Grade) — Round {round_label} ===")
        for u in unscheduled:
            grade = u['Grade']
            day_token = str(grade).split()[0]
            print(f"{day_token}: {u['Team 1']} vs {u['Team 2']} ({grade})")

    return scheduled_df_round, unscheduled_df_round

# ---------------------------
# Run for all rounds
# ---------------------------

all_scheduled = []
all_unscheduled = []

round_values = sorted(pre_fixtures_df['Round'].dropna().unique())

for r in round_values:
    pre_fixtures_round = pre_fixtures_df[pre_fixtures_df['Round'] == r].copy().reset_index(drop=True)
    print(f"\n================ Round {r} =================")
    sched_r, unsched_r = schedule_round(pre_fixtures_round, r)

    if not sched_r.empty:
        sched_r['Round'] = r
        all_scheduled.append(sched_r)

    if not unsched_r.empty:
        unsched_r['Round'] = r
        all_unscheduled.append(unsched_r)

# Combine all rounds
if all_scheduled:
    scheduled_df = pd.concat(all_scheduled, ignore_index=True)
    # Final de-dup just in case
    scheduled_df = scheduled_df.drop_duplicates(
        subset=['Round', 'Team 1', 'Team 2', 'Grade', 'Day', 'Time']
    )
    scheduled_df['Time_Sort'] = scheduled_df['Time'].apply(time_to_minutes)
    scheduled_df = scheduled_df.sort_values(['Day', 'Round', 'Time_Sort']).drop(columns=['Time_Sort'])
else:
    scheduled_df = pd.DataFrame(columns=['Team 1', 'Team 2', 'Grade', 'Venue', 'Playing Surface', 'Day', 'Time', 'Round'])

if all_unscheduled:
    unscheduled_df = pd.concat(all_unscheduled, ignore_index=True)
else:
    unscheduled_df = pd.DataFrame(columns=['Team 1', 'Team 2', 'Grade', 'Reason', 'Round'])

# Split the composite 'Venue' back into 'Venue' + 'Playing Surface' so the
# output CSV shows them as two separate columns (matches the input structure).
def _split_composite_venue(composite: str) -> tuple[str, str]:
    if composite is None:
        return ('', '')
    s = str(composite).strip()
    if not s:
        return ('', '')
    # Prefer the lookup populated during _merge_venue_surface — handles Monash's
    # non-standard surfaces (Stadium Court 1, Games Hall Court 1, etc.) and any
    # future naming that doesn't fit a simple " - Court" regex.
    if s in VENUE_SURFACE_LOOKUP:
        return VENUE_SURFACE_LOOKUP[s]
    # Fallback: split on the first " - Court" so legacy composites still work.
    m = re.search(r'\s-\sCourt', s)
    if m:
        idx = m.start()
        return (s[:idx].strip(), s[idx + 3:].strip())
    return (s, '')

if not scheduled_df.empty:
    split_pairs = scheduled_df['Venue'].apply(_split_composite_venue)
    scheduled_df['Venue'] = [p[0] for p in split_pairs]
    scheduled_df['Playing Surface'] = [p[1] for p in split_pairs]
    # Reorder columns: Venue and Playing Surface sit together, right after Grade
    scheduled_df = scheduled_df[
        ['Team 1', 'Team 2', 'Grade', 'Venue', 'Playing Surface', 'Day', 'Time', 'Round']
    ]

# Output paths
scheduled_path = os.path.join(BASE, 'scheduled_fixtures.csv')
unscheduled_path = os.path.join(BASE, 'unscheduled_fixtures.csv')

def _build_template_output(sched: pd.DataFrame) -> pd.DataFrame:
    """
    Produce the PlayHQ-style upload template from the internal scheduled_df.

    The scheduler fills in: grade, round, home team, away team, venue,
    playing surface, game time. Passthrough columns (organisation,
    competition, season, round date, game date, game alias) are carried
    through from the input template per fixture, or left blank if absent.

    Column order matches the upload template exactly:
      organisation, competition, season, grade, round date, round,
      home team, away team, venue, playing surface, game date,
      game time, game alias
    """
    rows = []
    for _, r in sched.iterrows():
        key = (
            str(r.get('Round', '')).strip(),
            str(r.get('Team 1', '')).strip(),
            str(r.get('Team 2', '')).strip(),
            str(r.get('Grade', '')).strip(),
        )
        meta = PREFIX_TEMPLATE_PASSTHROUGH.get(key, {})
        rows.append({
            'organisation': meta.get('organisation', ''),
            'competition': meta.get('competition', ''),
            'season': meta.get('season', ''),
            'grade': r.get('Grade', ''),
            'round date': meta.get('round date', ''),
            'round': r.get('Round', ''),
            'home team': r.get('Team 1', ''),
            'away team': r.get('Team 2', ''),
            'venue': r.get('Venue', ''),
            'playing surface': r.get('Playing Surface', ''),
            'game date': meta.get('game date', ''),
            'game time': format_game_time(r.get('Time', '')),
            'game alias': meta.get('game alias', ''),
        })
    return pd.DataFrame(rows, columns=PREFIX_TEMPLATE_COLUMNS)

if INPUT_WAS_TEMPLATE:
    # Write the scheduled fixtures back in the upload-template format.
    template_out = _build_template_output(scheduled_df)
    template_out.to_csv(scheduled_path, index=False)

    # Unscheduled also in template format (venue/surface/time left blank),
    # plus a trailing 'reason' column so the cause is visible.
    if not unscheduled_df.empty:
        unsched_rows = []
        for _, r in unscheduled_df.iterrows():
            key = (
                str(r.get('Round', '')).strip(),
                str(r.get('Team 1', '')).strip(),
                str(r.get('Team 2', '')).strip(),
                str(r.get('Grade', '')).strip(),
            )
            meta = PREFIX_TEMPLATE_PASSTHROUGH.get(key, {})
            unsched_rows.append({
                'organisation': meta.get('organisation', ''),
                'competition': meta.get('competition', ''),
                'season': meta.get('season', ''),
                'grade': r.get('Grade', ''),
                'round date': meta.get('round date', ''),
                'round': r.get('Round', ''),
                'home team': r.get('Team 1', ''),
                'away team': r.get('Team 2', ''),
                'venue': '',
                'playing surface': '',
                'game date': meta.get('game date', ''),
                'game time': '',
                'game alias': meta.get('game alias', ''),
                'reason': r.get('Reason', ''),
            })
        pd.DataFrame(
            unsched_rows,
            columns=PREFIX_TEMPLATE_COLUMNS + ['reason'],
        ).to_csv(unscheduled_path, index=False)
    else:
        pd.DataFrame(
            columns=PREFIX_TEMPLATE_COLUMNS + ['reason']
        ).to_csv(unscheduled_path, index=False)
else:
    # Legacy output format (unchanged).
    scheduled_df.to_csv(scheduled_path, index=False)
    unscheduled_df.to_csv(unscheduled_path, index=False)

print(f"\nDone. Scheduled {len(scheduled_df)} fixtures in total across {len(round_values)} round(s).")
if len(unscheduled_df):
    print(f"Could not schedule {len(unscheduled_df)} fixtures - see {unscheduled_path}")

# -----------------------------------------------------------------
# Post-run diagnostics
# -----------------------------------------------------------------

def _report_unavailable_violations(sched: pd.DataFrame):
    """
    Scan the final schedule and flag any fixture placed at a time that falls
    inside a team's Unavailable_Times. Team 1 violations are the important
    ones (scheduler treats Team 1 as the priority) — Team 2 violations are
    listed separately as "couldn't honour both teams".
    """
    if sched.empty:
        return

    team1_violations = []
    team2_violations = []

    for _, row in sched.iterrows():
        t = str(row.get('Time', '')).strip()
        if not t:
            continue

        t1 = row.get('Team 1', '')
        t2 = row.get('Team 2', '')

        u1 = get_unavailable(t1)
        if pd.notna(u1) and str(u1).strip() and is_time_blocked(t, u1):
            team1_violations.append({
                'team': t1, 'role': 'Team 1', 'request': str(u1).strip(),
                'time': t, 'day': row.get('Day', ''),
                'grade': row.get('Grade', ''),
                'opponent': t2,
            })

        if t2 and str(t2).strip().upper() != 'BYE':
            u2 = get_unavailable(t2)
            if pd.notna(u2) and str(u2).strip() and is_time_blocked(t, u2):
                team2_violations.append({
                    'team': t2, 'role': 'Team 2', 'request': str(u2).strip(),
                    'time': t, 'day': row.get('Day', ''),
                    'grade': row.get('Grade', ''),
                    'opponent': t1,
                })

    if team1_violations:
        print(f"\n=== Team 1 request violations ({len(team1_violations)}) ===")
        print("These fixtures had Team 1 placed INSIDE their Unavailable_Times window.")
        print("Usually means every slot outside that window was already taken.")
        for v in team1_violations:
            print(f"  [{v['day']}] {v['team']} unavailable '{v['request']}' → placed at {v['time']}")
            print(f"      vs {v['opponent']} ({v['grade']})")

    if team2_violations:
        print(f"\n=== Team 2 request violations ({len(team2_violations)}) ===")
        print("Team 1's request took priority so Team 2's could not be honoured.")
        for v in team2_violations:
            print(f"  [{v['day']}] {v['team']} unavailable '{v['request']}' → placed at {v['time']}")
            print(f"      vs {v['opponent']} ({v['grade']})")

    if not team1_violations and not team2_violations:
        print("\n=== All Unavailable_Times requests honoured ===")


def _report_linked_back_to_back(sched: pd.DataFrame):
    """
    Summary of how well linked teams are clustered in the final schedule.
    Reports back-to-back rate (same venue, ≤90min apart) and the share of
    linked pairs sent to different venues on the same day.
    """
    if sched.empty:
        return

    # Collect linked relationships
    linked_pairs = set()
    for _, row in teams_df.iterrows():
        t = row['Team']
        for col in LINKED_COLS:
            v = row.get(col, None)
            if pd.notna(v) and str(v).strip():
                linked_pairs.add(tuple(sorted([t, str(v).strip()])))

    if not linked_pairs:
        return

    # Collect fixtures by team
    fixtures_by_team = defaultdict(list)
    for _, row in sched.iterrows():
        if pd.isna(row.get('Time')) or not str(row['Time']).strip():
            continue
        venue_full = (
            f"{row.get('Venue', '')} - {row.get('Playing Surface', '')}"
            if row.get('Playing Surface', '')
            else row.get('Venue', '')
        )
        for col in ('Team 1', 'Team 2'):
            team = row.get(col, '')
            if not team or str(team).strip().upper() == 'BYE':
                continue
            fixtures_by_team[team].append({
                'day': row['Day'],
                'time_min': time_to_minutes(row['Time']),
                'venue_full': venue_full,
            })

    b2b = 0
    same_venue_not_b2b = 0
    different_venue = 0
    total_same_day = 0

    for a, b in linked_pairs:
        for ga in fixtures_by_team.get(a, []):
            for gb in fixtures_by_team.get(b, []):
                if ga['day'] != gb['day']:
                    continue
                total_same_day += 1
                same_venue = (ga['venue_full'] == gb['venue_full'])
                gap = abs(ga['time_min'] - gb['time_min'])
                if same_venue and gap <= 90:
                    b2b += 1
                elif same_venue:
                    same_venue_not_b2b += 1
                else:
                    different_venue += 1

    if total_same_day == 0:
        return

    print(f"\n=== Linked-team clustering ===")
    print(f"  Linked pairs playing same day:    {total_same_day}")
    print(f"  Back-to-back (same venue, ≤90m):  {b2b} ({b2b/total_same_day*100:.1f}%)")
    print(f"  Same venue, NOT back-to-back:     {same_venue_not_b2b}")
    print(f"  Different venues:                 {different_venue}")


_report_unavailable_violations(scheduled_df)
_report_linked_back_to_back(scheduled_df)



# =========================================================
# TRUE LAST RESORT FORCE ASSIGNMENT (GUARANTEED PLACEMENT)
# ---------------------------------------------------------
# Call this inside your force-pass BEFORE appending
# to unscheduled list.
# =========================================================

def true_last_resort_assignment(
    fixture,
    day,
    free_slots_by_day,
    is_timeslot_taken,
    venue_allowed_by_lower_rings,
    is_open_men_grade,
    OPEN_MEN_ALLOWED_VENUES,
    team_is_already_playing,
    assign_fixture_to_slot
):
    '''
    This function ignores:
        - Age windows
        - Preferred venues
        - Travel spacing
        - Clustering
        - Fixture requests
    It ONLY enforces:
        - No double booking
        - Hard venue rules
    '''

    grade = fixture['Grade']
    team1 = fixture['Team 1']
    team2 = fixture['Team 2']

    all_remaining_slots = [
        s for s in free_slots_by_day.get(day, [])
        if not is_timeslot_taken(s)
    ]

    for slot in sorted(all_remaining_slots, key=lambda x: x['Start']):

        # Hard lower-rings rule
        if not venue_allowed_by_lower_rings(grade, slot['Venue']):
            continue

        # Open Men restriction
        if is_open_men_grade(grade):
            if slot['Venue'] not in OPEN_MEN_ALLOWED_VENUES:
                continue

        # Prevent double booking
        if team_is_already_playing(team1, slot):
            continue
        if team_is_already_playing(team2, slot):
            continue

        assign_fixture_to_slot(fixture, slot)
        return True

    return False

# =========================================================
# END TRUE LAST RESORT BLOCK
# =========================================================