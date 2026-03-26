import tempfile
import unittest
from unittest import mock

from tsundoku import config, workflows


class WorkflowTests(unittest.TestCase):
    def test_analysis_missing_fields_detects_incomplete_payload(self):
        link = {
            "id": "lnk-1234",
            "url": "https://example.com",
            "status": "implemented",
            "title": "Example",
            "analysis": '{"title":"Example","technologies":["python"],"relevance_score":4,"integration_ideas":["Idea"]}',
        }
        missing = workflows._analysis_missing_fields(link)
        self.assertIn("summary", missing)

    def test_ensure_setup_creates_default_config_non_interactively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TSUNDOKU_CONFIG_HOME": tmpdir,
                "TSUNDOKU_DATA_HOME": tmpdir,
            }
            with mock.patch.dict("os.environ", env, clear=False):
                config.reset_cache()
                created = workflows._ensure_setup(interactive=False)
                self.assertEqual(created.active_profile, "default")
                self.assertTrue(config.default_config_path().exists())


if __name__ == "__main__":
    unittest.main()
