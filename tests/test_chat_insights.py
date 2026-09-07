import importlib
import json
import sys
import types
import unittest


def _load_module():
    if "openai" not in sys.modules:
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = object
        sys.modules["openai"] = fake_openai
    return importlib.import_module("chat_insights")


class _FakeResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("unexpected LLM call")
        return types.SimpleNamespace(id=f"resp_{len(self.calls)}", output_text=self.outputs.pop(0))


class _FakeClient:
    def __init__(self, outputs):
        self.responses = _FakeResponses(outputs)


class _FakeStore:
    def __init__(self, sessions):
        self.sessions = list(sessions)

    def get_restaurant_profile(self, restaurant_id):
        return {
            "id": restaurant_id,
            "name": "Northwind Diner",
            "restaurant_profile": (
                "Casual restaurant focused on lunch combos, weekday office traffic, "
                "and fast pickup ordering."
            ),
            "system_prompt": None,
        }

    def get_recent_chat_sessions_with_messages(self, restaurant_id, since_iso, until_iso, limit):
        return list(self.sessions)[:limit]


class ChatInsightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_aggregate_session_extractions_counts_entities_and_friction(self):
        aggregate = self.mod.aggregate_session_extractions(
            [
                {
                    "analysis_status": "ok",
                    "source_language": "eng",
                    "goal_category": "pricing",
                    "user_main_goal": "Compare combo prices",
                    "outcome_status": "partially_resolved",
                    "price_sensitivity": "high",
                    "sentiment": "neutral",
                    "mentioned_entities": [
                        {"name": "Lunch Combo", "entity_type": "dish", "certainty": "high"},
                        {"name": "Delivery", "entity_type": "service", "certainty": "high"},
                    ],
                    "friction_points": [
                        {"label": "pricing_uncertainty", "detail": "User could not tell whether the combo included a drink."}
                    ],
                    "purchase_signals": [{"label": "high_intent", "detail": "Asked whether pickup could happen today."}],
                    "unanswered_questions": ["Does the combo include a drink?"],
                    "revenue_opportunity": "Clarify combo inclusions near the price.",
                    "messages_truncated": False,
                    "content_truncated": False,
                    "messages_included": 4,
                },
                {
                    "analysis_status": "ok",
                    "source_language": "spa",
                    "goal_category": "delivery",
                    "user_main_goal": "Check delivery area",
                    "outcome_status": "unresolved",
                    "price_sensitivity": "medium",
                    "sentiment": "mixed",
                    "mentioned_entities": [
                        {"name": "Lunch Combo", "entity_type": "dish", "certainty": "medium"},
                        {"name": "Delivery", "entity_type": "service", "certainty": "high"},
                    ],
                    "friction_points": [
                        {"label": "delivery_uncertainty", "detail": "Delivery radius was unclear."}
                    ],
                    "purchase_signals": [{"label": "cart_risk", "detail": "User may abandon if delivery is unavailable."}],
                    "unanswered_questions": ["Do you deliver to Burnaby?"],
                    "revenue_opportunity": "Publish delivery radius and fees more clearly.",
                    "messages_truncated": True,
                    "content_truncated": False,
                    "messages_included": 6,
                },
            ]
        )

        self.assertEqual(aggregate["successful_session_count"], 2)
        self.assertEqual(aggregate["top_dishes"][0]["name"], "Lunch Combo")
        self.assertEqual(aggregate["top_dishes"][0]["count"], 2)
        self.assertEqual(aggregate["top_services"][0]["name"], "Delivery")
        self.assertEqual(aggregate["sessions_with_unanswered_questions"], 2)
        self.assertEqual(aggregate["high_intent_sessions"], 1)
        self.assertEqual(aggregate["coverage_flags"]["messages_truncated_sessions"], 1)

    def test_service_generate_returns_structured_summary(self):
        sessions = [
            {
                "id": "sess-1",
                "session_token": "token-1",
                "language": "eng",
                "created_at": "2026-04-01T12:00:00+00:00",
                "last_activity_at": "2026-04-01T12:05:00+00:00",
                "messages": [
                    {"role": "user", "content": "Does the lunch combo include a drink?", "created_at": "2026-04-01T12:00:00+00:00"},
                    {"role": "assistant", "content": "I am not sure about the drink inclusion.", "created_at": "2026-04-01T12:01:00+00:00"},
                ],
            },
            {
                "id": "sess-2",
                "session_token": "token-2",
                "language": "spa",
                "created_at": "2026-04-02T18:00:00+00:00",
                "last_activity_at": "2026-04-02T18:06:00+00:00",
                "messages": [
                    {"role": "user", "content": "Hacen entregas en Burnaby?", "created_at": "2026-04-02T18:00:00+00:00"},
                    {"role": "assistant", "content": "No tengo confirmada el area de entrega.", "created_at": "2026-04-02T18:01:00+00:00"},
                ],
            },
        ]
        client = _FakeClient(
            [
                json.dumps(
                    {
                        "source_language": "eng",
                        "summary_en": "Customer asked whether the lunch combo includes a drink.",
                        "user_main_goal": "Understand combo inclusions before buying.",
                        "goal_category": "pricing",
                        "goal_confidence": "high",
                        "price_sensitivity": "high",
                        "sentiment": "neutral",
                        "resolved_references": [],
                        "mentioned_entities": [{"name": "Lunch Combo", "entity_type": "dish", "certainty": "high"}],
                        "dietary_needs": [],
                        "occasion_signals": [],
                        "friction_points": [{"label": "pricing_uncertainty", "detail": "Combo contents were unclear."}],
                        "purchase_signals": [{"label": "high_intent", "detail": "Customer was close to ordering."}],
                        "unanswered_questions": ["Does the combo include a drink?"],
                        "outcome_status": "partially_resolved",
                        "outcome_reason": "Price context remained incomplete.",
                        "revenue_opportunity": "Clarify combo inclusions beside the price.",
                        "uncertainty_notes": [],
                    }
                ),
                json.dumps(
                    {
                        "source_language": "spa",
                        "summary_en": "Customer asked whether delivery is available in Burnaby.",
                        "user_main_goal": "Check delivery availability before ordering.",
                        "goal_category": "delivery",
                        "goal_confidence": "high",
                        "price_sensitivity": "medium",
                        "sentiment": "mixed",
                        "resolved_references": [],
                        "mentioned_entities": [{"name": "Delivery", "entity_type": "service", "certainty": "high"}],
                        "dietary_needs": [],
                        "occasion_signals": [],
                        "friction_points": [{"label": "delivery_uncertainty", "detail": "Delivery coverage was unclear."}],
                        "purchase_signals": [{"label": "cart_risk", "detail": "Order may be lost if delivery area is unclear."}],
                        "unanswered_questions": ["Do you deliver to Burnaby?"],
                        "outcome_status": "unresolved",
                        "outcome_reason": "Delivery zone could not be confirmed.",
                        "revenue_opportunity": "Publish delivery radius and fees on the site.",
                        "uncertainty_notes": [],
                    }
                ),
                json.dumps(
                    {
                        "executive_summary": "Customers show buying intent, but combo details and delivery coverage are unclear.",
                        "revenue_opportunities": [
                            {
                                "title": "Clarify combo contents",
                                "why_it_matters": "Pricing questions are slowing purchase decisions.",
                                "supporting_signals": ["Combo inclusion question remained unanswered."],
                                "recommended_action": "Show combo inclusions directly beside the price.",
                                "impact": "high",
                            }
                        ],
                        "menu_and_service_insights": [
                            {
                                "title": "Delivery coverage is a blocker",
                                "insight": "Customers cannot tell whether delivery reaches them.",
                                "supporting_signals": ["Burnaby delivery question stayed unresolved."],
                                "recommended_action": "Publish delivery zones and any fees in the ordering flow.",
                            }
                        ],
                        "content_gaps": [
                            {
                                "gap": "Combo inclusions are unclear",
                                "why_it_matters": "Customers need this before ordering.",
                                "recommended_action": "Add inclusions to menu cards and FAQ.",
                            }
                        ],
                        "questions_to_answer_more_clearly": [
                            "Does the lunch combo include a drink?",
                            "Do you deliver to Burnaby?",
                        ],
                        "operational_watchouts": ["Delivery-zone ambiguity may cause abandoned orders."],
                        "confidence_notes": ["Based on two analyzed sessions."],
                    }
                ),
            ]
        )
        service = self.mod.ChatInsightsService(
            client=client,
            store=_FakeStore(sessions),
            config=self.mod.ChatInsightsConfig(max_sessions=10),
        )

        report = service.generate(
            restaurant_id="11111111-1111-4111-8111-111111111111",
            window_days=30,
        )

        self.assertEqual(report["windowDays"], 30)
        self.assertEqual(report["coverage"]["sessionsAnalyzed"], 2)
        self.assertEqual(report["aggregateMetrics"]["successful_session_count"], 2)
        self.assertEqual(report["summary"]["revenue_opportunities"][0]["title"], "Clarify combo contents")
        self.assertEqual(len(report["sessionExtractions"]), 2)
        self.assertEqual(len(client.responses.calls), 3)


if __name__ == "__main__":
    unittest.main()
