import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = PROJECT_ROOT / "locales"


class LocalizationTests(unittest.TestCase):
    def test_language_packs_have_matching_keys(self):
        with (LOCALES_DIR / "en.json").open(encoding="utf-8") as f:
            english = json.load(f)

        with (LOCALES_DIR / "nb.json").open(encoding="utf-8") as f:
            norwegian = json.load(f)

        self.assertEqual(
            set(english["strings"]),
            set(norwegian["strings"]),
        )

    def render_language(self, language):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.yaml"
            config = {
                "timezone": "Europe/Oslo",
                "ui": {
                    "language": language,
                    "title": "Test Counter",
                },
                "detector": {
                    "name": "test-detector",
                    "range": "test-range",
                    "mode": "uploaded",
                    "allow_simulation": False,
                },
                "database": {"path": str(root / "shots.db")},
                "admin": {"pin": "test-pin"},
                "privacy": {
                    "mode_hours": 6,
                    "publish_delay_min_hours": 24,
                    "publish_delay_max_hours": 48,
                    "registration_pause_hours": 24,
                },
                "api": {"host": "127.0.0.1", "port": 8080},
                "uploads": {
                    "incoming": str(root / "incoming"),
                    "max_bytes": 95 * 1024 * 1024,
                },
            }

            with config_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(config, f)

            probe = """
import json
import app

app.init_db()
app.add_shot()
client = app.app.test_client()
page = client.get('/').get_data(as_text=True)
status = client.get('/api/status').get_json()
denied = client.post(
    '/api/privacy-reset',
    headers={
        'X-Shot-Counter-PIN': 'wrong',
        'X-Shot-Counter-Action': 'privacy-reset',
    },
).get_json()
print(json.dumps({
    'page': page,
    'date': status['last_shot']['date'],
    'denied': denied['error'],
}))
"""
            environment = os.environ.copy()
            environment["SHOT_COUNTER_CONFIG"] = str(config_path)
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=PROJECT_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_english_is_rendered_and_formats_dates(self):
        result = self.render_language("en")

        self.assertIn('<html lang="en">', result["page"])
        self.assertIn("TOTAL REGISTERED", result["page"])
        self.assertNotIn("TOTALT REGISTRERT", result["page"])
        self.assertRegex(result["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(result["denied"], "Invalid PIN or request.")

    def test_norwegian_is_rendered_and_formats_dates(self):
        result = self.render_language("nb")

        self.assertIn('<html lang="nb">', result["page"])
        self.assertIn("TOTALT REGISTRERT", result["page"])
        self.assertIn("Personvern", result["page"])
        self.assertRegex(result["date"], r"^\d{2}\.\d{2}\.\d{4}$")
        self.assertEqual(
            result["denied"],
            "Ugyldig PIN eller forespørsel.",
        )


if __name__ == "__main__":
    unittest.main()

