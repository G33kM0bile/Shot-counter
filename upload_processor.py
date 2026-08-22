#!/usr/bin/env python3

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("SHOT_COUNTER_CONFIG", APP_ROOT / "config.yaml")
)

SUPPORTED_EXTENSIONS = {".wav", ".m4a", ".mp3", ".aac"}


with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
    CONFIG = yaml.safe_load(config_file)

PROCESSING = CONFIG.get("processing", {})
SCAN_INTERVAL = int(PROCESSING.get("scan_interval_seconds", 5))
STABLE_CHECKS = int(PROCESSING.get("stable_checks", 3))
STABLE_DELAY = float(PROCESSING.get("stable_delay_seconds", 2))
RMS_WINDOW_MS = float(PROCESSING.get("rms_window_ms", 10))
THRESHOLD_MULTIPLIER = float(
    PROCESSING.get("threshold_multiplier", 15)
)
MINIMUM_THRESHOLD = float(PROCESSING.get("minimum_threshold", 0.02))
CLUSTER_GAP_MS = float(PROCESSING.get("cluster_gap_ms", 300))

UPLOAD_ROOT = Path(
    CONFIG.get("uploads", {}).get("root", APP_ROOT / "uploads")
)
INCOMING_DIR = Path(
    CONFIG.get("uploads", {}).get(
        "incoming",
        UPLOAD_ROOT / "incoming",
    )
)
PROCESSED_DIR = UPLOAD_ROOT / "processed"
FAILED_DIR = UPLOAD_ROOT / "failed"
WORK_DIR = Path(
    CONFIG.get("processing", {}).get("work_dir", APP_ROOT / "work")
)

DB_PATH = Path(CONFIG["database"]["path"])
DETECTOR_NAME = CONFIG["detector"]["name"]
RANGE_NAME = CONFIG["detector"]["range"]


for directory in (
    INCOMING_DIR,
    PROCESSED_DIR,
    FAILED_DIR,
    WORK_DIR,
    DB_PATH.parent,
):
    directory.mkdir(parents=True, exist_ok=True)


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
            CREATE TABLE IF NOT EXISTS processed_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                recording_started TEXT NOT NULL,
                timestamp_source TEXT NOT NULL,
                detected_events INTEGER NOT NULL,
                processed_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


def already_processed(source_hash):
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM processed_uploads
            WHERE source_hash = ?
            """,
            (source_hash,),
        ).fetchone()

    return row is not None


def wait_until_stable(path):
    previous_size = -1
    stable_count = 0

    while stable_count < STABLE_CHECKS:
        if not path.exists():
            return False

        try:
            current_size = path.stat().st_size
        except OSError:
            return False

        if current_size == previous_size and current_size > 0:
            stable_count += 1
        else:
            stable_count = 0

        previous_size = current_size
        time.sleep(STABLE_DELAY)

    return True


def unique_destination(directory, filename):
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    source = Path(filename)
    counter = 1

    while True:
        candidate = directory / (
            f"{source.stem}-{counter}{source.suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


def move_file(source, directory):
    destination = unique_destination(directory, source.name)
    shutil.move(str(source), str(destination))
    return destination


def metadata_recording_time(path):
    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "format_tags=creation_time:stream_tags=creation_time",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        metadata = json.loads(result.stdout)

        candidates = []

        format_tags = metadata.get("format", {}).get("tags", {})
        candidates.append(format_tags.get("creation_time"))

        for stream in metadata.get("streams", []):
            candidates.append(
                stream.get("tags", {}).get("creation_time")
            )

        for value in candidates:
            if not value:
                continue

            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

    except Exception as error:
        print(
            f"Metadata timestamp unavailable for {path.name}: "
            f"{error}",
            flush=True,
        )

    return None


def recording_time(path):
    embedded = metadata_recording_time(path)

    if embedded is not None:
        return embedded, "embedded_metadata"

    modified = datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=timezone.utc,
    )

    return modified, "filesystem_mtime"


def normalize_audio(source, destination):
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]

    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=3600,
    )


def calculate_rms(audio, window):
    squared = np.asarray(audio, dtype=np.float64) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))

    window_sums = cumulative[window:] - cumulative[:-window]
    values = np.sqrt(window_sums / window)

    left = window // 2
    right = len(audio) - len(values) - left

    return np.pad(values, (left, right), mode="edge")


def detect_events(wav_path):
    audio, sample_rate = sf.read(wav_path)

    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    if len(audio) == 0:
        return [], 0.0, sample_rate

    absolute_audio = np.abs(audio)

    window = max(1, int(sample_rate * RMS_WINDOW_MS / 1000))
    rms = calculate_rms(audio, window)

    threshold = max(
        float(np.median(rms)) * THRESHOLD_MULTIPLIER,
        MINIMUM_THRESHOLD,
    )
    indices = np.where(rms > threshold)[0]

    events = []

    if len(indices) == 0:
        return events, threshold, sample_rate

    start = int(indices[0])
    last = int(indices[0])
    cluster_gap = int(sample_rate * CLUSTER_GAP_MS / 1000)

    def append_event(event_start, event_end):
        segment = slice(event_start, event_end + 1)

        peak_index = (
            event_start
            + int(np.argmax(absolute_audio[segment]))
        )

        events.append(
            {
                "offset": peak_index / sample_rate,
                "peak": float(absolute_audio[peak_index]),
                "energy": float(rms[peak_index]),
            }
        )

    for index in indices[1:]:
        index = int(index)

        if index - last > cluster_gap:
            append_event(start, last)
            start = index

        last = index

    append_event(start, last)

    return events, threshold, sample_rate


def store_results(
    source_name,
    source_hash,
    recording_started,
    timestamp_source,
    events,
    threshold,
):
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT id
            FROM processed_uploads
            WHERE source_hash = ?
            """,
            (source_hash,),
        ).fetchone()

        if existing:
            conn.rollback()
            return False

        for event in events:
            timestamp = (
                recording_started
                + timedelta(seconds=event["offset"])
            ).isoformat()

            confidence = min(
                1.0,
                event["energy"] / threshold,
            )

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
                    event["peak"],
                ),
            )

        conn.execute(
            """
            INSERT INTO processed_uploads (
                source_name,
                source_hash,
                recording_started,
                timestamp_source,
                detected_events,
                processed_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_name,
                source_hash,
                recording_started.isoformat(),
                timestamp_source,
                len(events),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        conn.commit()

    return True


def eligible_file(path):
    try:
        path = path.resolve()
    except OSError:
        return False

    if not path.is_file():
        return False

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False

    if PROCESSED_DIR.resolve() in path.parents:
        return False

    if FAILED_DIR.resolve() in path.parents:
        return False

    return True


def process_file(path):
    path = Path(path)

    if not eligible_file(path):
        return

    print(f"Found upload: {path}", flush=True)

    if not wait_until_stable(path):
        print(f"Upload disappeared before completion: {path}", flush=True)
        return

    work_wav = WORK_DIR / (
        f"{int(time.time() * 1000)}-{path.stem}.wav"
    )

    try:
        source_hash = sha256_file(path)

        if already_processed(source_hash):
            destination = move_file(path, PROCESSED_DIR)

            print(
                f"Duplicate audio skipped: {path.name} "
                f"-> {destination}",
                flush=True,
            )
            return

        started, timestamp_source = recording_time(path)

        normalize_audio(path, work_wav)

        events, threshold, sample_rate = detect_events(work_wav)

        stored = store_results(
            source_name=path.name,
            source_hash=source_hash,
            recording_started=started,
            timestamp_source=timestamp_source,
            events=events,
            threshold=threshold,
        )

        destination = move_file(path, PROCESSED_DIR)

        if stored:
            print(
                f"Processed {path.name}: "
                f"{len(events)} event(s), "
                f"sample_rate={sample_rate}, "
                f"threshold={threshold:.6f}, "
                f"timestamp_source={timestamp_source}, "
                f"moved_to={destination}",
                flush=True,
            )
        else:
            print(
                f"Duplicate detected during database write: "
                f"{path.name}",
                flush=True,
            )

    except Exception as error:
        print(
            f"FAILED {path}: {type(error).__name__}: {error}",
            flush=True,
        )

        if path.exists():
            try:
                destination = move_file(path, FAILED_DIR)
                print(f"Moved failed file to {destination}", flush=True)
            except Exception as move_error:
                print(
                    f"Could not move failed file: {move_error}",
                    flush=True,
                )

    finally:
        try:
            work_wav.unlink(missing_ok=True)
        except OSError:
            pass


def initial_scan():
    candidates = []

    for directory in (UPLOAD_ROOT, INCOMING_DIR):
        for path in directory.iterdir():
            if eligible_file(path):
                candidates.append(path)

    candidates.sort(key=lambda item: item.stat().st_mtime)

    for path in candidates:
        process_file(path)


class UploadHandler(FileSystemEventHandler):
    def handle(self, path):
        path = Path(path)

        if eligible_file(path):
            process_file(path)

    def on_created(self, event):
        if not event.is_directory:
            self.handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.handle(event.dest_path)


def main():
    init_db()

    print(
        f"Upload processor started. Watching {UPLOAD_ROOT}",
        flush=True,
    )

    initial_scan()

    observer = Observer()
    handler = UploadHandler()

    observer.schedule(
        handler,
        str(UPLOAD_ROOT),
        recursive=True,
    )

    observer.start()

    try:
        while True:
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()

