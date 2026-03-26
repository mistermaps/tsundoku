import tempfile
import unittest
from unittest import mock

from tsundoku import config


class ConfigTests(unittest.TestCase):
    def test_save_and_load_config_uses_override_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TSUNDOKU_CONFIG_HOME": tmpdir,
                "TSUNDOKU_DATA_HOME": tmpdir,
            }
            with mock.patch.dict("os.environ", env, clear=False):
                config.reset_cache()
                cfg = config.create_default_config()
                cfg.data_dir = ""
                saved_path = config.save_config(cfg)
                self.assertEqual(saved_path, config.default_config_path())
                loaded = config.load_config()
                self.assertEqual(loaded.active_profile, "default")
                self.assertEqual(config.get_data_dir(loaded), config.default_data_dir())


if __name__ == "__main__":
    unittest.main()
