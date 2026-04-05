from __future__ import annotations

import unittest

from world_schema import (
    normalize_faction_status,
    normalize_relation_importance,
    normalize_relation_label,
    normalize_relation_type,
    relation_type_label,
)


class WorldSchemaTests(unittest.TestCase):
    def test_normalize_relation_type_maps_chinese_labels(self) -> None:
        self.assertEqual(normalize_relation_type("母子"), "kinship")
        self.assertEqual(normalize_relation_type("盟友"), "affinity")
        self.assertEqual(normalize_relation_type("宿敌"), "hostility")
        self.assertEqual(normalize_relation_type("上下级"), "authority")

    def test_normalize_relation_type_maps_english_aliases(self) -> None:
        self.assertEqual(normalize_relation_type("friend"), "affinity")
        self.assertEqual(normalize_relation_type("enemy"), "hostility")
        self.assertEqual(normalize_relation_type("parent child"), "kinship")

    def test_normalize_relation_label_localizes_known_english_values(self) -> None:
        self.assertEqual(normalize_relation_label("friend"), "朋友")
        self.assertEqual(normalize_relation_label("boss subordinate"), "上下级")
        self.assertEqual(normalize_relation_label("盟友"), "盟友")

    def test_relation_type_label_uses_normalized_key(self) -> None:
        self.assertEqual(relation_type_label("affinity"), "友好")
        self.assertEqual(relation_type_label("enemy"), "敌对")

    def test_normalize_relation_importance(self) -> None:
        self.assertEqual(normalize_relation_importance("核心"), "core")
        self.assertEqual(normalize_relation_importance("major"), "major")
        self.assertEqual(normalize_relation_importance("次要"), "minor")
        self.assertEqual(normalize_relation_importance("路人"), "background")

    def test_normalize_faction_status(self) -> None:
        self.assertEqual(normalize_faction_status("active"), "active")
        self.assertEqual(normalize_faction_status("已退出"), "former")


if __name__ == "__main__":
    unittest.main()
