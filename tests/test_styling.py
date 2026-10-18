import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from styling import rewrite


class StyleTests(unittest.TestCase):
    def test_local_response_and_review_payload(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            result = {"choices": [{"message": {"content": "Ну привет, рада тебя видеть! 😊"}}]}
            return io.BytesIO(json.dumps(result).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text, used_ai = rewrite("привет", "kind", ["Как ты там?"], "Доброе утро")
        self.assertEqual(text, "Ну привет, рада тебя видеть! 😊")
        self.assertTrue(used_ai)
        self.assertEqual(requests[0].full_url, "http://127.0.0.1:18765/v1/chat/completions")
        payload = json.loads(requests[0].data)
        self.assertIn("Добрый", payload["messages"][0]["content"])
        self.assertIn("вводные слова", payload["messages"][0]["content"])
        self.assertIn("эмодзи", payload["messages"][0]["content"])
        self.assertIn("Как ты там?", payload["messages"][1]["content"])
        self.assertNotIn("api_key", payload)


if __name__ == "__main__":
    unittest.main()
