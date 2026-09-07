import importlib.util
import pathlib
import sys
import types
import unittest


def _load_module():
    if "numpy" not in sys.modules:
        try:
            import numpy as real_np

            sys.modules["numpy"] = real_np
        except ModuleNotFoundError:
            fake_np = types.ModuleType("numpy")
            fake_np.array = lambda v, dtype=None: v
            fake_np.float32 = float
            fake_np.clip = lambda a, b, c: a
            fake_np.dot = lambda a, b: sum(float(x) * float(y) for x, y in zip(a, b))
            fake_np.linalg = types.SimpleNamespace(
                norm=lambda v, axis=None, keepdims=None: (sum(float(x) * float(x) for x in v) ** 0.5)
            )
            sys.modules["numpy"] = fake_np
    if "dotenv" not in sys.modules:
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = fake_dotenv
    if "openai" not in sys.modules:
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = object
        sys.modules["openai"] = fake_openai
    if "stripe" not in sys.modules:
        fake_stripe = types.ModuleType("stripe")
        fake_stripe.Webhook = types.SimpleNamespace(construct_event=lambda *args, **kwargs: {})
        fake_stripe.Subscription = types.SimpleNamespace(retrieve=lambda *args, **kwargs: {})
        sys.modules["stripe"] = fake_stripe

    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "rag-chatbot.py"
    spec = importlib.util.spec_from_file_location("rag_chatbot_priority_reranking_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load rag-chatbot.py for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeRetriever:
    def __init__(self, rows):
        self.rows = list(rows)

    def retrieve(self, client, user_q, top_k, min_score, restaurant_id=None, query_embedding=None):
        return {"results": list(self.rows), "timing_start": 0.0}


class PriorityRerankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _session_state(self):
        return {
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
        }

    def test_priority_score_boost_can_outrank_higher_similarity_item(self):
        rows = [
            {
                "id": "low-priority",
                "title": "Regular Beef",
                "text": "Regular Beef",
                "score": 0.90,
                "extra_metadata": {"priority_score": 0},
            },
            {
                "id": "high-priority",
                "title": "Deep Fried Beef & Szechuan Sauce",
                "text": "Deep Fried Beef & Szechuan Sauce",
                "score": 0.82,
                "extra_metadata": {"priority_score": 2},
            },
        ]

        result = self.mod.retrieve_menu_items(
            retriever=_FakeRetriever(rows),
            client=object(),
            restaurant_id="11111111-1111-4111-8111-555555555555",
            retrieval_query="beef",
            top_k=2,
            min_score=0.0,
            session_state=self._session_state(),
            active_constraints={},
        )

        self.assertEqual(result["results"][0]["id"], "high-priority")
        self.assertAlmostEqual(result["results"][0]["priority_score"], 2.0)
        self.assertAlmostEqual(result["results"][0]["priority_boost"], 0.10)
        self.assertIn("priority_score_boost", result["results"][0]["boost_reason"])

    def test_missing_and_malformed_priority_score_fall_back_to_zero(self):
        rows = [
            {
                "id": "missing",
                "title": "Missing Priority",
                "text": "Missing Priority",
                "score": 0.80,
                "extra_metadata": {},
            },
            {
                "id": "malformed",
                "title": "Malformed Priority",
                "text": "Malformed Priority",
                "score": 0.70,
                "extra_metadata": {"priority_score": "not-a-number"},
            },
        ]

        result = self.mod.retrieve_menu_items(
            retriever=_FakeRetriever(rows),
            client=object(),
            restaurant_id="11111111-1111-4111-8111-555555555555",
            retrieval_query="priority",
            top_k=2,
            min_score=0.0,
            session_state=self._session_state(),
            active_constraints={},
        )

        self.assertEqual([row["id"] for row in result["results"]], ["missing", "malformed"])
        self.assertEqual([row["priority_score"] for row in result["results"]], [0.0, 0.0])
        self.assertEqual([row["priority_boost"] for row in result["results"]], [0.0, 0.0])
        self.assertNotIn("priority_score_boost", result["results"][0]["boost_reason"])
        self.assertNotIn("priority_score_boost", result["results"][1]["boost_reason"])


if __name__ == "__main__":
    unittest.main()
