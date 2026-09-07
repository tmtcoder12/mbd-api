import threading
import time
import unittest


def _load_module():
    from mbd_api import core

    return core


class _FakeQueryCache:
    def __init__(self):
        self.exact_map = {}
        self.normalized_map = {}
        self.semantic_map = {}
        self.set_calls = 0

    def lookup_exact(self, group_prefix, exact_query_hash):
        return self.exact_map.get((group_prefix, exact_query_hash))

    def lookup_normalized(self, group_prefix, normalized_query_hash):
        return self.normalized_map.get((group_prefix, normalized_query_hash))

    def semantic_candidates(self, group_prefix, max_candidates):
        rows = list(self.semantic_map.get(group_prefix, []))
        return rows[:max_candidates]

    def store_entry(self, group_prefix, exact_query_hash, normalized_query_hash, payload, ttl_seconds):
        self.set_calls += 1
        self.exact_map[(group_prefix, exact_query_hash)] = payload
        self.normalized_map[(group_prefix, normalized_query_hash)] = payload
        self.semantic_map.setdefault(group_prefix, []).insert(0, payload)


class _FakeRetriever:
    def __init__(self):
        self.calls = 0
        self.query_embeddings = []

    def retrieve(self, client, user_q, top_k, min_score, restaurant_id=None, query_embedding=None):
        self.calls += 1
        self.query_embeddings.append(query_embedding)
        return {
            "results": [
                {
                    "id": "hours-1",
                    "text": "We close at 10 PM.",
                    "title": "Hours",
                    "score": 0.9,
                    "type": "policy",
                    "source_url": "",
                    "page_path": "/hours",
                    "image_url": "",
                    "extra_metadata": {},
                }
            ],
            "timing_start": 0.0,
        }


class _VecRow:
    def __init__(self, vals):
        self._vals = vals

    def tolist(self):
        return list(self._vals)


class _Vec:
    def __init__(self, vals):
        self._row = _VecRow(vals)

    def __getitem__(self, idx):
        return self._row


class QueryCacheLlmGatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _base_session_state(self):
        return {
            "session_id": "s1",
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }

    def test_classifier_starts_before_semantic_stage(self):
        mod = self.mod
        cache = _FakeQueryCache()
        retriever = _FakeRetriever()
        classifier_started = threading.Event()

        orig_classify = mod.classify_query_cacheability
        orig_embed = mod.embed_query
        orig_create = mod.create_assistant_response
        try:

            def _classify(client, model, user_query):
                classifier_started.set()
                time.sleep(0.02)
                return True, "CACHEABLE", {"response_id": "r1", "latency_ms": 1, "raw_output_len": 9}

            def _embed(client, text):
                self.assertTrue(classifier_started.wait(0.2))
                return _Vec([1.0, 0.0])

            mod.classify_query_cacheability = _classify
            mod.embed_query = _embed
            mod.create_assistant_response = lambda *args, **kwargs: (
                "We close at 10 PM.",
                "resp1",
                {"include_images": False, "max_images": 0, "target_item_names": []},
            )
            mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=self._base_session_state(),
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=200,
                request_id="req-semantic-order",
            )
        finally:
            mod.classify_query_cacheability = orig_classify
            mod.embed_query = orig_embed
            mod.create_assistant_response = orig_create

        self.assertEqual(retriever.calls, 1)
        self.assertEqual(cache.set_calls, 1)

    def test_cache_store_when_classifier_cacheable(self):
        mod = self.mod
        cache = _FakeQueryCache()
        retriever = _FakeRetriever()

        orig_classify = mod.classify_query_cacheability
        orig_embed = mod.embed_query
        orig_create = mod.create_assistant_response
        try:
            mod.classify_query_cacheability = lambda *args, **kwargs: (
                True,
                "CACHEABLE",
                {"response_id": "r2", "latency_ms": 1, "raw_output_len": 9},
            )
            mod.embed_query = lambda *args, **kwargs: _Vec([1.0, 0.0])
            mod.create_assistant_response = lambda *args, **kwargs: (
                "We close at 10 PM.",
                "resp2",
                {"include_images": False, "max_images": 0, "target_item_names": []},
            )
            turn = mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=self._base_session_state(),
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=200,
                request_id="req-store-yes",
            )
        finally:
            mod.classify_query_cacheability = orig_classify
            mod.embed_query = orig_embed
            mod.create_assistant_response = orig_create

        self.assertFalse(turn.get("cache_hit"))
        self.assertEqual(cache.set_calls, 1)

    def test_cache_store_skipped_when_classifier_not_cacheable(self):
        mod = self.mod
        cache = _FakeQueryCache()
        retriever = _FakeRetriever()

        orig_classify = mod.classify_query_cacheability
        orig_embed = mod.embed_query
        orig_create = mod.create_assistant_response
        try:
            mod.classify_query_cacheability = lambda *args, **kwargs: (
                False,
                "NOT_CACHEABLE",
                {"response_id": "r3", "latency_ms": 1, "raw_output_len": 13},
            )
            mod.embed_query = lambda *args, **kwargs: _Vec([1.0, 0.0])
            mod.create_assistant_response = lambda *args, **kwargs: (
                "We close at 10 PM.",
                "resp3",
                {"include_images": False, "max_images": 0, "target_item_names": []},
            )
            mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=self._base_session_state(),
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=200,
                request_id="req-store-no",
            )
        finally:
            mod.classify_query_cacheability = orig_classify
            mod.embed_query = orig_embed
            mod.create_assistant_response = orig_create

        self.assertEqual(cache.set_calls, 0)

    def test_cache_store_skipped_on_classifier_timeout(self):
        mod = self.mod
        cache = _FakeQueryCache()
        retriever = _FakeRetriever()

        orig_classify = mod.classify_query_cacheability
        orig_embed = mod.embed_query
        orig_create = mod.create_assistant_response
        try:

            def _slow_classify(*args, **kwargs):
                time.sleep(0.1)
                return True, "CACHEABLE", {"response_id": "r4", "latency_ms": 100, "raw_output_len": 9}

            mod.classify_query_cacheability = _slow_classify
            mod.embed_query = lambda *args, **kwargs: _Vec([1.0, 0.0])
            mod.create_assistant_response = lambda *args, **kwargs: (
                "We close at 10 PM.",
                "resp4",
                {"include_images": False, "max_images": 0, "target_item_names": []},
            )
            mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=self._base_session_state(),
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=5,
                request_id="req-timeout",
            )
        finally:
            mod.classify_query_cacheability = orig_classify
            mod.embed_query = orig_embed
            mod.create_assistant_response = orig_create

        self.assertEqual(cache.set_calls, 0)

    def test_semantic_cache_miss_reuses_embedding_for_retrieval(self):
        mod = self.mod
        cache = _FakeQueryCache()
        retriever = _FakeRetriever()
        embed_calls = []

        orig_classify = mod.classify_query_cacheability
        orig_embed = mod.embed_query
        orig_create = mod.create_assistant_response
        try:
            mod.classify_query_cacheability = lambda *args, **kwargs: (
                False,
                "NOT_CACHEABLE",
                {"response_id": "r-embed", "latency_ms": 1, "raw_output_len": 13},
            )

            def _embed(client, text):
                embed_calls.append(text)
                return _Vec([1.0, 0.0])

            mod.embed_query = _embed
            mod.create_assistant_response = lambda *args, **kwargs: (
                "We close at 10 PM.",
                "resp-embed",
                {"include_images": False, "max_images": 0, "target_item_names": []},
            )
            mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=self._base_session_state(),
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=200,
                request_id="req-embed-reuse",
            )
        finally:
            mod.classify_query_cacheability = orig_classify
            mod.embed_query = orig_embed
            mod.create_assistant_response = orig_create

        self.assertEqual(embed_calls, ["When do you close?"])
        self.assertEqual(retriever.calls, 1)
        self.assertEqual(retriever.query_embeddings, [[1.0, 0.0]])

    def test_exact_hit_returns_without_waiting_for_classifier(self):
        mod = self.mod
        cache = _FakeQueryCache()
        session_state = self._base_session_state()
        restaurant_id = "11111111-1111-4111-8111-111111111111"
        query = "When do you close?"
        context_hash = mod.build_query_cache_context_hash(
            restaurant_id=restaurant_id,
            system_instructions="Prompt A",
            top_k=8,
            min_score=0.0,
            model=mod.CHAT_MODEL,
        )
        group_prefix = mod.build_query_cache_group_prefix("qcache:v1", restaurant_id, context_hash)
        cache.exact_map[(group_prefix, mod._hash_text(query))] = {
            "assistant_text": "Cached: We close at 10 PM.",
            "results": [],
            "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
            "new_session_state": dict(session_state),
        }

        class _MustNotRetrieve:
            def retrieve(self, *args, **kwargs):
                raise AssertionError("retrieval should not run on exact cache hit")

        classifier_started = threading.Event()
        orig_classify = mod.classify_query_cacheability
        try:

            def _slow_classify(*args, **kwargs):
                classifier_started.set()
                time.sleep(0.1)
                return True, "CACHEABLE", {"response_id": "r5", "latency_ms": 100, "raw_output_len": 9}

            mod.classify_query_cacheability = _slow_classify
            t0 = time.perf_counter()
            turn = mod.handle_chat_turn(
                client=object(),
                retriever=_MustNotRetrieve(),
                store=None,
                restaurant_id=restaurant_id,
                user_query=query,
                system_instructions="Prompt A",
                session_state=session_state,
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_classifier_timeout_ms=250,
                request_id="req-exact-hit",
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.assertTrue(classifier_started.wait(0.2))
        finally:
            mod.classify_query_cacheability = orig_classify

        self.assertTrue(turn.get("cache_hit"))
        self.assertEqual(turn.get("cache_hit_stage"), "exact")
        self.assertLess(elapsed_ms, 80.0)


if __name__ == "__main__":
    unittest.main()
