
import os
import re
import sqlite3
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, render_template
from dotenv import load_dotenv

load_dotenv()

# ================= CONFIG =================

API_URL = os.getenv(
    "API_URL",
    "https://draw.ar-lottery01.com/TrxWinGo/TrxWinGo_1M/GetHistoryIssuePage.json"
)

LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "").strip()
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "CHANGE-THIS-TO-A-LONG-RANDOM-SECRET"
)
DB_PATH = os.getenv("DB_PATH", "wingo.sqlite3")

app = Flask(__name__)
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)


# ================= DATABASE =================

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS prediction_history (
                target_issue TEXT PRIMARY KEY,
                predicted_side TEXT NOT NULL,
                score INTEGER,
                basis TEXT,
                created_at TEXT NOT NULL,
                result_number INTEGER,
                actual_side TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING'
            )
        """)


# IMPORTANT: Also runs when Gunicorn imports this file.
init_db()


def utcnow():
    return datetime.now(timezone.utc)


# ================= API DATA PARSING =================

ISSUE_KEYS = (
    "issueNumber",
    "issue",
    "period",
    "issue_no",
    "drawIssue",
    "expect",
    "periodNumber"
)

NUMBER_KEYS = (
    "number",
    "openNumber",
    "result",
    "lotteryNumber",
    "openCode",
    "winningNumber",
    "drawResult"
)


def find_row_lists(value):
    """
    Search nested JSON objects for lists of result rows.
    """
    found = []

    if isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value):
            found.append(value)

        for item in value:
            found.extend(find_row_lists(item))

    elif isinstance(value, dict):
        for child in value.values():
            found.extend(find_row_lists(child))

    return found


def parse_rows(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    candidates = find_row_lists(payload)

    # Prefer the largest list of objects.
    candidates.sort(key=len, reverse=True)

    for rows in candidates:
        if rows and any(
            any(k in row for k in ISSUE_KEYS)
            and any(k in row for k in NUMBER_KEYS)
            for row in rows
        ):
            return rows

    return []


def number_of(row):
    raw = None

    for key in NUMBER_KEYS:
        if row.get(key) is not None:
            raw = row.get(key)
            break

    if raw is None:
        return None

    # Do not accidentally read the issue number as the result.
    text = str(raw).strip()

    match = re.fullmatch(r"\d", text)

    if not match:
        # Some APIs return a result string with separators.
        match = re.match(r"^\s*(\d)(?:[,| ]|$)", text)

    if not match:
        return None

    try:
        number = int(match.group(1) if match.lastindex else match.group())
    except (ValueError, TypeError):
        return None

    return number if 0 <= number <= 9 else None


def issue_of(row):
    for key in ISSUE_KEYS:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def fetch_history():
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
        ),
        "Referer": "https://draw.ar-lottery01.com/"
    }

    response = requests.get(
        API_URL,
        headers=headers,
        timeout=20
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        raise ValueError(
            "API did not return JSON. "
            f"Content-Type: {response.headers.get('Content-Type', 'unknown')}"
        )

    rows = parse_rows(payload)

    if not rows:
        if isinstance(payload, dict):
            keys = ", ".join(list(payload.keys())[:15])
        else:
            keys = type(payload).__name__

        raise ValueError(
            f"Could not find result rows in API response. Top-level: {keys}"
        )

    clean = []
    seen = set()

    for row in rows:
        number = number_of(row)
        issue = issue_of(row)

        if number is None or not issue or issue in seen:
            continue

        seen.add(issue)

        color = (
            row.get("colour")
            or row.get("color")
            or row.get("colorName")
            or "—"
        )

        clean.append({
            "issue": issue,
            "number": number,
            "size": "BIG" if number >= 5 else "SMALL",
            "color": str(color)
        })

    if not clean:
        raise ValueError(
            "API rows were found, but no valid issue/number pairs were recognized."
        )

    return clean


# ================= HYBRID ANALYSIS =================

def hybrid_analysis(history):
    sides = [x["size"] for x in history]

    if len(sides) < 30:
        return {
            "side": "WAIT",
            "score": None,
            "next_issue": None,
            "basis": f"Need 30 valid draws; currently have {len(sides)}",
            "components": {},
            "confidence_label": "INSUFFICIENT DATA"
        }

    chrono = list(reversed(sides))

    recent20 = chrono[-20:]
    recent50 = chrono[-50:]

    big20 = recent20.count("BIG") / 20
    big50 = recent50.count("BIG") / 50

    current = sides[0]

    transitions = {
        "BIG": {"BIG": 0, "SMALL": 0},
        "SMALL": {"BIG": 0, "SMALL": 0}
    }

    for older, newer in zip(chrono[:-1], chrono[1:]):
        transitions[older][newer] += 1

    counts = transitions[current]
    total_transitions = sum(counts.values())

    transition_big = (
        counts["BIG"] / total_transitions
        if total_transitions else 0.5
    )

    streak_side = sides[0]
    streak = 0

    for side in sides:
        if side != streak_side:
            break
        streak += 1

    streak_bias = (
        0.06 if streak_side == "BIG" else -0.06
    ) * (0.5 if streak >= 4 else 1)

    probability_big = (
        0.40 * big20
        + 0.25 * big50
        + 0.30 * transition_big
        + 0.05 * (0.5 + streak_bias)
    )

    score = round(max(probability_big, 1 - probability_big) * 100)

    if abs(probability_big - 0.5) >= 0.08 and score >= 58:
        side = "BIG" if probability_big > 0.5 else "SMALL"
    else:
        side = "WAIT"

    try:
        next_issue = str(int(history[0]["issue"]) + 1)
    except (ValueError, TypeError):
        next_issue = None

    components = {
        "big_20_pct": round(big20 * 100),
        "big_50_pct": round(big50 * 100),
        "transition_big_pct": round(transition_big * 100),
        "current_streak": f"{streak_side} × {streak}",
        "sample_transitions": total_transitions
    }

    basis = (
        f"20-draw BIG {round(big20 * 100)}% · "
        f"50-draw BIG {round(big50 * 100)}% · "
        f"transition BIG {round(transition_big * 100)}% · "
        f"streak {streak_side}×{streak}"
    )

    label = (
        "LOW" if score < 65
        else "MODERATE" if score < 72
        else "HISTORICAL EDGE"
    )

    return {
        "side": side,
        "score": score,
        "next_issue": next_issue,
        "basis": basis,
        "components": components,
        "confidence_label": label
    }


# ================= BACKTEST =================

def backtest(history):
    chrono = list(reversed(history))

    wins = 0
    losses = 0
    skipped = 0
    tested = []

    for i in range(30, len(chrono)):
        previous = list(reversed(chrono[:i]))
        prediction = hybrid_analysis(previous)

        actual = chrono[i]["size"]

        if prediction["side"] == "WAIT":
            skipped += 1
            continue

        status = (
            "WIN"
            if prediction["side"] == actual
            else "LOSE"
        )

        if status == "WIN":
            wins += 1
        else:
            losses += 1

        tested.append({
            "issue": chrono[i]["issue"],
            "pick": prediction["side"],
            "actual": actual,
            "status": status
        })

    settled = wins + losses

    return {
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "tested": settled,
        "win_rate": round(wins / settled * 100, 1) if settled else None,
        "recent": list(reversed(tested[-12:]))
    }


# ================= LOGIN =================

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            session.clear()
            return jsonify({"error": "Login required"}), 401

        return fn(*args, **kwargs)

    return wrapper


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password", ""))

    if not LOGIN_PASSWORD:
        return jsonify({
            "ok": False,
            "error": "LOGIN_PASSWORD is not configured on the server"
        }), 500

    if not hmac.compare_digest(supplied, LOGIN_PASSWORD):
        return jsonify({
            "ok": False,
            "error": "Incorrect password"
        }), 401

    session.clear()
    session.permanent = True
    session["authenticated"] = True

    return jsonify({"ok": True})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ================= DASHBOARD API =================

@app.get("/api/dashboard")
@login_required
def api_dashboard():
    try:
        history = fetch_history()

        sample = history[:50]

        big = sum(
            1 for item in sample
            if item["size"] == "BIG"
        )

        total = len(sample)

        prediction = hybrid_analysis(history)
        bt = backtest(history)

        with db() as con:

            # Settle predictions when their target issue appears.
            for result in history:
                con.execute("""
                    UPDATE prediction_history
                    SET result_number = ?,
                        actual_side = ?,
                        status = CASE
                            WHEN predicted_side = ? THEN 'WIN'
                            ELSE 'LOSE'
                        END
                    WHERE target_issue = ?
                    AND status = 'PENDING'
                """, (
                    result["number"],
                    result["size"],
                    result["size"],
                    result["issue"]
                ))

            # Save a new prediction only when there is a signal.
            if (
                prediction.get("next_issue")
                and prediction.get("side") in ("BIG", "SMALL")
            ):
                con.execute("""
                    INSERT OR IGNORE INTO prediction_history
                    (
                        target_issue,
                        predicted_side,
                        score,
                        basis,
                        created_at,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, 'PENDING')
                """, (
                    prediction["next_issue"],
                    prediction["side"],
                    prediction.get("score"),
                    prediction.get("basis"),
                    utcnow().isoformat()
                ))

            rows = con.execute("""
                SELECT
                    target_issue,
                    predicted_side,
                    score,
                    basis,
                    created_at,
                    result_number,
                    actual_side,
                    status
                FROM prediction_history
                ORDER BY created_at DESC
                LIMIT 100
            """).fetchall()

        prediction_rows = [dict(row) for row in rows]

        wins = sum(
            row["status"] == "WIN"
            for row in prediction_rows
        )

        losses = sum(
            row["status"] == "LOSE"
            for row in prediction_rows
        )

        settled = wins + losses

        trend = [
            {
                "issue": item["issue"][-5:],
                "big": 1 if item["size"] == "BIG" else 0,
                "number": item["number"]
            }
            for item in reversed(sample)
        ]

        return jsonify({
            "ok": True,
            "history": history[:50],
            "prediction_history": prediction_rows,

            "prediction_stats": {
                "wins": wins,
                "losses": losses,
                "pending": sum(
                    row["status"] == "PENDING"
                    for row in prediction_rows
                ),
                "win_rate": (
                    round(wins / settled * 100, 1)
                    if settled else 0
                )
            },

            "stats": {
                "total": total,
                "big": big,
                "small": total - big,
                "big_pct": round(big / total * 100) if total else 0,
                "small_pct": round((total - big) / total * 100) if total else 0
            },

            "prediction": prediction,
            "backtest": bt,
            "trend": trend,
            "updated_at": utcnow().isoformat()
        })

    except requests.exceptions.Timeout:
        return jsonify({
            "error": "Live API unavailable: request timed out"
        }), 502

    except requests.exceptions.RequestException as error:
        return jsonify({
            "error": f"Live API request failed: {str(error)[:180]}"
        }), 502

    except Exception as error:
        return jsonify({
            "error": f"Live API unavailable: {str(error)[:180]}"
        }), 502


# ================= HEALTH CHECK =================

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "TRX WINGO",
        "time": utcnow().isoformat()
    })


# ================= START SERVER =================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080"))
    )
