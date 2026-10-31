# GSP: Garmin Strength Progression

Strong-app-style progression for Garmin Connect strength workouts: after you
train from a template (Push / Pull / Legs) with the watch's native Strength
app, GSP copies the reps and weight you actually did back into the template
and sends the updated workout to your watch.

## Setup

```bash
git clone https://github.com/studzin-sky/GSP.git && cd GSP
cp .env.example .env     # optional settings, gitignored
./update.sh login        # once; tokens go to .secrets/ (gitignored, valid ~1 year)
```

`update.sh` creates the Python virtualenv on first run.

## Usage

```bash
./update.sh -n        # dry run: show what would change
./update.sh           # show changes, confirm, update + queue for watch
```

Options: `--days N` (lookback, default 7), `-w "Push Gemini"` (single template),
`-y` (no prompt), `--no-push` (don't queue updated workouts for the watch).

- Push to watch without changes: `./update.sh push ["Push Gemini" ...]`
- Restore: `./update.sh restore backups/<file>.json`
- Debug: `./update.sh dump workouts|activities|devices|<activityId>`

Updated workouts are queued to your watch because Garmin doesn't refresh
workouts already on the device when a template is edited. They arrive on
the next phone/watch sync.

## Configuration

All optional, in `.env` (see [.env.example](.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `GSP_TOKENSTORE` | `.secrets/garminconnect` | Garmin login tokens |
| `GSP_BACKUP_DIR` | `backups` | Template backups before each update |
| `GSP_DAYS` | `7` | Activity lookback |
| `GSP_DEVICE_ID` | last used device | Watch to send workouts to |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | empty | Auto re-login when tokens expire |

Gitignored: `.env`, `.secrets/`, `backups/`, `.venv/`.

## How it works

1. Finds strength activities from the last N days.
2. Matches each template to its newest activity (by workout id, else by name).
3. Walks the template's exercise steps in order and assigns the activity's
   sets of the same exercise to them. A step inside a repeat group of N
   iterations takes N sets; since one step holds a single reps/weight value,
   it gets your **top set** (heaviest, then most reps).
4. Only weight and reps are changed; set count, rest and order are kept.
   Sets without weight (bodyweight) leave the template weight alone.
   Template exercises with no matching sets (e.g. you did Plank instead of
   Crunch) are left unchanged, and unmatched sets are ignored.

Exercises are matched by Garmin exercise name, so if the watch logged a set
as a different exercise, fix it in Garmin Connect before running.

Uses the unofficial Garmin Connect API (`garminconnect`), which can break.
