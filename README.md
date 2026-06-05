# MBA Fixture Scheduler — web app

A single site that combines the whole fixturing process: edit your config,
load the round's matchups, run the scheduler, review a plain-English summary,
and download the PlayHQ upload file.

It does **not** change the scheduling engine. The site drives the existing
`fixture_requests_vW26.py` and reuses the checks/summary in `run_fixtures.py`,
so the result is identical to running from the command line.

## Files

| File | Role |
|------|------|
| `app.py` | The web app (Streamlit UI). |
| `run_fixtures.py` | Backend: input checks + plain-English summary. Also works on its own as a one-command CLI. |
| `fixture_requests_vW26.py` | The scheduling engine. Unchanged. |
| `mckinnon_logo.png` | Club logo shown in the app header. |
| `.streamlit/config.toml` | App theme (McKinnon maroon / gold / navy). Keep it in a `.streamlit` folder next to `app.py`. |
| `teams.csv`, `timeslots.csv`, `division_venues.csv` | Your stable config. |
| `pre_fixtures.csv` | The round's matchups (home vs away, no venue/time yet). |

All files live in one folder. The app reads and writes the CSVs in that folder,
so you can still use the command line whenever you prefer.

## Run it locally (one time setup)

1. Install Python 3.10+.
2. In this folder:
   ```
   pip install -r requirements.txt
   ```

## Each time you fixture a round

```
streamlit run app.py
```

Your browser opens automatically. Then:

1. **Inputs tab** — edit Teams / Timeslots / Division-Venues and Save, then
   upload the round's `pre_fixtures.csv`.
2. Press **Generate fixtures** in the sidebar.
3. **Run tab** shows the input checks, then the summary.
4. **Results tab** — filter by round/venue/team, and download
   `scheduled_fixtures.csv` to upload to PlayHQ.

## Prefer the command line?

```
python run_fixtures.py
```

Same checks, same scheduling, same summary — just no browser.

## Sharing it with the team (optional)

Because it's a normal Streamlit app you can host it instead of running locally:

- **Streamlit Community Cloud** — push this folder to a GitHub repo and deploy
  `app.py`. Free for a single app; good for an internal tool.
- **A small server** — run `streamlit run app.py --server.port 80` on any
  machine the team can reach.

Hosting only changes *where* it runs; the workflow above is the same.

## Notes

- `Unavailable_Times` syntax: `<11 am` means no games before 11am, `>9:45 am`
  means no games after 9:45am. Separate multiple with `;`.
- Leave a court's `Time_Slots` blank in `timeslots.csv` to skip it.
- **Per-round timeslots:** in the app's Timeslots tab, the "Editing slots for"
  dropdown lets you change a single round (a holiday week, finals, or a closed
  court) without touching the rest. Behind the scenes this adds an optional
  `Round` column to `timeslots.csv`: blank-round rows are the default for every
  round, and a row with a round number overrides just that round for that court.
  Set a round override's slots to empty to close a court for that week only.
- The input checks will warn you about the usual mistakes (venue names that
  don't match between files, teams missing from `teams.csv`, bad time syntax)
  before scheduling, so you don't discover them in the output.
