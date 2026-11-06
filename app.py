
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

# ==================================================
# CONFIG
# ==================================================

API_URL = os.getenv(
    "API_URL",
    "https://draw.ar-lottery01.com/TrxWinGo/TrxWinGo_1M/GetHistoryIssuePage.json"
)

LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE-THIS-SECRET")
DB_PATH = os.getenv("DB_PATH", "wingo.sqlite3")

MIN_DRAWS = 10
MAX_HISTORY = 500

app = Flask(__name__)
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)


# ==================================================
# USER PATTERNS
# B = BIG, S = SMALL
# ==================================================

PATTERNS = [
    "SBSBBS", "BBSSBBSBS", "SBSBSBSBS",
    "BBBBBBBSB", "SBSBSBSBSSS",
    "BSSBSSSB", "BSBSBBSS", "BSSBSSBBS",
    "BSBBSB", "BBSSBSS",
    "SSBSBBSB", "SBBSSB", "BBSBBS",
    "SBSSB", "BSSBBS",
    "SSBBSBB", "BBSBSB", "SBBSBSB",
    "SBBSB", "SBBSBSBB",
    "SBSBSSSB", "BBSBS", "SSSBSB",
    "BSBSSS", "BSSSBBSSSBS",
    "SBBSSSB", "SSSSBSBB", "BBSSSB",
    "SSBBBSBS", "BSBBSSS",
    "BSSSSBBBBB", "SSSSBBBB",
    "BBBBSBBBSS", "BBBSBBSS", "BBSBSSS",
    "SBBSBBSSS", "BSBBBSSS", "BSSBBSB",
    "SSBSBSSB", "BSSSBSBS"
]

PATTERNS = list(dict.fromkeys(PATTERNS))


# ==================================================
# DATABASE
# ==================================================

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
            status TEXT DEFAULT 'PENDING'
        )
        """)


init_db()


def utcnow():
    return datetime.now(timezone.utc)


# ==================================================
# API PARSER
# ==================================================

ISSUE_KEYS = [
    "issueNumber", "issue", "period",
    "issue_no", "drawIssue", "expect",
    "periodNumber"
]

NUMBER_KEYS = [
    "number", "openNumber", "result",
    "lotteryNumber", "openCode",
    "winningNumber", "drawResult"
]


def find_lists(value):
    found = []

    if isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value):
            found.append(value)

        for item in value:
            found.extend(find_lists(item))

    elif isinstance(value, dict):
        for child in value.values():
            found.extend(find_lists(child))

    return found


def parse_rows(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    candidates = find_lists(payload)
    candidates.sort(key=len, reverse=True)

    for rows in candidates:
        if rows and any(
            any(k in row for k in ISSUE_KEYS)
            and any(k in row for k in NUMBER_KEYS)
            for row in rows
        ):
            return rows

    return []


def get_issue(row):
    for key in ISSUE_KEYS:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def get_number(row):
    raw = None

    for key in NUMBER_KEYS:
        if row.get(key) is not None:
            raw = str(row[key]).strip()
            break

    if raw is None:
        return None

    # TRX result must be a single digit 0-9.
    match = re.fullmatch(r"\d", raw)

    if not match:
        return None

    number = int(raw)

    return number if 0 <= number <= 9 else None


def fetch_history():
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://draw.ar-lottery01.com/"
    }

    response = requests.get(
        API_URL,
        headers=headers,
        timeout=20
    )

    response.raise_for_status()
    payload = response.json()

    rows = parse_rows(payload)

    if not rows:
        raise ValueError("API result list not found")

    output = []
    seen = set()

    for row in rows:
        issue = get_issue(row)
        number = get_number(row)

        if not issue or number is None or issue in seen:
            continue

        seen.add(issue)

        output.append({
            "issue": issue,
            "number": number,
            "size": "BIG" if number >= 5 else "SMALL",
            "color": str(
                row.get("colour")
                or row.get("color")
                or "—"
            )
        })

    if not output:
        raise ValueError("No valid TRX results found")

    return output[:MAX_HISTORY]


# ==================================================
# PATTERN MATCHING
# ==================================================

def pattern_analysis(history):
    sides = "".join(
        "B" if x["size"] == "BIG" else "S"
        for x in reversed(history)
    )

    matches = []

    for pattern in PATTERNS:
        length = len(pattern)

        if len(sides) <= length:
            continue

        # Find historical occurrences and their next outcome.
        for i in range(len(sides) - length):
            segment = sides[i:i + length]

            if segment == pattern:
                next_side = sides[i + length]

                matches.append({
                    "pattern": pattern,
                    "next": next_side
                })

    big = sum(x["next"] == "B" for x in matches)
    small = sum(x["next"] == "S" for x in matches)
    total = big + small

    if total:
        probability_big = big / total
    else:
        probability_big = 0.5

    return {
        "matches": total,
        "big": big,
        "small": small,
        "big_pct": round(probability_big * 100),
        "small_pct": round((1 - probability_big) * 100),
        "probability_big": probability_big
    }


# ==================================================
# HYBRID ANALYSIS
# ==================================================

def hybrid_analysis(history):

    sides = [x["size"] for x in history]

    if len(sides) < MIN_DRAWS:
        return {
            "side": "WAIT",
            "score": None,
            "next_issue": None,
            "basis": f"Need {MIN_DRAWS} draws; got {len(sides)}",
            "components": {},
            "confidence_label": "INSUFFICIENT DATA"
        }

    chrono = list(reversed(sides))

    recent5 = chrono[-5:]
    recent10 = chrono[-10:]
    recent20 = chrono[-min(20, len(chrono)):]

    big5 = recent5.count("BIG") / len(recent5)
    big10 = recent10.count("BIG") / len(recent10)
    big20 = recent20.count("BIG") / len(recent20)

    # Transition analysis
    transitions = {
        "BIG": {"BIG": 0, "SMALL": 0},
        "SMALL": {"BIG": 0, "SMALL": 0}
    }

    for older, newer in zip(chrono[:-1], chrono[1:]):
        transitions[older][newer] += 1

    current = sides[0]
    counts = transitions[current]
    transition_total = sum(counts.values())

    transition_big = (
        counts["BIG"] / transition_total
        if transition_total else 0.5
    )

    # Pattern analysis
    pattern = pattern_analysis(history)

    pattern_big = pattern["probability_big"]

    # Streak
    streak_side = sides[0]
    streak = 0

    for side in sides:
        if side != streak_side:
            break
        streak += 1

    streak_bias = (
        0.04 if streak_side == "BIG" else -0.04
    ) * (0.5 if streak >= 4 else 1)

    # Weighted hybrid probability
    probability_big = (
        0.25 * big5
        + 0.20 * big10
        + 0.15 * big20
        + 0.20 * transition_big
        + 0.15 * pattern_big
        + 0.05 * (0.5 + streak_bias)
    )

    score = round(
        max(probability_big, 1 - probability_big) * 100
    )

    # Require meaningful separation from 50/50.
    if abs(probability_big - 0.5) >= 0.08:
        side = "BIG" if probability_big > 0.5 else "SMALL"
    else:
        side = "WAIT"

    try:
        next_issue = str(int(history[0]["issue"]) + 1)
    except (ValueError, TypeError):
        next_issue = None

    components = {
        "big_5_pct": round(big5 * 100),
        "big_10_pct": round(big10 * 100),
        "big_20_pct": round(big20 * 100),
        "transition_big_pct": round(transition_big * 100),
        "pattern_big_pct": pattern["big_pct"],
        "pattern_matches": pattern["matches"],
        "pattern_big": pattern["big"],
        "pattern_small": pattern["small"],
        "current_streak": f"{streak_side} × {streak}"
    }

    basis = (
        f"5 Draw {round(big5 * 100)}% | "
        f"10 Draw {round(big10 * 100)}% | "
        f"20 Draw {round(big20 * 100)}% | "
        f"Transition {round(transition_big * 100)}% | "
        f"Pattern {pattern['big_pct']}% "
        f"({pattern['matches']} matches)"
    )

    label = (
        "LOW SAMPLE" if len(sides) < 20
        else "MODERATE SAMPLE" if len(sides) < 50
        else "HISTORICAL ANALYSIS"
    )

    return {
        "side": side,
        "score": score,
        "next_issue": next_issue,
        "basis": basis,
        "components": components,
        "confidence_label": label
    }


# ==================================================
# BACKTEST
# ==================================================

def backtest(history):
    chrono = list(reversed(history))

    wins = 0
    losses = 0
    skipped = 0
    tested = []

    for i in range(MIN_DRAWS, len(chrono)):

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


# ==================================================
# LOGIN
# ==================================================

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
            "error": "LOGIN_PASSWORD is not configured"
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


# ==================================================
# DASHBOARD
# ==================================================

@app.get("/api/dashboard")
@login_required
def api_dashboard():

    try:
        history = fetch_history()

        sample = history[:50]

        big = sum(
            1 for x in sample
            if x["size"] == "BIG"
        )

        total = len(sample)

        prediction = hybrid_analysis(history)
        bt = backtest(history)

        with db() as con:

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
                SELECT *
                FROM prediction_history
                ORDER BY created_at DESC
                LIMIT 100
            """).fetchall()

        prediction_rows = [dict(x) for x in rows]

        wins = sum(x["status"] == "WIN" for x in prediction_rows)
        losses = sum(x["status"] == "LOSE" for x in prediction_rows)
        settled = wins + losses

        trend = [
            {
                "issue": x["issue"][-5:],
                "big": 1 if x["size"] == "BIG" else 0,
                "number": x["number"]
            }
            for x in reversed(sample)
        ]

        return jsonify({
            "ok": True,
            "history": history[:50],
            "prediction_history": prediction_rows,

            "prediction_stats": {
                "wins": wins,
                "losses": losses,
                "pending": sum(
                    x["status"] == "PENDING"
                    for x in prediction_rows
                ),
                "win_rate": round(wins / settled * 100, 1) if settled else 0
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
            "pattern_analysis": pattern_analysis(history),
            "pattern_count": len(PATTERNS),
            "updated_at": utcnow().isoformat()
        })

    except requests.exceptions.Timeout:
        return jsonify({
            "error": "Live API timeout"
        }), 502

    except requests.exceptions.RequestException as error:
        return jsonify({
            "error": f"API request failed: {str(error)[:180]}"
        }), 502

    except Exception as error:
        return jsonify({
            "error": f"Dashboard error: {str(error)[:180]}"
        }), 502


# ==================================================
# HEALTH CHECK
# ==================================================

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "TRX WINGO V3",
        "patterns": len(PATTERNS),
        "time": utcnow().isoformat()
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080"))
    )
