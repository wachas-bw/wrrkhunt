from __future__ import annotations

import unittest
from pathlib import Path

from prospecting.config import DEFAULT_SETTINGS


ROOT = Path(__file__).resolve().parents[1]


class DocumentationContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_agent_entry_points_route_to_the_canonical_guide(self):
        for name in ("README.md", "AGENTS.md", "CLAUDE.md"):
            self.assertIn("TEAM-PROSPECTING-GUIDE.md", self.read(name))
        self.assertTrue(self.read("CLAUDE.md").startswith("@AGENTS.md"))

    def test_documented_defaults_match_runtime_defaults(self):
        guide = self.read("TEAM-PROSPECTING-GUIDE.md")
        agents = self.read("AGENTS.md")
        self.assertIn(f"fit score at or above {DEFAULT_SETTINGS['fit_threshold']}", guide)
        self.assertIn(f"daily cap is {DEFAULT_SETTINGS['email_daily_cap']}", guide)
        self.assertIn(f"cap is {DEFAULT_SETTINGS['linkedin_daily_cap']}", guide)
        self.assertIn(DEFAULT_SETTINGS["email_copy_style"], guide)
        self.assertIn(DEFAULT_SETTINGS["email_copy_style"], agents)
        self.assertIn(DEFAULT_SETTINGS["email_booking_url"], guide)

    def test_method_catalog_keeps_high_risk_social_paths_non_production(self):
        guide = self.read("TEAM-PROSPECTING-GUIDE.md")
        for phrase in (
            "X/Twitter intent search",
            "Threads keyword-post harvesting",
            "Telegram hiring-channel ingestion",
            "Automated LinkedIn browser control",
            "Guessed email patterns or synthesized addresses",
        ):
            self.assertIn(phrase, guide)
        self.assertIn("Prohibited", guide)
        self.assertIn("Historical and not approved for wrrkhunt", guide)

    def test_historical_status_files_warn_against_live_state_use(self):
        self.assertIn("Historical architecture snapshot", self.read("SYSTEM-OVERVIEW.md"))
        self.assertIn("Historical snapshot", self.read("TODAY.md"))


if __name__ == "__main__":
    unittest.main()
