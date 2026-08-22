#!/usr/bin/env python3

import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from flask import (
    Flask,
    Response,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
)
from werkzeug.utils import secure_filename


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("SHOT_COUNTER_CONFIG", APP_ROOT / "config.yaml")
)


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config()

TIMEZONE_NAME = config.get("timezone", "Europe/Oslo")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
os.environ["TZ"] = TIMEZONE_NAME

if hasattr(time, "tzset"):
    time.tzset()

DB_PATH = config["database"]["path"]
DETECTOR_NAME = config["detector"]["name"]
RANGE_NAME = config["detector"]["range"]
MODE = config["detector"]["mode"]
UPLOAD_DIR = Path(
    config.get("uploads", {}).get(
        "incoming",
        APP_ROOT / "uploads" / "incoming",
    )
)
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".m4a", ".mp3", ".aac"}
MAX_UPLOAD_BYTES = int(
    config.get("uploads", {}).get(
        "max_bytes",
        95 * 1024 * 1024,
    )
)
ADMIN_PIN = str(config.get("admin", {}).get("pin", ""))
PRIVACY_MODE_HOURS = int(
    config.get("privacy", {}).get("mode_hours", 6)
)
PRIVACY_PUBLISH_MIN_HOURS = int(
    config.get("privacy", {}).get("publish_delay_min_hours", 24)
)
PRIVACY_PUBLISH_MAX_HOURS = int(
    config.get("privacy", {}).get("publish_delay_max_hours", 48)
)
REGISTRATION_PAUSE_HOURS = int(
    config.get("privacy", {}).get("registration_pause_hours", 24)
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                detector TEXT NOT NULL,
                range_name TEXT NOT NULL,
                confidence REAL,
                peak REAL,
                uploaded INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_privacy_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reset_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_privacy_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                publish_after TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_registration_pauses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ends_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def add_shot(confidence=1.0, peak=1.0):
    if registration_pause_state()["active"]:
        print("Shot ignored because registration is paused.")
        return False

    timestamp = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO shots (
                timestamp,
                detector,
                range_name,
                confidence,
                peak,
                uploaded
            )
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                timestamp,
                DETECTOR_NAME,
                RANGE_NAME,
                confidence,
                peak,
            ),
        )
        conn.commit()

    print(
        f"SHOT {timestamp} "
        f"confidence={confidence:.2f} "
        f"peak={peak:.2f}"
    )

    return True


def shot_count():
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        return conn.execute(
            """
            SELECT COUNT(*)
            FROM shots
            WHERE NOT EXISTS (
                SELECT 1
                FROM dashboard_privacy_sessions AS session
                WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                  AND julianday(shots.timestamp) < julianday(session.ends_at)
                  AND julianday(session.publish_after) > julianday(?)
            )
            """,
            (now_iso,),
        ).fetchone()[0]


def queued_count():
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT COUNT(*)
            FROM shots
            WHERE uploaded = 0
              AND NOT EXISTS (
                SELECT 1
                FROM dashboard_privacy_sessions AS session
                WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                  AND julianday(shots.timestamp) < julianday(session.ends_at)
              )
            """
        ).fetchone()[0]


def format_local(timestamp):
    if not timestamp:
        return None

    dt = datetime.fromisoformat(timestamp)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(LOCAL_TZ)


def privacy_cutoff():
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT reset_at
            FROM dashboard_privacy_resets
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    return row["reset_at"] if row else None


def privacy_cutoff_payload(cutoff):
    if not cutoff:
        return None

    dt = format_local(cutoff)

    return {
        "iso": dt.isoformat(),
        "date": dt.strftime("%d.%m.%Y"),
        "time": dt.strftime("%H:%M"),
    }


def deadline_payload(timestamp):
    dt = format_local(timestamp)

    return {
        "iso": dt.isoformat(),
        "date": dt.strftime("%d.%m.%Y"),
        "time": dt.strftime("%H:%M"),
    }


def active_privacy_session(conn, now_iso):
    return conn.execute(
        """
        SELECT id, started_at, ends_at, publish_after
        FROM dashboard_privacy_sessions
        WHERE julianday(started_at) <= julianday(?)
          AND julianday(ends_at) > julianday(?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (now_iso, now_iso),
    ).fetchone()


def privacy_mode_state():
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        row = active_privacy_session(conn, now_iso)

    if not row:
        return {"active": False}

    return {
        "active": True,
        "started_at": deadline_payload(row["started_at"]),
        "ends_at": deadline_payload(row["ends_at"]),
    }


def delayed_publish_after(session_end):
    minimum = max(0, PRIVACY_PUBLISH_MIN_HOURS)
    maximum = max(minimum, PRIVACY_PUBLISH_MAX_HOURS)
    spread_seconds = (maximum - minimum) * 60 * 60
    delay_seconds = minimum * 60 * 60

    if spread_seconds:
        delay_seconds += secrets.randbelow(spread_seconds + 1)

    return session_end + timedelta(seconds=delay_seconds)


def set_privacy_mode(enabled):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = active_privacy_session(conn, now_iso)

        if enabled and not row:
            ends_at = now + timedelta(hours=PRIVACY_MODE_HOURS)
            publish_after = delayed_publish_after(ends_at)

            conn.execute(
                """
                INSERT INTO dashboard_privacy_resets (reset_at)
                VALUES (?)
                """,
                (now_iso,),
            )
            conn.execute(
                """
                INSERT INTO dashboard_privacy_sessions (
                    started_at,
                    ends_at,
                    publish_after
                )
                VALUES (?, ?, ?)
                """,
                (
                    now_iso,
                    ends_at.isoformat(),
                    publish_after.isoformat(),
                ),
            )
        elif not enabled and row:
            publish_after = delayed_publish_after(now)
            conn.execute(
                """
                UPDATE dashboard_privacy_sessions
                SET ends_at = ?, publish_after = ?
                WHERE id = ?
                """,
                (now_iso, publish_after.isoformat(), row["id"]),
            )

        conn.commit()

    return privacy_mode_state()


def active_registration_pause(conn, now_iso):
    return conn.execute(
        """
        SELECT id, started_at, ends_at
        FROM dashboard_registration_pauses
        WHERE julianday(started_at) <= julianday(?)
          AND julianday(ends_at) > julianday(?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (now_iso, now_iso),
    ).fetchone()


def registration_pause_state():
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        row = active_registration_pause(conn, now_iso)

    if not row:
        return {"active": False}

    return {
        "active": True,
        "started_at": deadline_payload(row["started_at"]),
        "ends_at": deadline_payload(row["ends_at"]),
    }


def set_registration_pause(enabled):
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = active_registration_pause(conn, now_iso)

        if enabled and not row:
            ends_at = now + timedelta(hours=REGISTRATION_PAUSE_HOURS)
            conn.execute(
                """
                INSERT INTO dashboard_registration_pauses (
                    started_at,
                    ends_at
                )
                VALUES (?, ?)
                """,
                (now_iso, ends_at.isoformat()),
            )
        elif not enabled and row:
            conn.execute(
                """
                UPDATE dashboard_registration_pauses
                SET ends_at = ?
                WHERE id = ?
                """,
                (now_iso, row["id"]),
            )

        conn.commit()

    return registration_pause_state()


def valid_admin_pin():
    supplied = request.headers.get("X-Shot-Counter-PIN", "")
    return bool(ADMIN_PIN) and secrets.compare_digest(
        str(supplied),
        ADMIN_PIN,
    )


def record_privacy_reset():
    reset_at = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO dashboard_privacy_resets (reset_at)
            VALUES (?)
            """,
            (reset_at,),
        )
        conn.commit()

    return reset_at


def last_shot(cutoff=None):
    conditions = [
        """
        NOT EXISTS (
            SELECT 1
            FROM dashboard_privacy_sessions AS session
            WHERE julianday(shots.timestamp) >= julianday(session.started_at)
              AND julianday(shots.timestamp) < julianday(session.ends_at)
        )
        """
    ]
    parameters = []

    if cutoff:
        conditions.append(
            "julianday(shots.timestamp) >= julianday(?)"
        )
        parameters.append(cutoff)

    where_clause = "WHERE " + " AND ".join(conditions)

    with db_connect() as conn:
        row = conn.execute(
            f"""
            SELECT timestamp
            FROM shots
            {where_clause}
            ORDER BY julianday(timestamp) DESC, id DESC
            LIMIT 1
            """,
            parameters,
        ).fetchone()

    if not row:
        return None

    dt = format_local(row["timestamp"])

    return {
        "iso": dt.isoformat(),
        "time": dt.strftime("%H:%M:%S"),
        "date": dt.strftime("%d.%m.%Y"),
    }


def recent_shots(limit=10, cutoff=None):
    conditions = [
        """
        NOT EXISTS (
            SELECT 1
            FROM dashboard_privacy_sessions AS session
            WHERE julianday(shots.timestamp) >= julianday(session.started_at)
              AND julianday(shots.timestamp) < julianday(session.ends_at)
        )
        """
    ]
    parameters = []

    if cutoff:
        conditions.append(
            "julianday(shots.timestamp) >= julianday(?)"
        )
        parameters.append(cutoff)

    where_clause = "WHERE " + " AND ".join(conditions)
    parameters.append(limit)

    with db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, timestamp, confidence, peak
            FROM shots
            {where_clause}
            ORDER BY julianday(timestamp) DESC, id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()

    result = []

    for row in rows:
        dt = format_local(row["timestamp"])

        result.append(
            {
                "id": row["id"],
                "time": dt.strftime("%H:%M:%S"),
                "date": dt.strftime("%d.%m.%Y"),
                "confidence": row["confidence"],
                "peak": row["peak"],
            }
        )

    return result


def activity_statistics(cutoff=None):
    """Return calendar-day statistics in the server's Europe/Oslo timezone."""
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM shots
            WHERE NOT EXISTS (
                SELECT 1
                FROM dashboard_privacy_sessions AS session
                WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                  AND julianday(shots.timestamp) < julianday(session.ends_at)
                  AND julianday(session.publish_after) > julianday(?)
            )
            """,
            (now_iso,),
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT
                date(shots.timestamp, 'localtime') AS activity_day,
                COUNT(*) AS shot_count
            FROM shots
            WHERE NOT EXISTS (
                SELECT 1
                FROM dashboard_privacy_sessions AS session
                WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                  AND julianday(shots.timestamp) < julianday(session.ends_at)
                  AND julianday(session.publish_after) > julianday(?)
            )
            GROUP BY activity_day
            ORDER BY activity_day
            """,
            (now_iso,),
        ).fetchall()
        detail_rows = conn.execute(
            """
            SELECT
                date(shots.timestamp, 'localtime') AS activity_day,
                COUNT(*) AS shot_count
            FROM shots
            WHERE NOT EXISTS (
                SELECT 1
                FROM dashboard_privacy_sessions AS session
                WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                  AND julianday(shots.timestamp) < julianday(session.ends_at)
            )
            GROUP BY activity_day
            ORDER BY activity_day
            """
        ).fetchall()

        if cutoff:
            short_term_rows = conn.execute(
                """
                SELECT
                    date(shots.timestamp, 'localtime') AS activity_day,
                    COUNT(*) AS shot_count
                FROM shots
                WHERE julianday(shots.timestamp) >= julianday(?)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM dashboard_privacy_sessions AS session
                    WHERE julianday(shots.timestamp) >= julianday(session.started_at)
                      AND julianday(shots.timestamp) < julianday(session.ends_at)
                  )
                GROUP BY activity_day
                ORDER BY activity_day
                """,
                (cutoff,),
            ).fetchall()
        else:
            short_term_rows = detail_rows

    def rows_to_daily_counts(source_rows):
        counts = {}

        for row in source_rows:
            if not row["activity_day"]:
                continue

            day = datetime.strptime(
                row["activity_day"],
                "%Y-%m-%d",
            ).date()
            counts[day] = row["shot_count"]

        return counts

    daily_counts = rows_to_daily_counts(rows)
    detail_counts = rows_to_daily_counts(detail_rows)
    short_term_counts = rows_to_daily_counts(short_term_rows)

    today = datetime.now(LOCAL_TZ).date()

    def counts_for_last(source_counts, days):
        first_day = today - timedelta(days=days - 1)
        return {
            day: count
            for day, count in source_counts.items()
            if first_day <= day <= today
        }

    last_7 = counts_for_last(short_term_counts, 7)
    last_30 = counts_for_last(daily_counts, 30)
    detail_last_30 = counts_for_last(detail_counts, 30)
    last_365 = counts_for_last(daily_counts, 365)
    this_calendar_year = {
        day: count
        for day, count in daily_counts.items()
        if day.year == today.year and day <= today
    }

    def day_stat(items):
        if not items:
            return None

        # If several days share the record, show the earliest one.
        day, count = sorted(
            items.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]

        return {
            "iso": day.isoformat(),
            "date": day.strftime("%d.%m.%Y"),
            "shots": count,
        }

    active_days_30 = len(detail_last_30)
    shots_30 = sum(last_30.values())
    detail_shots_30 = sum(detail_last_30.values())
    last_activity_day = max(detail_counts) if detail_counts else None

    return {
        "today": short_term_counts.get(today, 0),
        "yesterday": short_term_counts.get(
            today - timedelta(days=1),
            0,
        ),
        "last_7_days": sum(last_7.values()),
        "last_30_days": shots_30,
        "last_365_days": sum(last_365.values()),
        "this_calendar_year": sum(this_calendar_year.values()),
        "active_days_30": active_days_30,
        "average_per_active_day_30": (
            round(detail_shots_30 / active_days_30, 1)
            if active_days_30
            else 0
        ),
        "most_active_day_30": day_stat(detail_last_30),
        "record_day": day_stat(detail_counts),
        "last_activity_day": (
            {
                "iso": last_activity_day.isoformat(),
                "date": last_activity_day.strftime("%d.%m.%Y"),
            }
            if last_activity_day
            else None
        ),
        "total": total,
    }


def unique_upload_destination(filename):
    destination = UPLOAD_DIR / filename

    if not destination.exists():
        return destination

    source = Path(filename)

    return UPLOAD_DIR / (
        f"{source.stem}-{uuid4().hex[:8]}{source.suffix}"
    )


def queue_audio_upload(audio_file):
    filename = secure_filename(audio_file.filename or "")

    if not filename:
        raise ValueError("Velg en lydfil som skal lastes opp.")

    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise ValueError(
            "Filtypen støttes ikke. Bruk WAV, M4A, MP3 eller AAC."
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temporary = UPLOAD_DIR / f".uploading-{uuid4().hex}.part"

    try:
        audio_file.save(temporary)

        if temporary.stat().st_size == 0:
            raise ValueError("Lydfilen er tom.")

        os.chmod(temporary, 0o664)
        destination = unique_upload_destination(filename)
        os.replace(temporary, destination)

        return destination
    finally:
        temporary.unlink(missing_ok=True)


HTML = """
<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive">
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/favicon.png">

<title>Shot Counter</title>

<style>
    * {
        box-sizing: border-box;
    }

    body {
        margin: 0;
        font-family:
            system-ui,
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            sans-serif;

        background: #101214;
        color: #f2f2f2;
    }

    .container {
        max-width: 900px;
        margin: 0 auto;
        padding: 30px 20px;
    }

    header {
        text-align: center;
        margin-bottom: 30px;
    }

    h1 {
        margin: 0;
        font-size: 30px;
    }

    .subtitle {
        margin-top: 8px;
        color: #999;
    }

    .counter {
        position: relative;
        overflow: hidden;
        background: #191c1f;
        border: 1px solid #292d31;
        border-radius: 18px;
        padding: 35px 20px;
        text-align: center;
        margin-bottom: 20px;
    }

    .experimental-ribbon {
        position: absolute;
        top: 24px;
        right: -46px;
        width: 180px;
        padding: 8px 0;
        transform: rotate(45deg);
        background: linear-gradient(135deg, #9f2731, #c63d45);
        border-top: 1px solid rgba(255, 255, 255, 0.16);
        border-bottom: 1px solid rgba(0, 0, 0, 0.28);
        box-shadow: 0 5px 14px rgba(0, 0, 0, 0.28);
        color: #fff;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1.2px;
        line-height: 1;
        pointer-events: none;
        z-index: 1;
    }

    .counter-number {
        font-size: 80px;
        line-height: 1;
        font-weight: 700;
        letter-spacing: -3px;
    }

    .counter-label {
        margin-top: 10px;
        color: #aaa;
        font-size: 15px;
        letter-spacing: 2px;
    }

    .grid {
        display: grid;
        grid-template-columns:
            repeat(auto-fit, minmax(180px, 1fr));
        gap: 15px;
        margin-bottom: 20px;
    }

    .section-title {
        margin: 26px 2px 12px;
        color: #8d9297;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 1.4px;
        text-transform: uppercase;
    }

    .period-grid .card-value {
        font-size: 30px;
    }

    .statistics-grid {
        grid-template-columns:
            repeat(auto-fit, minmax(220px, 1fr));
    }

    .card-detail {
        margin-top: 6px;
        color: #8d9297;
        font-size: 14px;
    }

    .card {
        background: #191c1f;
        border: 1px solid #292d31;
        border-radius: 14px;
        padding: 20px;
    }

    .card-label {
        color: #8d9297;
        font-size: 13px;
        margin-bottom: 8px;
    }

    .card-value {
        font-size: 22px;
        font-weight: 600;
    }

    .online {
        color: #5bd66f;
    }

    .status-warning {
        color: #d0a646;
    }

    .offline {
        color: #ff7b72;
    }

    .status-detail {
        white-space: pre-line;
    }

    .table-card {
        background: #191c1f;
        border: 1px solid #292d31;
        border-radius: 14px;
        overflow: hidden;
    }

    .upload-card,
    .privacy-card {
        background: #191c1f;
        border: 1px solid #292d31;
        border-radius: 14px;
        padding: 20px;
    }

    .upload-description,
    .privacy-description {
        margin: 0 0 16px;
        color: #aaa;
        line-height: 1.5;
    }

    .upload-form {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 12px;
    }

    .upload-form input[type="file"] {
        flex: 1 1 320px;
        min-width: 0;
        color: #c8c8c8;
    }

    .upload-form input[type="file"]::file-selector-button {
        margin-right: 12px;
        background: #31363b;
        color: white;
        border: 1px solid #454b51;
        border-radius: 8px;
        padding: 9px 14px;
        cursor: pointer;
    }

    .upload-form button {
        margin-top: 0;
    }

    .upload-form button:disabled {
        cursor: wait;
        opacity: 0.65;
    }

    .privacy-card button {
        margin-top: 0;
    }

    .privacy-card button:disabled {
        cursor: wait;
        opacity: 0.65;
    }

    .admin-pin-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 10px;
        margin-bottom: 18px;
    }

    .admin-pin-row label {
        color: #aaa;
        font-size: 14px;
        font-weight: 600;
    }

    .admin-pin-row input {
        width: 130px;
        background: #111315;
        color: #fff;
        border: 1px solid #454b51;
        border-radius: 8px;
        padding: 9px 11px;
    }

    .privacy-controls {
        display: grid;
        gap: 14px;
    }

    .privacy-control {
        border-top: 1px solid #292d31;
        padding-top: 14px;
    }

    .privacy-control-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
    }

    .privacy-control-description {
        margin: 8px 0 0;
        color: #8d9297;
        font-size: 14px;
        line-height: 1.5;
    }

    .toggle-button {
        min-width: 82px;
        white-space: nowrap;
    }

    .toggle-button.active {
        background: #28543a;
        border-color: #3c7a52;
    }

    .toggle-button.danger.active {
        background: #6d3035;
        border-color: #9a454c;
    }

    .upload-status {
        min-height: 20px;
        margin-top: 14px;
        color: #8d9297;
        font-size: 14px;
    }

    .upload-status.success {
        color: #5bd66f;
    }

    .upload-status.error {
        color: #ff7b72;
    }

    .privacy-status {
        min-height: 20px;
        margin-top: 14px;
        color: #8d9297;
        font-size: 14px;
    }

    .privacy-status.success {
        color: #5bd66f;
    }

    .privacy-status.error {
        color: #ff7b72;
    }

    .table-title {
        padding: 18px 20px;
        font-weight: 600;
        border-bottom: 1px solid #292d31;
    }

    table {
        width: 100%;
        border-collapse: collapse;
    }

    th,
    td {
        text-align: left;
        padding: 12px 20px;
        border-bottom: 1px solid #25292d;
    }

    th {
        color: #888;
        font-size: 12px;
        font-weight: 500;
    }

    tr:last-child td {
        border-bottom: none;
    }

    .footer {
        text-align: center;
        color: #666;
        font-size: 12px;
        margin-top: 20px;
    }

    .footer-disclaimer {
        max-width: 760px;
        margin: 0 auto 10px;
        color: #8d9297;
        line-height: 1.5;
    }

    .footer a {
        color: #8d9297;
        text-decoration: none;
    }

    .footer a:hover {
        color: #c5c8ca;
        text-decoration: underline;
    }

    button {
        margin-top: 20px;
        background: #31363b;
        color: white;
        border: 1px solid #454b51;
        border-radius: 10px;
        padding: 10px 18px;
        cursor: pointer;
        font-size: 14px;
    }

    button:hover {
        background: #3a4046;
    }
</style>
</head>

<body>

<div class="container">

<header>
    <h1>Shot Counter</h1>
    <div class="subtitle">
        Proof of Concept
    </div>
</header>


<div class="counter">

    <div class="experimental-ribbon">Experimental</div>

    <div
        id="shots-total"
        class="counter-number">
        -
    </div>

    <div class="counter-label">
        TOTALT REGISTRERT
    </div>

    <button
        onclick="simulateShot()"
        id="simulate-button">
        Simuler skudd
    </button>

</div>


<div class="section-title">Aktivitet</div>

<div class="grid period-grid">

    <div class="card">
        <div class="card-label">I dag</div>
        <div id="shots-today" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">I går</div>
        <div id="shots-yesterday" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">Siste uken</div>
        <div id="shots-7-days" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">Siste måneden</div>
        <div id="shots-30-days" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">Siste året</div>
        <div id="shots-365-days" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">Dette kalenderåret</div>
        <div id="shots-calendar-year" class="card-value">-</div>
    </div>

</div>


<div class="section-title">Statistikk</div>

<div class="grid statistics-grid">

    <div class="card">
        <div class="card-label">Aktive dager siste 30 dager</div>
        <div id="active-days-30" class="card-value">-</div>
    </div>

    <div class="card">
        <div class="card-label">Snitt per aktiv dag</div>
        <div id="average-active-day" class="card-value">-</div>
        <div class="card-detail">Siste 30 dager</div>
    </div>

    <div class="card">
        <div class="card-label">Mest aktive dag</div>
        <div id="most-active-day-date" class="card-value">-</div>
        <div id="most-active-day-shots" class="card-detail">-</div>
    </div>

    <div class="card">
        <div class="card-label">Rekorddag</div>
        <div id="record-day-date" class="card-value">-</div>
        <div id="record-day-shots" class="card-detail">-</div>
    </div>

    <div class="card">
        <div class="card-label">Siste aktivitetsdag</div>
        <div id="last-activity-day" class="card-value">-</div>
    </div>

</div>


<div class="section-title">System</div>


<div class="grid">

    <div class="card">
        <div class="card-label">
            Status
        </div>

        <div
            id="status"
            class="card-value online">
            -
        </div>
        <div
            id="status-detail"
            class="card-detail status-detail"></div>
    </div>


    <div class="card">
        <div class="card-label">
            Siste skudd
        </div>

        <div
            id="last-shot"
            class="card-value">
            -
        </div>
    </div>


    <div class="card">
        <div class="card-label">
            Modus
        </div>

        <div
            id="mode"
            class="card-value">
            -
        </div>
    </div>


    <div class="card">
        <div class="card-label">
            Venter på opplasting
        </div>

        <div
            id="queued"
            class="card-value">
            -
        </div>
    </div>

</div>


<div class="table-card">

    <div class="table-title">
        Siste registreringer
    </div>

    <table>

        <thead>
        <tr>
            <th>Tid</th>
            <th>Dato</th>
            <th>Confidence</th>
            <th>Peak</th>
        </tr>
        </thead>

        <tbody id="recent-shots">
        </tbody>

    </table>

</div>


<div class="section-title">Last opp lydfil</div>

<div class="upload-card">
    <p class="upload-description">
        Last opp et lydopptak – det blir automatisk analysert og lagt til i statistikken.
        <br>
        Støttede formater: WAV, M4A, MP3 og AAC. Maks 95 MB.
    </p>

    <form id="upload-form" class="upload-form">
        <input
            id="audio-file"
            name="audio"
            type="file"
            accept=".wav,.m4a,.mp3,.aac"
            required>

        <button id="upload-button" type="submit">
            Last opp og behandle
        </button>
    </form>

    <div
        id="upload-status"
        class="upload-status"
        role="status"
        aria-live="polite"></div>
</div>


<div class="section-title">Personvern</div>

<div class="privacy-card">
    <div class="admin-pin-row">
        <label for="admin-pin">Admin-PIN</label>
        <input
            id="admin-pin"
            type="password"
            inputmode="numeric"
            autocomplete="off"
            placeholder="PIN">
    </div>

    <div class="privacy-controls">
        <div class="privacy-control">
            <div class="privacy-control-heading">
                <strong>Skjul korttidsaktivitet</strong>
                <button
                    id="privacy-mode-button"
                    class="toggle-button"
                    type="button"
                    aria-pressed="false">
                    Av
                </button>
            </div>
            <p class="privacy-control-description">
                Nye registreringer lagres, men skjules permanent fra
                korttids- og datovisninger. Samlet aktivitet publiseres
                i måneds-, års- og totaltall etter 24–48 timer. Modusen
                slås automatisk av etter 6 timer.
            </p>
            <div
                id="privacy-mode-status"
                class="privacy-status"
                role="status"
                aria-live="polite"></div>
        </div>

        <div class="privacy-control">
            <div class="privacy-control-heading">
                <strong>Stopp registrering</strong>
                <button
                    id="registration-pause-button"
                    class="toggle-button danger"
                    type="button"
                    aria-pressed="false">
                    Av
                </button>
            </div>
            <p class="privacy-control-description">
                Avviser nye nettleseropplastinger og setter behandling
                av køen på pause. Registrering starter automatisk igjen
                etter 24 timer.
            </p>
            <div
                id="registration-pause-status"
                class="privacy-status"
                role="status"
                aria-live="polite"></div>
        </div>

        <div class="privacy-control">
            <div class="privacy-control-heading">
                <strong>Nullstill korttidsvisningen</strong>
                <button id="privacy-reset-button" type="button">
                    Nullstill
                </button>
            </div>
            <p class="privacy-control-description">
                Nullstill korttidsvisningen. Måneds-, års- og totaltall
                beholdes.
            </p>
            <div
                id="privacy-reset-status"
                class="privacy-status"
                role="status"
                aria-live="polite"></div>
        </div>
    </div>
</div>


<div class="footer">
    <div class="footer-disclaimer">
        Dette er et eksperimentelt verktøy for læring og uformell
        statistikk. Det kan telle feil og er ikke en offisiell
        skuddlogg. Skal ikke brukes som sikkerhetssystem eller som
        grunnlag for kontroll, fakturering eller myndighetskrav.
    </div>
    <div>
        Automatisk skuddteller ·
        <a
            href="https://github.com/G33kM0bile/Shot-counter"
            target="_blank"
            rel="noopener noreferrer">
            GitHub
        </a>
    </div>
</div>

</div>


<script>

const numberFormatter =
    new Intl.NumberFormat("nb-NO");

const decimalFormatter =
    new Intl.NumberFormat(
        "nb-NO",
        {
            minimumFractionDigits: 0,
            maximumFractionDigits: 1
        }
    );

const actionHeaderName =
    "X-Shot-Counter-Action";

function showDayStat(dateId, shotsId, stat) {

    document.getElementById(
        dateId
    ).textContent =
        stat ? stat.date : "Ingen aktivitet";

    document.getElementById(
        shotsId
    ).textContent =
        stat
            ? `${numberFormatter.format(stat.shots)} skudd`
            : "";
}

function adminHeaders(action, pin, json = false) {

    const headers = {
        "X-Shot-Counter-PIN": pin
    };

    headers[actionHeaderName] = action;

    if (json) {
        headers["Content-Type"] = "application/json";
    }

    return headers;
}

function readAdminPin(status) {

    const input =
        document.getElementById("admin-pin");
    const pin = input.value.trim();

    if (!pin) {
        status.className = "privacy-status error";
        status.textContent = "Skriv inn admin-PIN.";
        input.focus();
        return null;
    }

    return pin;
}

function setToggleState(button, active) {

    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.textContent = active ? "På" : "Av";
}

async function togglePrivacyMode() {

    const button =
        document.getElementById("privacy-mode-button");
    const status =
        document.getElementById("privacy-mode-status");
    const active =
        button.getAttribute("aria-pressed") === "true";
    const enabled = !active;
    const pin = readAdminPin(status);

    if (!pin) {
        return;
    }

    const confirmed = window.confirm(
        enabled
            ? "Skjul korttidsaktivitet i inntil 6 timer?"
            : "Avslutt skjuling av korttidsaktivitet?"
    );

    if (!confirmed) {
        return;
    }

    button.disabled = true;
    status.className = "privacy-status";
    status.textContent = "Oppdaterer …";

    try {
        const response = await fetch(
            "/api/privacy-mode",
            {
                method: "POST",
                headers: adminHeaders("privacy-mode", pin, true),
                body: JSON.stringify({enabled})
            }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Endringen mislyktes.");
        }

        status.className = "privacy-status success";
        status.textContent = data.message;
        await updateDashboard();
    } catch (error) {
        status.className = "privacy-status error";
        status.textContent = error.message || "Endringen mislyktes.";
    } finally {
        button.disabled = false;
    }
}

async function toggleRegistrationPause() {

    const button =
        document.getElementById("registration-pause-button");
    const status =
        document.getElementById("registration-pause-status");
    const active =
        button.getAttribute("aria-pressed") === "true";
    const enabled = !active;
    const pin = readAdminPin(status);

    if (!pin) {
        return;
    }

    const confirmed = window.confirm(
        enabled
            ? "Stopp registrering og opplasting i inntil 24 timer?"
            : "Start registrering igjen nå?"
    );

    if (!confirmed) {
        return;
    }

    button.disabled = true;
    status.className = "privacy-status";
    status.textContent = "Oppdaterer …";

    try {
        const response = await fetch(
            "/api/registration-pause",
            {
                method: "POST",
                headers: adminHeaders("registration-pause", pin, true),
                body: JSON.stringify({enabled})
            }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.error || "Endringen mislyktes.");
        }

        status.className = "privacy-status success";
        status.textContent = data.message;
        await updateDashboard();
    } catch (error) {
        status.className = "privacy-status error";
        status.textContent = error.message || "Endringen mislyktes.";
    } finally {
        button.disabled = false;
    }
}

function uploadAudio(event) {

    event.preventDefault();

    const input =
        document.getElementById("audio-file");
    const button =
        document.getElementById("upload-button");
    const status =
        document.getElementById("upload-status");

    if (!input.files.length) {
        status.className = "upload-status error";
        status.textContent = "Velg en lydfil først.";
        return;
    }

    const data = new FormData();
    data.append("audio", input.files[0]);

    const upload = new XMLHttpRequest();

    button.disabled = true;
    button.dataset.uploading = "true";
    status.className = "upload-status";
    status.textContent = "Starter opplasting …";

    upload.upload.addEventListener(
        "progress",
        (progress) => {
            if (progress.lengthComputable) {
                const percent = Math.round(
                    progress.loaded / progress.total * 100
                );
                status.textContent =
                    `Laster opp … ${percent} %`;
            }
        }
    );

    upload.addEventListener(
        "load",
        () => {
            let response = {};

            try {
                response = JSON.parse(upload.responseText);
            } catch (error) {
                response = {};
            }

            if (upload.status >= 200 && upload.status < 300) {
                status.className = "upload-status success";
                status.textContent =
                    response.message ||
                    "Filen er lastet opp og står i behandlingskø.";
                input.value = "";
            } else {
                status.className = "upload-status error";
                status.textContent =
                    response.error ||
                    "Opplastingen mislyktes. Prøv igjen.";
            }

            button.disabled = false;
            delete button.dataset.uploading;
        }
    );

    upload.addEventListener(
        "error",
        () => {
            status.className = "upload-status error";
            status.textContent =
                "Kunne ikke kontakte serveren. Prøv igjen.";
            button.disabled = false;
            delete button.dataset.uploading;
        }
    );

    upload.open("POST", "/api/upload");
    upload.send(data);
}

async function resetPrivacyDisplay() {

    const confirmed = window.confirm(
        "Nullstill korttidsvisningen? Ingen registrerte skudd blir slettet."
    );

    if (!confirmed) {
        return;
    }

    const button =
        document.getElementById("privacy-reset-button");
    const status =
        document.getElementById("privacy-reset-status");
    const pin = readAdminPin(status);

    if (!pin) {
        return;
    }

    button.disabled = true;
    status.className = "privacy-status";
    status.textContent = "Nullstiller …";

    try {
        const response = await fetch(
            "/api/privacy-reset",
            {
                method: "POST",
                headers: adminHeaders("privacy-reset", pin)
            }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.error || "Nullstillingen mislyktes."
            );
        }

        status.className = "privacy-status success";
        status.textContent = data.message;
        await updateDashboard();
    } catch (error) {
        status.className = "privacy-status error";
        status.textContent =
            error.message || "Nullstillingen mislyktes.";
    } finally {
        button.disabled = false;
    }
}

async function updateDashboard() {

    try {

        const response =
            await fetch("/api/status");

        const data =
            await response.json();

        document.getElementById(
            "shots-total"
        ).textContent =
            numberFormatter.format(
                data.shots_total
            );

        const stats = data.statistics;

        document.getElementById(
            "shots-today"
        ).textContent =
            numberFormatter.format(stats.today);

        document.getElementById(
            "shots-yesterday"
        ).textContent =
            numberFormatter.format(stats.yesterday);

        document.getElementById(
            "shots-7-days"
        ).textContent =
            numberFormatter.format(stats.last_7_days);

        document.getElementById(
            "shots-30-days"
        ).textContent =
            numberFormatter.format(stats.last_30_days);

        document.getElementById(
            "shots-365-days"
        ).textContent =
            numberFormatter.format(stats.last_365_days);

        document.getElementById(
            "shots-calendar-year"
        ).textContent =
            numberFormatter.format(stats.this_calendar_year);

        document.getElementById(
            "active-days-30"
        ).textContent =
            `${stats.active_days_30} av 30`;

        document.getElementById(
            "average-active-day"
        ).textContent =
            decimalFormatter.format(
                stats.average_per_active_day_30
            );

        showDayStat(
            "most-active-day-date",
            "most-active-day-shots",
            stats.most_active_day_30
        );

        showDayStat(
            "record-day-date",
            "record-day-shots",
            stats.record_day
        );

        document.getElementById(
            "last-activity-day"
        ).textContent =
            stats.last_activity_day
                ? stats.last_activity_day.date
                : "Ingen aktivitet";

        document.getElementById(
            "status"
        ).textContent =
            data.status.toUpperCase();

        document.getElementById(
            "mode"
        ).textContent =
            data.mode;

        document.getElementById(
            "simulate-button"
        ).hidden =
            data.mode !== "simulated";

        document.getElementById(
            "queued"
        ).textContent =
            data.queued_uploads;

        const privacyMode =
            data.privacy_mode || {active: false};
        const privacyModeButton =
            document.getElementById("privacy-mode-button");
        const privacyModeStatus =
            document.getElementById("privacy-mode-status");

        setToggleState(
            privacyModeButton,
            privacyMode.active
        );

        if (privacyMode.active) {
            privacyModeStatus.className =
                "privacy-status success";
            privacyModeStatus.textContent =
                `Aktiv til ${privacyMode.ends_at.date} ` +
                `kl. ${privacyMode.ends_at.time}.`;
        } else if (!privacyModeStatus.classList.contains("error")) {
            privacyModeStatus.className = "privacy-status";
            privacyModeStatus.textContent =
                "Skjuling av korttidsaktivitet er av.";
        }

        const registrationPause =
            data.registration_pause || {active: false};
        const registrationPauseButton =
            document.getElementById("registration-pause-button");
        const registrationPauseStatus =
            document.getElementById("registration-pause-status");

        setToggleState(
            registrationPauseButton,
            registrationPause.active
        );

        if (registrationPause.active) {
            registrationPauseStatus.dataset.modeState = "active";
            registrationPauseStatus.className =
                "privacy-status error";
            registrationPauseStatus.textContent =
                `Registrering er stoppet til ` +
                `${registrationPause.ends_at.date} ` +
                `kl. ${registrationPause.ends_at.time}.`;
        } else if (
            registrationPauseStatus.dataset.modeState === "active"
            || !registrationPauseStatus.classList.contains("error")
        ) {
            delete registrationPauseStatus.dataset.modeState;
            registrationPauseStatus.className = "privacy-status";
            registrationPauseStatus.textContent =
                "Registrering er aktiv.";
        }

        const systemStatus =
            document.getElementById("status");
        const systemStatusDetail =
            document.getElementById("status-detail");
        const statusDetails = [];

        systemStatus.className = "card-value";

        if (registrationPause.active) {
            systemStatus.textContent = "PAUSE";
            systemStatus.classList.add("status-warning");
            statusDetails.push(
                `Registrering til ${registrationPause.ends_at.date} ` +
                `kl. ${registrationPause.ends_at.time}`
            );

            if (privacyMode.active) {
                statusDetails.push(
                    `Personvern til ${privacyMode.ends_at.date} ` +
                    `kl. ${privacyMode.ends_at.time}`
                );
            }
        } else if (privacyMode.active) {
            systemStatus.textContent = "PERSONVERN";
            systemStatus.classList.add("status-warning");
            statusDetails.push(
                `Til ${privacyMode.ends_at.date} ` +
                `kl. ${privacyMode.ends_at.time}`
            );
        } else {
            systemStatus.textContent = data.status.toUpperCase();
            systemStatus.classList.add("online");
        }

        systemStatusDetail.textContent = statusDetails.join("\\n");

        const audioInput =
            document.getElementById("audio-file");
        const uploadButton =
            document.getElementById("upload-button");
        const uploadStatus =
            document.getElementById("upload-status");

        audioInput.disabled = registrationPause.active;
        uploadButton.disabled =
            registrationPause.active
            || uploadButton.dataset.uploading === "true";

        if (registrationPause.active) {
            uploadStatus.className = "upload-status error";
            uploadStatus.textContent =
                "Opplasting er deaktivert mens registrering er stoppet.";
            uploadStatus.dataset.pauseMessage = "true";
        } else if (uploadStatus.dataset.pauseMessage === "true") {
            uploadStatus.className = "upload-status";
            uploadStatus.textContent = "";
            delete uploadStatus.dataset.pauseMessage;
        }

        document.getElementById(
            "simulate-button"
        ).disabled = registrationPause.active;

        const privacyStatus =
            document.getElementById("privacy-reset-status");

        if (data.privacy_reset) {
            privacyStatus.textContent =
                `Sist nullstilt ${data.privacy_reset.date} ` +
                `kl. ${data.privacy_reset.time}`;
        } else if (!privacyStatus.classList.contains("success")) {
            privacyStatus.textContent =
                "Korttidsvisningen er ikke nullstilt.";
        }

        if (data.last_shot) {
            document.getElementById(
                "last-shot"
            ).textContent =
                data.last_shot.time;
        } else {
            document.getElementById(
                "last-shot"
            ).textContent =
                "Ingen";
        }


        const tbody =
            document.getElementById(
                "recent-shots"
            );

        tbody.innerHTML = "";

        for (const shot of data.recent) {

            const row =
                document.createElement("tr");

            row.innerHTML = `
                <td>${shot.time}</td>
                <td>${shot.date}</td>
                <td>${shot.confidence.toFixed(2)}</td>
                <td>${shot.peak.toFixed(2)}</td>
            `;

            tbody.appendChild(row);
        }

    } catch (error) {

        const status = document.getElementById("status");
        status.className = "card-value offline";
        status.textContent = "OFFLINE";
        document.getElementById("status-detail").textContent = "";

    }
}


async function simulateShot() {

    await fetch(
        "/api/shot",
        {
            method: "POST"
        }
    );

    updateDashboard();
}


updateDashboard();

document.getElementById(
    "upload-form"
).addEventListener(
    "submit",
    uploadAudio
);

document.getElementById(
    "privacy-reset-button"
).addEventListener(
    "click",
    resetPrivacyDisplay
);

document.getElementById(
    "privacy-mode-button"
).addEventListener(
    "click",
    togglePrivacyMode
);

document.getElementById(
    "registration-pause-button"
).addEventListener(
    "click",
    toggleRegistrationPause
);

setInterval(
    updateDashboard,
    3000
);

</script>

</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(HTML)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder,
        "favicon.png",
        mimetype="image/png",
    )


@app.after_request
def discourage_indexing(response):
    response.headers["X-Robots-Tag"] = (
        "noindex, nofollow, noarchive"
    )
    return response


@app.route("/robots.txt")
@app.route("/bots.txt")
def bot_policy():
    return Response(
        "User-agent: *\nDisallow: /\n",
        mimetype="text/plain",
    )


@app.route("/api/status")
def api_status():
    cutoff = privacy_cutoff()
    statistics = activity_statistics(cutoff)
    privacy_mode = privacy_mode_state()
    registration_pause = registration_pause_state()

    return jsonify(
        {
            "status": "online",
            "detector": DETECTOR_NAME,
            "range": RANGE_NAME,
            "shots_total": statistics["total"],
            "queued_uploads": queued_count(),
            "last_shot": last_shot(cutoff),
            "mode": MODE,
            "recent": recent_shots(10, cutoff),
            "statistics": statistics,
            "privacy_reset": privacy_cutoff_payload(cutoff),
            "privacy_mode": privacy_mode,
            "registration_pause": registration_pause,
        }
    )


@app.route("/api/shot", methods=["POST"])
def simulate_shot():
    if registration_pause_state()["active"]:
        return jsonify(
            {
                "ok": False,
                "error": "Registrering er satt på pause.",
            }
        ), 423

    if MODE != "simulated":
        return jsonify(
            {
                "ok": False,
                "error": "Simulering er deaktivert.",
            }
        ), 403

    add_shot(
        confidence=1.0,
        peak=1.0,
    )

    return jsonify(
        {
            "ok": True,
            "shots_total": shot_count(),
        }
    )


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if registration_pause_state()["active"]:
        return jsonify(
            {
                "ok": False,
                "error": "Registrering er satt på pause. Opplasting er deaktivert.",
            }
        ), 423

    audio_file = request.files.get("audio")

    if audio_file is None:
        return jsonify(
            {"ok": False, "error": "Velg en lydfil som skal lastes opp."}
        ), 400

    try:
        destination = queue_audio_upload(audio_file)
    except ValueError as error:
        return jsonify(
            {"ok": False, "error": str(error)}
        ), 400

    return jsonify(
        {
            "ok": True,
            "filename": destination.name,
            "message": "Filen er lastet opp og står i behandlingskø.",
        }
    ), 202


@app.route("/api/privacy-reset", methods=["POST"])
def api_privacy_reset():
    if (
        request.headers.get("X-Shot-Counter-Action")
        != "privacy-reset"
    ) or not valid_admin_pin():
        return jsonify(
            {
                "ok": False,
                "error": "Ugyldig PIN eller forespørsel.",
            }
        ), 403

    cutoff = record_privacy_reset()

    return jsonify(
        {
            "ok": True,
            "privacy_reset": privacy_cutoff_payload(cutoff),
            "message": "Korttidsvisningen er nullstilt.",
        }
    )


@app.route("/api/privacy-mode", methods=["POST"])
def api_privacy_mode():
    if (
        request.headers.get("X-Shot-Counter-Action")
        != "privacy-mode"
    ) or not valid_admin_pin():
        return jsonify(
            {"ok": False, "error": "Ugyldig PIN eller forespørsel."}
        ), 403

    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")

    if not isinstance(enabled, bool):
        return jsonify(
            {"ok": False, "error": "Ugyldig modusverdi."}
        ), 400

    state = set_privacy_mode(enabled)

    return jsonify(
        {
            "ok": True,
            "privacy_mode": state,
            "message": (
                "Korttidsaktivitet skjules."
                if state["active"]
                else "Skjuling av korttidsaktivitet er avsluttet."
            ),
        }
    )


@app.route("/api/registration-pause", methods=["POST"])
def api_registration_pause():
    if (
        request.headers.get("X-Shot-Counter-Action")
        != "registration-pause"
    ) or not valid_admin_pin():
        return jsonify(
            {"ok": False, "error": "Ugyldig PIN eller forespørsel."}
        ), 403

    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")

    if not isinstance(enabled, bool):
        return jsonify(
            {"ok": False, "error": "Ugyldig modusverdi."}
        ), 400

    state = set_registration_pause(enabled)

    return jsonify(
        {
            "ok": True,
            "registration_pause": state,
            "message": (
                "Registrering er satt på pause."
                if state["active"]
                else "Registrering er startet igjen."
            ),
        }
    )


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify(
        {
            "ok": False,
            "error": "Filen er for stor. Maksimal størrelse er 95 MB.",
        }
    ), 413


@app.route("/api/shots")
def api_shots():
    return jsonify(recent_shots(100, privacy_cutoff()))


def simulator():
    print("Simulation mode enabled.")
    print("Automatic test shot every 30 seconds.")

    while True:
        time.sleep(30)

        add_shot(
            confidence=0.99,
            peak=0.95,
        )


if __name__ == "__main__":

    init_db()

    if MODE == "simulated":

        thread = threading.Thread(
            target=simulator,
            daemon=True,
        )

        thread.start()

    app.run(
        host=config["api"]["host"],
        port=config["api"]["port"],
    )

