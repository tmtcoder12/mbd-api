import types
import unittest


def _load_module():
    from mbd_api import core

    return core


class _FakeOpenAIStream:
    def __init__(self, events, final_response):
        self.events = list(events)
        self.final_response = final_response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_response(self):
        return self.final_response


class _FakeResponses:
    def __init__(self, events):
        self.events = events
        self.stream_calls = 0
        self.create_calls = 0
        self.last_stream_kwargs = None

    def stream(self, **kwargs):
        self.stream_calls += 1
        self.last_stream_kwargs = kwargs
        return _FakeOpenAIStream(self.events, types.SimpleNamespace(id="resp-final"))

    def create(self, **kwargs):
        self.create_calls += 1
        raise AssertionError("non-streaming create should not be called")


class _FakeClient:
    def __init__(self, events):
        self.responses = _FakeResponses(events)


class _FakeRetriever:
    def __init__(self):
        self.calls = 0

    def retrieve(self, client, user_q, top_k, min_score, restaurant_id=None, query_embedding=None):
        self.calls += 1
        return {
            "results": [
                {
                    "id": "chunk-1",
                    "text": "We close at 10 PM.",
                    "title": "Hours",
                    "score": 0.91,
                    "type": "policy",
                    "source_url": "",
                    "page_path": "/hours",
                    "image_url": "",
                    "extra_metadata": {},
                }
            ],
            "timing_start": 0.0,
        }


class AssistantStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _tagged_chunks(self, assistant_text):
        mod = self.mod
        return [
            "prefix that should not render " + mod.ASSISTANT_TEXT_START_MARKER[:14],
            mod.ASSISTANT_TEXT_START_MARKER[14:] + "\n" + assistant_text[:66],
            assistant_text[66:124],
            assistant_text[124:] + "\n" + mod.ASSISTANT_TEXT_END_MARKER[:11],
            mod.ASSISTANT_TEXT_END_MARKER[11:]
            + "\n"
            + mod.IMAGE_DECISION_JSON_START_MARKER
            + '\n{"include_images":true,"max_images":1,"target_item_names":["Country Style Rice Noodles"]}\n'
            + mod.IMAGE_DECISION_JSON_END_MARKER,
        ]

    def test_parser_streams_text_and_parses_image_decision_across_split_markers(self):
        text = (
            "Hello, this is a first streamed sentence with enough text to flush. "
            "Here is another streamed sentence with enough text to flush."
        )
        parser = self.mod.AssistantTaggedStreamParser()
        emitted = []
        for chunk in self._tagged_chunks(text):
            emitted.extend(parser.feed(chunk))
        late_chunks, assistant_text, image_decision = parser.finish()
        emitted.extend(late_chunks)

        self.assertEqual("".join(emitted), text)
        self.assertEqual(assistant_text, text)
        self.assertTrue(image_decision["include_images"])
        self.assertEqual(image_decision["max_images"], 1)
        self.assertEqual(image_decision["target_item_names"], ["Country Style Rice Noodles"])
        self.assertNotIn("<<<", "".join(emitted))

    def test_parser_falls_back_to_old_json_without_streaming_markers(self):
        raw = (
            '{"assistant_text":"Fallback answer.",'
            '"image_decision":{"include_images":true,"max_images":1,"target_item_names":["Noodles"]}}'
        )
        parser = self.mod.AssistantTaggedStreamParser()
        emitted = []
        for chunk in [raw[:12], raw[12:40], raw[40:]]:
            emitted.extend(parser.feed(chunk))
        self.assertEqual(emitted, [])

        late_chunks, assistant_text, image_decision = parser.finish()

        self.assertEqual(late_chunks, ["Fallback answer."])
        self.assertEqual(assistant_text, "Fallback answer.")
        self.assertTrue(image_decision["include_images"])
        self.assertEqual(image_decision["target_item_names"], ["Noodles"])

    def test_parser_does_not_emit_image_json_when_text_end_marker_is_missing(self):
        mod = self.mod
        raw = (
            mod.ASSISTANT_TEXT_START_MARKER
            + "\nAnswer text"
            + "\n"
            + mod.IMAGE_DECISION_JSON_START_MARKER
            + '\n{"include_images":true,"max_images":1,"target_item_names":["Noodles"]}\n'
            + mod.IMAGE_DECISION_JSON_END_MARKER
        )
        parser = mod.AssistantTaggedStreamParser()
        emitted = []
        for chunk in [raw[:40], raw[40:80], raw[80:]]:
            emitted.extend(parser.feed(chunk))
        late_chunks, assistant_text, image_decision = parser.finish()
        emitted.extend(late_chunks)

        self.assertEqual("".join(emitted), "Answer text")
        self.assertEqual(assistant_text, "Answer text")
        self.assertTrue(image_decision["include_images"])
        self.assertNotIn("MINTGEN_IMAGE_DECISION", "".join(emitted))

    def test_create_assistant_response_streams_one_openai_request(self):
        text = (
            "Hello, this is a first streamed sentence with enough text to flush. "
            "Here is another streamed sentence with enough text to flush."
        )
        events = [
            types.SimpleNamespace(type="response.output_text.delta", delta=chunk) for chunk in self._tagged_chunks(text)
        ]
        events.append(types.SimpleNamespace(type="response.completed", response=types.SimpleNamespace(id="resp-event")))
        client = _FakeClient(events)
        emitted = []

        assistant_text, response_id, image_decision = self.mod.create_assistant_response(
            client=client,
            system_instructions="Prompt A",
            user_query="When do you close?",
            menu_context="Hours: We close at 10 PM.",
            session_context="{}",
            previous_response_id="resp-prev",
            assistant_delta_callback=emitted.append,
        )

        self.assertEqual(client.responses.stream_calls, 1)
        self.assertEqual(client.responses.create_calls, 0)
        self.assertEqual(client.responses.last_stream_kwargs["previous_response_id"], "resp-prev")
        self.assertGreaterEqual(len(emitted), 2)
        self.assertEqual("".join(emitted), text)
        self.assertEqual(assistant_text, text)
        self.assertEqual(response_id, "resp-final")
        self.assertTrue(image_decision["include_images"])

    def test_handle_chat_turn_uses_delta_callback_for_fresh_llm_response(self):
        text = (
            "Hello, this is a first streamed sentence with enough text to flush. "
            "Here is another streamed sentence with enough text to flush."
        )
        events = [
            types.SimpleNamespace(type="response.output_text.delta", delta=chunk) for chunk in self._tagged_chunks(text)
        ]
        events.append(types.SimpleNamespace(type="response.completed", response=types.SimpleNamespace(id="resp-event")))
        client = _FakeClient(events)
        emitted = []

        turn = self.mod.handle_chat_turn(
            client=client,
            retriever=_FakeRetriever(),
            store=None,
            restaurant_id="11111111-1111-4111-8111-111111111111",
            user_query="When do you close?",
            system_instructions="Prompt A",
            session_state={
                "session_id": "s1",
                "last_response_id": None,
                "last_discussed_item_ids": [],
                "last_candidate_item_ids": [],
                "last_intent": None,
                "active_constraints": {},
            },
            top_k=8,
            min_score=0.0,
            query_cache=None,
            request_id="req-stream",
            assistant_delta_callback=emitted.append,
        )

        self.assertEqual("".join(emitted), text)
        self.assertEqual(turn["assistant_text"], text)
        self.assertEqual(turn["response_id"], "resp-final")
        self.assertFalse(turn["cache_hit"])


if __name__ == "__main__":
    unittest.main()
