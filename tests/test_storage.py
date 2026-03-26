from pathlib import Path
import tempfile
import unittest

from tsundoku import models, storage


class StorageTests(unittest.TestCase):
    def test_prefs_and_links_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = storage.StorageManager(store_dir=Path(tmpdir))
            prefs = mgr.load_prefs()
            self.assertEqual(prefs["sort_mode"], "date-desc")

            mgr.update_prefs(sort_mode="relevance-desc")
            self.assertEqual(mgr.load_prefs()["sort_mode"], "relevance-desc")

            link = models.create_link("https://example.com", "demo").to_dict()
            self.assertTrue(mgr.save_links([link]))
            loaded = mgr.load_links()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
