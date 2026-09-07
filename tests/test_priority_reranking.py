import unittest


def _load_module():
    from mbd_api import core

    return core


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
