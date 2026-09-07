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
    spec = importlib.util.spec_from_file_location("rag_chatbot_image_metadata_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load rag-chatbot.py for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImageMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_image_payload_reads_capitalized_extra_metadata_image_url(self):
        image_url = (
            "https://zdthrfpibaieeetibofc.supabase.co/storage/v1/object/public/"
            "Restaurant%20Images/11111111-1111-4111-8111-555555555555/countryStyleRiceNoodles.png"
        )
        rows = [
            {
                "id": "chunk-1",
                "title": "Country Style Rice Noodles",
                "text": "Country Style Rice Noodles with vegetables.",
                "image_url": "",
                "extra_metadata": {
                    "price": "16.98",
                    "category": "Noodles & Chow Fun",
                    "Image_url": image_url,
                    "item_name": "Country Style Rice Noodles",
                },
                "score": 0.91,
            }
        ]

        payload = self.mod.build_image_payload_from_decision(
            rows,
            {
                "include_images": True,
                "max_images": 1,
                "target_item_names": ["Country Style Rice Noodles"],
            },
        )

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["image_url"], image_url)
        self.assertEqual(payload[0]["title"], "Country Style Rice Noodles")

    def test_retriever_preserves_rpc_extra_metadata_image_url(self):
        image_url = "https://example.com/countryStyleRiceNoodles.png"

        class _VecRow:
            def tolist(self):
                return [1.0, 0.0]

        class _Vec:
            def __getitem__(self, idx):
                return _VecRow()

        class _Store:
            def match_chunks(self, restaurant_id, query_embedding, match_count, min_score):
                return [
                    {
                        "id": "chunk-1",
                        "text": "Country Style Rice Noodles",
                        "type": "menu_item",
                        "source_url": "",
                        "page_path": "",
                        "title": "Country Style Rice Noodles",
                        "score": 0.92,
                        "image_url": "",
                        "extra_metadata": {
                            "Image_url": image_url,
                            "item_name": "Country Style Rice Noodles",
                        },
                    }
                ]

            def get_knowledge_chunks_by_ids(self, chunk_ids):
                return []

        original_embed = self.mod.embed_query
        self.mod.embed_query = lambda client, text: _Vec()
        try:
            result = self.mod.Retriever(_Store()).retrieve(
                client=object(),
                user_q="show me country style rice noodles",
                top_k=1,
                min_score=0.0,
                restaurant_id="11111111-1111-4111-8111-555555555555",
            )
        finally:
            self.mod.embed_query = original_embed

        payload = self.mod.build_image_payload_from_decision(
            result["results"],
            {"include_images": True, "max_images": 1, "target_item_names": ["Country Style Rice Noodles"]},
        )

        self.assertEqual(payload[0]["image_url"], image_url)

    def test_image_payload_requires_target_item_name_match(self):
        image_url = "https://example.com/countryStyleRiceNoodles.png"
        rows = [
            {
                "id": "chunk-1",
                "title": "Country Style Rice Noodles",
                "text": "Country Style Rice Noodles with vegetables.",
                "image_url": "",
                "extra_metadata": {"Image_url": image_url, "item_name": "Country Style Rice Noodles"},
                "score": 0.91,
            }
        ]

        no_targets_payload = self.mod.build_image_payload_from_decision(
            rows,
            {"include_images": True, "max_images": 1, "target_item_names": []},
        )
        unmatched_payload = self.mod.build_image_payload_from_decision(
            rows,
            {"include_images": True, "max_images": 1, "target_item_names": ["Deep Fried Beef"]},
        )

        self.assertEqual(no_targets_payload, [])
        self.assertEqual(unmatched_payload, [])


if __name__ == "__main__":
    unittest.main()
