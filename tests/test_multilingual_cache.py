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

    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "rag-chatbot.py"
    spec = importlib.util.spec_from_file_location("rag_chatbot_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load rag-chatbot.py for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    def retrieve(self, client, user_q, top_k, min_score, restaurant_id=None, query_embedding=None):
        self.calls += 1
        return {"results": list(self.rows), "timing_start": 0.0}


class MultilingualCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_query_cache_eligibility_allows_non_latin_unknown_relevance_lookup(self):
        ok, reason = self.mod.query_cache_eligibility(
            "आप कब बंद करते हैं?",
            session_state={"last_response_id": None},
            resolved_reference={"status": "none"},
            inferred_intent=None,
            require_restaurant_relevance=True,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "relevance_unknown_lookup_allowed")

    def test_system_instruction_cache_normalization_ignores_language_code(self):
        eng = self.mod._normalize_system_instructions_for_cache(
            "Answer in language: eng",
            query_language="eng",
            multilingual_enabled=True,
        )
        cmn = self.mod._normalize_system_instructions_for_cache(
            "Answer in language: cmn",
            query_language="cmn",
            multilingual_enabled=True,
        )
        self.assertEqual(eng, cmn)

    def test_store_blocked_when_unknown_relevance_and_low_retrieval_score(self):
        cache = _FakeQueryCache()
        retriever = _FakeRetriever(
            [
                {
                    "id": "c1",
                    "text": "Parking information",
                    "title": "Parking",
                    "score": 0.10,
                    "type": "policy",
                    "source_url": "",
                    "page_path": "/visit",
                    "image_url": "",
                    "extra_metadata": {},
                }
            ]
        )
        session_state = {
            "session_id": "s1",
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }

        orig_embed = self.mod.embed_query
        orig_create = self.mod.create_assistant_response
        try:
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

            self.mod.embed_query = lambda client, text: _Vec([1.0, 0.0])
            self.mod.create_assistant_response = (
                lambda client, system_instructions, user_query, menu_context, session_context, previous_response_id:
                ("हम 10 बजे बंद करते हैं।", "resp_hin_1", {"include_images": False, "max_images": 0, "target_item_names": []})
            )
            turn = self.mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id="11111111-1111-4111-8111-111111111111",
                user_query="आप कब बंद करते हैं?",
                system_instructions="Prompt A",
                session_state=session_state,
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_namespace="qcache:v1",
                query_cache_ttl_seconds=900,
                query_cache_store_min_retrieval_score=0.25,
                query_language="hin",
                request_id="req-low-score",
            )
        finally:
            self.mod.embed_query = orig_embed
            self.mod.create_assistant_response = orig_create

        self.assertFalse(turn.get("cache_hit"))
        self.assertEqual(retriever.calls, 1)
        self.assertEqual(cache.set_calls, 0)

    def test_cross_language_semantic_hit_regenerates_response(self):
        cache = _FakeQueryCache()
        session_state = {
            "session_id": "s1",
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }
        restaurant_id = "11111111-1111-4111-8111-111111111111"
        context_hash = self.mod.build_query_cache_context_hash(
            restaurant_id=restaurant_id,
            system_instructions="Prompt A",
            top_k=8,
            min_score=0.0,
            model="gpt-5-mini",
        )
        group_prefix = self.mod.build_query_cache_group_prefix("qcache:v1", restaurant_id, context_hash)
        cache.semantic_map[group_prefix] = [
            {
                "assistant_text": "We close at 10 PM.",
                "results": [
                    {
                        "id": "p1",
                        "text": "We are open daily and close at 10 PM.",
                        "title": "Hours",
                        "score": 0.9,
                        "type": "policy",
                        "source_url": "",
                        "page_path": "/hours",
                        "image_url": "",
                        "extra_metadata": {},
                    }
                ],
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "intent": None,
                "active_constraints": {},
                "retrieval_query": "when do you close",
                "resolved_reference": {"status": "none"},
                "new_session_state": {
                    "session_id": "s1",
                    "last_response_id": None,
                    "last_discussed_item_ids": [],
                    "last_candidate_item_ids": [],
                    "last_intent": None,
                    "active_constraints": {},
                },
                "fallback_reason": None,
                "created_at": 1,
                "ttl_seconds": 900,
                "schema_version": 1,
                "cache_query_embedding": [1.0, 0.0],
                "query_script": "latin",
                "query_text_exact": "When do you close?",
                "query_language": "eng",
            }
        ]

        class _MustNotRetrieve:
            def retrieve(self, *args, **kwargs):
                raise AssertionError("retrieval should not run on semantic hit")

        orig_embed = self.mod.embed_query
        orig_create = self.mod.create_assistant_response
        try:
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

            self.mod.embed_query = lambda client, text: _Vec([1.0, 0.0])
            self.mod.create_assistant_response = (
                lambda client, system_instructions, user_query, menu_context, session_context, previous_response_id:
                ("हम रात 10 बजे बंद करते हैं।", "resp_hin_cross", {"include_images": False, "max_images": 0, "target_item_names": []})
            )
            turn = self.mod.handle_chat_turn(
                client=object(),
                retriever=_MustNotRetrieve(),
                store=None,
                restaurant_id=restaurant_id,
                user_query="आप कब बंद करते हैं?",
                system_instructions="Prompt A",
                session_state=session_state,
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_namespace="qcache:v1",
                query_cache_ttl_seconds=900,
                query_cache_semantic_threshold=0.8,
                query_cache_xlang_threshold=0.72,
                query_cache_semantic_margin=0.05,
                query_language="cmn",
                request_id="req-cross-hit",
            )
        finally:
            self.mod.embed_query = orig_embed
            self.mod.create_assistant_response = orig_create

        self.assertTrue(turn.get("cache_hit"))
        self.assertEqual(turn.get("cache_hit_stage"), "semantic_cross_lang")
        self.assertEqual(turn.get("assistant_text"), "हम रात 10 बजे बंद करते हैं।")
        self.assertTrue(turn.get("cache_cross_language_regenerated"))
        self.assertEqual(turn.get("response_id"), "resp_hin_cross")

    def test_same_language_semantic_hit_does_not_regenerate(self):
        cache = _FakeQueryCache()
        session_state = {
            "session_id": "s1",
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }
        restaurant_id = "11111111-1111-4111-8111-111111111111"
        context_hash = self.mod.build_query_cache_context_hash(
            restaurant_id=restaurant_id,
            system_instructions="Prompt A",
            top_k=8,
            min_score=0.0,
            model="gpt-5-mini",
        )
        group_prefix = self.mod.build_query_cache_group_prefix("qcache:v1", restaurant_id, context_hash)
        cache.semantic_map[group_prefix] = [
            {
                "assistant_text": "We close at 10 PM.",
                "results": [],
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "intent": None,
                "active_constraints": {},
                "retrieval_query": "when do you close",
                "resolved_reference": {"status": "none"},
                "new_session_state": {
                    "session_id": "s1",
                    "last_response_id": None,
                    "last_discussed_item_ids": [],
                    "last_candidate_item_ids": [],
                    "last_intent": None,
                    "active_constraints": {},
                },
                "fallback_reason": None,
                "created_at": 1,
                "ttl_seconds": 900,
                "schema_version": 1,
                "cache_query_embedding": [1.0, 0.0],
                "query_script": "latin",
                "query_text_exact": "When do you close?",
                "query_language": "eng",
            }
        ]

        class _MustNotRetrieve:
            def retrieve(self, *args, **kwargs):
                raise AssertionError("retrieval should not run on semantic hit")

        orig_embed = self.mod.embed_query
        orig_create = self.mod.create_assistant_response
        try:
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

            self.mod.embed_query = lambda client, text: _Vec([1.0, 0.0])
            self.mod.create_assistant_response = (
                lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not regenerate on same-language hit"))
            )
            turn = self.mod.handle_chat_turn(
                client=object(),
                retriever=_MustNotRetrieve(),
                store=None,
                restaurant_id=restaurant_id,
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=session_state,
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_namespace="qcache:v1",
                query_cache_ttl_seconds=900,
                query_cache_semantic_threshold=0.8,
                query_cache_semantic_margin=0.05,
                query_language="eng",
                request_id="req-same-hit",
            )
        finally:
            self.mod.embed_query = orig_embed
            self.mod.create_assistant_response = orig_create

        self.assertTrue(turn.get("cache_hit"))
        self.assertEqual(turn.get("cache_hit_stage"), "semantic_same_lang")
        self.assertEqual(turn.get("assistant_text"), "We close at 10 PM.")
        self.assertFalse(turn.get("cache_cross_language_regenerated"))

    def test_semantic_margin_blocks_false_positive_and_falls_back(self):
        cache = _FakeQueryCache()
        session_state = {
            "session_id": "s1",
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }
        restaurant_id = "11111111-1111-4111-8111-111111111111"
        context_hash = self.mod.build_query_cache_context_hash(
            restaurant_id=restaurant_id,
            system_instructions="Prompt A",
            top_k=8,
            min_score=0.0,
            model="gpt-5-mini",
        )
        group_prefix = self.mod.build_query_cache_group_prefix("qcache:v1", restaurant_id, context_hash)
        cache.semantic_map[group_prefix] = [
            {
                "assistant_text": "Candidate one",
                "results": [],
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "intent": None,
                "active_constraints": {},
                "retrieval_query": "one",
                "resolved_reference": {"status": "none"},
                "new_session_state": dict(session_state),
                "fallback_reason": None,
                "created_at": 1,
                "ttl_seconds": 900,
                "schema_version": 1,
                "cache_query_embedding": [1.0, 0.0],
                "query_script": "latin",
                "query_text_exact": "one",
                "query_language": "eng",
            },
            {
                "assistant_text": "Candidate two",
                "results": [],
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "intent": None,
                "active_constraints": {},
                "retrieval_query": "two",
                "resolved_reference": {"status": "none"},
                "new_session_state": dict(session_state),
                "fallback_reason": None,
                "created_at": 1,
                "ttl_seconds": 900,
                "schema_version": 1,
                "cache_query_embedding": [0.99, 0.01],
                "query_script": "latin",
                "query_text_exact": "two",
                "query_language": "eng",
            },
        ]
        retriever = _FakeRetriever(
            [
                {
                    "id": "h1",
                    "text": "We close at 10 PM",
                    "title": "Hours",
                    "score": 0.9,
                    "type": "policy",
                    "source_url": "",
                    "page_path": "/hours",
                    "image_url": "",
                    "extra_metadata": {},
                }
            ]
        )

        orig_embed = self.mod.embed_query
        orig_create = self.mod.create_assistant_response
        try:
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

            self.mod.embed_query = lambda client, text: _Vec([1.0, 0.0])
            self.mod.create_assistant_response = (
                lambda client, system_instructions, user_query, menu_context, session_context, previous_response_id:
                ("We close at 10 PM.", "resp_after_miss", {"include_images": False, "max_images": 0, "target_item_names": []})
            )
            turn = self.mod.handle_chat_turn(
                client=object(),
                retriever=retriever,
                store=None,
                restaurant_id=restaurant_id,
                user_query="When do you close?",
                system_instructions="Prompt A",
                session_state=session_state,
                top_k=8,
                min_score=0.0,
                query_cache=cache,
                query_cache_namespace="qcache:v1",
                query_cache_ttl_seconds=900,
                query_cache_semantic_threshold=0.7,
                query_cache_semantic_margin=0.05,
                query_language="eng",
                request_id="req-margin-miss",
            )
        finally:
            self.mod.embed_query = orig_embed
            self.mod.create_assistant_response = orig_create

        self.assertFalse(turn.get("cache_hit"))
        self.assertEqual(retriever.calls, 1)


if __name__ == "__main__":
    unittest.main()
