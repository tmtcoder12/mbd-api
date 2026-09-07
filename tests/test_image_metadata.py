import unittest


def _load_module():
    from mbd_api import core

    return core


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
