import json
import unittest
from unittest import mock

from tsundoku import backend, config


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BackendTests(unittest.TestCase):
    def test_send_message_applies_auth_and_normalizes_response(self):
        profile = config.BackendProfile(
            name="demo",
            base_url="https://example.test",
            message_path="/message/{agent}",
            auth=config.AuthConfig(type="bearer", env_var="TSUNDOKU_TOKEN"),
        )
        client = backend.BackendClient(profile)
        seen = {}

        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["authorization"] = request.headers.get("Authorization")
            seen["timeout"] = timeout
            return _FakeResponse({"response": "ok", "model": "demo-model"})

        with mock.patch.dict("os.environ", {"TSUNDOKU_TOKEN": "secret"}):
            with mock.patch("tsundoku.backend.urlopen", side_effect=fake_urlopen):
                result = client.send_message("analyst", "hello", timeout=12, max_retries=0)

        envelope = client.normalize_message_response(result)
        self.assertEqual(seen["url"], "https://example.test/message/analyst")
        self.assertEqual(seen["authorization"], "Bearer secret")
        self.assertEqual(seen["timeout"], 12)
        self.assertEqual(envelope.text, "ok")
        self.assertEqual(envelope.model, "demo-model")


if __name__ == "__main__":
    unittest.main()
