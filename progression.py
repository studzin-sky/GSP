#!/usr/bin/env python3
"""GSP - Garmin Strength Progression.

Update Garmin Connect strength workout templates with your latest results.

For every strength workout template in your Garmin Connect library, find the
most recent strength activity you did from it, and copy the reps/weight you
actually performed back into the template (Strong-app style).

Uses the unofficial Garmin Connect API via the `garminconnect` library.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from garminconnect import Garmin

ROOT = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real env vars take precedence."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env(ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


TOKENSTORE = _path(os.environ.get("GSP_TOKENSTORE") or ".secrets/garminconnect")
BACKUP_DIR = _path(os.environ.get("GSP_BACKUP_DIR") or "backups")
DEFAULT_DAYS = int(os.environ.get("GSP_DAYS") or 7)
DEVICE_ID = os.environ.get("GSP_DEVICE_ID") or None  # default: last used device
SUPPORTED_TYPES = {"strength_training", "hiit"}

# exerciseSets report weight in grams
GRAMS_PER_UNIT = {"kilogram": 1000.0, "pound": 453.59237}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def connect(interactive: bool = False) -> Garmin:
    TOKENSTORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    email = os.environ.get("GARMIN_EMAIL") or None
    password = os.environ.get("GARMIN_PASSWORD") or None
    mfa = lambda: input("MFA code: ").strip()  # noqa: E731

    if not interactive:
        try:
            # with email/password in .env, expired tokens trigger a fresh login
            client = Garmin(email, password, prompt_mfa=mfa)
            client.login(str(TOKENSTORE))
            return client
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Not logged in ({e}).\nRun:  ./update.sh login")

    email = email or input("Garmin email: ").strip()
    password = password or getpass.getpass("Garmin password: ")
    client = Garmin(email, password, prompt_mfa=mfa)
    client.login(str(TOKENSTORE))
    print(f"Logged in. Tokens saved to {TOKENSTORE}")
    return client


# --------------------------------------------------------------------------- #
# Workout template helpers
# --------------------------------------------------------------------------- #
def iter_slots(steps: list[dict], iterations: int = 1):
    """Yield (step, sets_count) for every executable exercise step, in order.

    A step inside a repeat group of N iterations represents N sets.
    """
    for step in sorted(steps or [], key=lambda s: s.get("stepOrder", 0)):
        if step.get("type") == "RepeatGroupDTO" or step.get("workoutSteps"):
            n = int(step.get("numberOfIterations") or 1)
            yield from iter_slots(step.get("workoutSteps"), iterations * n)
            continue
        step_type = (step.get("stepType") or {}).get("stepTypeKey")
        if step_type in ("rest", "recovery"):
            continue
        if not (step.get("category") or step.get("exerciseName")):
            continue
        yield step, iterations


def step_key(step: dict) -> tuple[str | None, str | None]:
    return step.get("category"), step.get("exerciseName")


def set_key(s: dict) -> tuple[str | None, str | None]:
    ex = (s.get("exercises") or [{}])
    # pick the highest-probability exercise guess
    ex = max(ex, key=lambda e: e.get("probability") or 0) if ex else {}
    return ex.get("category"), ex.get("name")


def same_exercise(a: tuple, b: tuple) -> bool:
    (cat_a, name_a), (cat_b, name_b) = a, b
    if name_a and name_b:
        return name_a == name_b
    return bool(cat_a) and cat_a == cat_b


def fmt_ex(key: tuple) -> str:
    cat, name = key
    return (name or cat or "?").replace("_", " ").title()


def step_weight_unit(step: dict) -> str:
    return ((step.get("weightUnit") or {}).get("unitKey")) or "kilogram"


def is_reps_step(step: dict) -> bool:
    return ((step.get("endCondition") or {}).get("conditionTypeKey")) == "reps"


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def top_set(sets: list[dict]) -> dict:
    """Heaviest set; ties broken by most reps."""
    return max(sets, key=lambda s: ((s.get("weight") or 0), (s.get("repetitionCount") or 0)))


def plan_update_detailed(
    workout: dict, exercise_sets: list[dict]
) -> tuple[dict, list[str], dict[str, Any]]:
    """Return (updated_workout, list_of_change_descriptions, structured_details)."""
    new = copy.deepcopy(workout)
    active = [s for s in exercise_sets if s.get("setType") == "ACTIVE"]
    active.sort(key=lambda s: s.get("startTime") or "")
    used = [False] * len(active)
    changes: list[str] = []
    step_details: list[dict[str, Any]] = []

    slot_index = 0
    for seg in new.get("workoutSegments") or []:
        for step, n_sets in iter_slots(seg.get("workoutSteps")):
            key = step_key(step)
            unit = step_weight_unit(step)
            unit_label = "lb" if unit == "pound" else "kg"
            old_w = step.get("weightValue")
            old_r = step.get("endConditionValue")
            cond_key = ((step.get("endCondition") or {}).get("conditionTypeKey")) or ""
            reps_mode = is_reps_step(step)

            # take the next n_sets unused sets of this exercise, in order
            chunk = []
            for i, s in enumerate(active):
                if not used[i] and same_exercise(key, set_key(s)):
                    used[i] = True
                    chunk.append(s)
                    if len(chunk) == n_sets:
                        break

            if not chunk:
                changes.append(f"  {fmt_ex(key)}: no sets found in activity, unchanged")
                step_details.append({
                    "slotIndex": slot_index,
                    "stepId": step.get("stepId"),
                    "exercise": fmt_ex(key),
                    "category": key[0],
                    "exerciseName": key[1],
                    "setsCount": n_sets,
                    "setsDone": [],
                    "setsDoneText": "",
                    "matched": False,
                    "changed": False,
                    "weightChanged": False,
                    "repsChanged": False,
                    "oldWeight": float(old_w) if old_w is not None else None,
                    "newWeight": float(old_w) if old_w is not None else None,
                    "unit": unit,
                    "unitLabel": unit_label,
                    "isRepsStep": reps_mode,
                    "endConditionKey": cond_key,
                    "oldReps": int(old_r) if (reps_mode and old_r is not None) else None,
                    "newReps": int(old_r) if (reps_mode and old_r is not None) else None,
                    "durationSecs": int(old_r) if (cond_key == "time" and old_r is not None) else None,
                })
                slot_index += 1
                continue

            best = top_set(chunk)
            parts = []
            weight_changed = False
            reps_changed = False
            final_w = float(old_w) if old_w is not None else None
            final_r = int(old_r) if (reps_mode and old_r is not None) else None

            grams = best.get("weight")
            if grams:  # keep template weight for bodyweight / unlogged sets
                new_w = round(grams / GRAMS_PER_UNIT.get(unit, 1000.0) * 4) / 4
                final_w = new_w
                if old_w is None or abs(float(old_w) - new_w) > 1e-6:
                    step["weightValue"] = new_w
                    if not step.get("weightUnit"):
                        step["weightUnit"] = {"unitKey": unit}
                    parts.append(f"weight {old_w} -> {new_w} {unit_label}")
                    weight_changed = True

            reps = best.get("repetitionCount")
            if reps and reps_mode:
                final_r = int(reps)
                if old_r is None or int(old_r) != int(reps):
                    step["endConditionValue"] = float(reps)
                    parts.append(f"reps {int(old_r) if old_r else old_r} -> {reps}")
                    reps_changed = True

            sets_done_list = [
                f"{s.get('repetitionCount')}x{(s.get('weight') or 0) / 1000:g}kg" for s in chunk
            ]
            done = ", ".join(sets_done_list)
            label = f"  {fmt_ex(key)} [{done}]"
            changes.append(f"{label}: " + ("; ".join(parts) if parts else "unchanged"))

            step_details.append({
                "slotIndex": slot_index,
                "stepId": step.get("stepId"),
                "exercise": fmt_ex(key),
                "category": key[0],
                "exerciseName": key[1],
                "setsCount": n_sets,
                "setsDone": sets_done_list,
                "setsDoneText": done,
                "matched": True,
                "changed": bool(weight_changed or reps_changed),
                "weightChanged": weight_changed,
                "repsChanged": reps_changed,
                "oldWeight": float(old_w) if old_w is not None else None,
                "newWeight": final_w,
                "unit": unit,
                "unitLabel": unit_label,
                "isRepsStep": reps_mode,
                "endConditionKey": cond_key,
                "oldReps": int(old_r) if (reps_mode and old_r is not None) else None,
                "newReps": final_r,
                "durationSecs": int(old_r) if (cond_key == "time" and old_r is not None) else None,
            })
            slot_index += 1

    leftover = [s for i, s in enumerate(active) if not used[i]]
    extra_names = sorted({fmt_ex(set_key(s)) for s in leftover}) if leftover else []
    if leftover:
        changes.append(f"  (ignored {len(leftover)} extra set(s): {', '.join(extra_names)})")

    details = {
        "steps": step_details,
        "extraSetsCount": len(leftover),
        "extraExercises": extra_names,
        "hasChanges": new != workout,
    }
    return new, changes, details


def plan_update(workout: dict, exercise_sets: list[dict]) -> tuple[dict, list[str]]:
    """Return (updated_workout, list_of_change_descriptions)."""
    new, changes, _ = plan_update_detailed(workout, exercise_sets)
    return new, changes


def apply_step_overrides(workout: dict, step_overrides: list[dict[str, Any]]) -> dict:
    """Return a copy of workout with user-specified weight/reps per slotIndex applied."""
    new = copy.deepcopy(workout)
    by_slot = {int(item["slotIndex"]): item for item in (step_overrides or []) if "slotIndex" in item}
    slot_index = 0
    for seg in new.get("workoutSegments") or []:
        for step, _n_sets in iter_slots(seg.get("workoutSteps")):
            override = by_slot.get(slot_index)
            if override is not None:
                unit = step_weight_unit(step)
                w_val = override.get("weightValue")
                if w_val is not None and w_val != "":
                    new_w = round(float(w_val) * 4) / 4
                    old_w = step.get("weightValue")
                    if old_w is None or abs(float(old_w) - new_w) > 1e-6:
                        step["weightValue"] = new_w
                        if not step.get("weightUnit"):
                            step["weightUnit"] = {"unitKey": unit}
                r_val = override.get("repsValue")
                if r_val is not None and r_val != "" and is_reps_step(step):
                    new_r = float(int(float(r_val)))
                    old_r = step.get("endConditionValue")
                    if old_r is None or int(old_r) != int(new_r):
                        step["endConditionValue"] = new_r
            slot_index += 1
    return new


def activity_workout_id(activity: dict) -> int | None:
    for k in ("workoutId", "associatedWorkoutId"):
        if activity.get(k):
            return int(activity[k])
    return None


def find_latest_activity(workout: dict, activities: list[dict]) -> dict | None:
    wid, wname = workout["workoutId"], (workout.get("workoutName") or "").strip().lower()
    for a in activities:  # newest first
        if activity_workout_id(a) == wid:
            return a
    for a in activities:
        if (a.get("activityName") or "").strip().lower() == wname:
            return a
    return None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_update(args: argparse.Namespace) -> None:
    client = connect()
    start = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    # strength_training and hiit are sub-types of fitness_equipment in the activity search
    activities = [
        a for a in client.get_activities_by_date(start, activitytype="fitness_equipment")
        if ((a.get("activityType") or {}).get("typeKey")) in SUPPORTED_TYPES
    ]
    activities.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    print(f"{len(activities)} strength/HIIT activities since {start}")

    workouts = [
        w for w in client.get_workouts(limit=200)
        if ((w.get("sportType") or {}).get("sportTypeKey")) in SUPPORTED_TYPES
    ]
    if args.workout:
        wanted = {n.lower() for n in args.workout}
        workouts = [w for w in workouts if (w.get("workoutName") or "").lower() in wanted]
    if not workouts:
        sys.exit("No strength workout templates found.")

    pending = []
    for summary in workouts:
        name = summary.get("workoutName")
        act = find_latest_activity(summary, activities)
        if not act:
            print(f"\n== {name}: no activity in the last {args.days} days, skipping")
            continue
        workout = client.get_workout_by_id(summary["workoutId"])
        sets = (client.get_activity_exercise_sets(act["activityId"]) or {}).get("exerciseSets") or []
        new, changes = plan_update(workout, sets)
        print(f"\n== {name}  <-  {act.get('activityName')} ({act.get('startTimeLocal')})")
        print("\n".join(changes))
        if new != workout:
            pending.append((workout, new))

    if not pending:
        print("\nNothing to update.")
        return
    if args.dry_run:
        print(f"\nDry run: {len(pending)} template(s) would be updated.")
        return
    if not args.yes and input(f"\nUpdate {len(pending)} template(s)? [y/N] ").lower() != "y":
        print("Aborted.")
        return

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for old, new in pending:
        safe = "".join(c if c.isalnum() else "_" for c in old.get("workoutName") or "workout")
        (BACKUP_DIR / f"{stamp}-{safe}.json").write_text(json.dumps(old, indent=2))
        client.update_workout(old["workoutId"], new)
        print(f"Updated {old.get('workoutName')}")
        if args.push:
            res = client.push_workout_to_device(old["workoutId"], DEVICE_ID)
            print("  queued for watch on next sync")
            if args.verbose:
                print(f"  {res}")
    print(f"Backups in {BACKUP_DIR}")


def cmd_push(args: argparse.Namespace) -> None:
    client = connect()
    wanted = {n.lower() for n in args.workout}
    for w in client.get_workouts(limit=200):
        if ((w.get("sportType") or {}).get("sportTypeKey")) not in SUPPORTED_TYPES:
            continue
        if wanted and (w.get("workoutName") or "").lower() not in wanted:
            continue
        client.push_workout_to_device(w["workoutId"], DEVICE_ID)
        print(f"Queued {w.get('workoutName')} for watch on next sync")


def cmd_restore(args: argparse.Namespace) -> None:
    client = connect()
    old = json.loads(Path(args.file).read_text())
    client.update_workout(old["workoutId"], old)
    print(f"Restored {old.get('workoutName')} from {args.file}")


def cmd_dump(args: argparse.Namespace) -> None:
    """Print raw JSON to help debug matching."""
    client = connect()
    if args.what == "workouts":
        data = [
            client.get_workout_by_id(w["workoutId"]) for w in client.get_workouts(limit=200)
            if ((w.get("sportType") or {}).get("sportTypeKey")) in SUPPORTED_TYPES
        ]
    elif args.what == "activities":
        data = [
            a for a in client.get_activities(0, 30, activitytype="fitness_equipment")
            if ((a.get("activityType") or {}).get("typeKey")) in SUPPORTED_TYPES
        ]
    elif args.what == "devices":
        data = [
            {k: d.get(k) for k in ("deviceId", "displayName", "productDisplayName", "deviceStatus")}
            for d in client.get_devices()
        ]
    else:
        data = client.get_activity_exercise_sets(args.what)
    print(json.dumps(data, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("login", help="log in once (stores tokens in ~/.garminconnect)")

    u = sub.add_parser("update", help="update templates from latest activities (default)")
    u.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"look back N days (default {DEFAULT_DAYS})")
    u.add_argument("-n", "--dry-run", action="store_true", help="show changes only")
    u.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    u.add_argument("-w", "--workout", action="append", help="only this template (repeatable)")
    u.add_argument("-v", "--verbose", action="store_true", help="print API responses")
    u.add_argument("--no-push", dest="push", action="store_false",
                   help="don't send updated workouts to the watch")

    r = sub.add_parser("restore", help="restore a template from a backup file")
    r.add_argument("file")

    pu = sub.add_parser("push", help="queue strength workouts for the watch")
    pu.add_argument("workout", nargs="*", help="workout names (default: all strength)")

    d = sub.add_parser("dump", help="print raw JSON: workouts | activities | devices | <activityId>")
    d.add_argument("what")

    ui = sub.add_parser("ui", help="start the local web UI")
    ui.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ui.add_argument("--port", type=int, default=8765, help="bind port (default 8765)")
    ui.add_argument("--no-open", action="store_true", help="don't auto-open browser")

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["update", *argv]
    args = p.parse_args(argv)

    if args.cmd == "login":
        connect(interactive=True)
    elif args.cmd == "update":
        cmd_update(args)
    elif args.cmd == "restore":
        cmd_restore(args)
    elif args.cmd == "push":
        cmd_push(args)
    elif args.cmd == "dump":
        cmd_dump(args)
    elif args.cmd == "ui":
        from ui import run_ui_server
        run_ui_server(host=args.host, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
