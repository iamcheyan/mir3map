import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import audit


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "site" / "data"


class AuditParserTests(unittest.TestCase):
    def test_map_info_decodes_gb18030_and_preserves_link_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "MapInfo.txt"
            source.write_bytes("[1 比奇城 0]\n1 429,96 -> 1_002 10,17\n".encode("gb18030"))
            issues = []

            maps, links = audit.parse_map_info(source, issues)

        self.assertEqual(maps[0]["display_name"], "比奇城")
        self.assertEqual(maps[0]["source_ref"], "MapInfo.txt:1")
        self.assertEqual(links[0], {
            "from_map_id": "1",
            "from_map_key": "1",
            "from_x": 429,
            "from_y": 96,
            "to_map_id": "1_002",
            "to_map_key": "1_002",
            "to_x": 10,
            "to_y": 17,
            "source_ref": "MapInfo.txt:2",
        })
        self.assertEqual(issues, [])

    def test_repeated_generator_loads_keep_distinct_spawn_occurrences(self):
        with tempfile.TemporaryDirectory() as directory:
            envir = Path(directory)
            generator = envir / "Mon_Def" / "sample.gen"
            generator.parent.mkdir()
            generator.write_bytes("D011 136 210 鸡 0 5 8\n".encode("gb18030"))
            mongen = envir / "Mongen.txt"
            mongen.write_bytes('loadgen "sample.gen"\nloadgen "sample.gen"\n'.encode("gb18030"))
            issues = []

            loads = audit.parse_mongen(mongen, issues)
            spawns, resolved = audit.parse_generators(envir, loads, issues)

        self.assertEqual([row["source_ref"] for row in loads], ["Mongen.txt:1", "Mongen.txt:2"])
        self.assertEqual(len(spawns), 2)
        self.assertEqual([row["source_ref"] for row in spawns], ["sample.gen:1", "sample.gen:1"])
        self.assertEqual(len({row["record_id"] for row in spawns}), 2)
        self.assertEqual([row["load_order"] for row in spawns], [0, 1])
        self.assertEqual([row["numeric_parameters"] for row in spawns], [[0, 5, 8], [0, 5, 8]])
        self.assertEqual([row["status"] for row in resolved], ["resolved", "resolved"])
        self.assertEqual(issues, [])


class PublishedAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((DATA / "audit.json").read_text(encoding="utf-8"))

    def test_map_cards_associate_source_connections_npcs_and_spawns(self):
        maps = self.data["records"]["map_details"]
        by_id = {row["normalized_map_id"]: row for row in maps}
        town = by_id["1"]
        linked_map = by_id["1_002"]
        self.assertIn("Merchant.txt:2", {row["source_ref"] for row in town["mud3_npcs"]})
        connection_ref = "MapInfo.txt:246"
        self.assertIn(connection_ref, {row["source_ref"] for row in town["mud3_connections"]})
        self.assertIn(connection_ref, {row["source_ref"] for row in linked_map["mud3_connections"]})
        self.assertEqual(
            next(row for row in town["mud3_connections"] if row["source_ref"] == connection_ref)["to_map_id"],
            "1_002",
        )
        spawn_map = by_id["d011"]
        self.assertEqual(spawn_map["mud3"]["map_id"], "d011")
        self.assertIn("各地图小怪.gen:6", {row["source_ref"] for row in spawn_map["mud3_spawns"]})

    def test_map_cards_retain_every_source_record_occurrence(self):
        records = self.data["records"]
        maps = records["map_details"]
        unassigned_spawns = {
            row["SourceRef"] for row in records["unassigned_godot_spawns"]
        }
        for source_key, card_key, ref_key in (
            ("mud3_npcs", "mud3_npcs", "source_ref"),
            ("mud3_spawns", "mud3_spawns", "record_id"),
            ("mud3_connections", "mud3_connections", "source_ref"),
            ("godot_npcs", "godot_npcs", "SourceRef"),
            ("godot_spawns", "godot_spawns", "SourceRef"),
            ("godot_movements", "godot_movements", "SourceRef"),
        ):
            source_refs = {row[ref_key] for row in records[source_key]}
            card_refs = {row[ref_key] for card in maps for row in card[card_key]}
            if source_key == "godot_spawns":
                card_refs.update(unassigned_spawns)
            self.assertEqual(card_refs, source_refs, source_key)

        card_ids = {row["normalized_map_id"] for row in maps}
        mud3_map_ids = {row["normalized_map_id"] for row in records["mud3_maps"]}
        mud3_map_ids.update(row["normalized_map_id"] for row in records["mud3_npcs"])
        mud3_map_ids.update(row["normalized_map_id"] for row in records["mud3_spawns"])
        mud3_map_ids.update(
            map_id
            for row in records["mud3_connections"]
            for map_id in (row["from_map_key"], row["to_map_key"])
        )
        self.assertTrue(mud3_map_ids <= card_ids)

    def test_regionless_godot_spawns_are_unassigned_not_source_missing(self):
        records = self.data["records"]
        missing_region = [
            row for row in records["godot_spawns"]
            if not (row.get("Region") or {}).get("MapFileName")
        ]
        unassigned = records["unassigned_godot_spawns"]
        self.assertEqual(
            {row["SourceRef"] for row in unassigned},
            {row["SourceRef"] for row in missing_region},
        )
        issue_refs = {
            row["source_ref"] for row in self.data["issues"]
            if row["code"] == "missing_godot_spawn_region"
        }
        self.assertEqual(issue_refs, {row["SourceRef"] for row in missing_region})
        for spawn in missing_region:
            matches = [row for row in self.data["comparisons"] if row.get("godot_ref") == spawn["SourceRef"]]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["status"], "mapping_unverified")

    def test_status_counts_records_and_csv_references_close(self):
        with (DATA / "comparisons.csv").open(newline="", encoding="utf-8") as source:
            csv_rows = list(csv.DictReader(source))
        audit.validate_audit(self.data, csv_rows)

        comparisons = self.data["comparisons"]
        self.assertEqual(sum(self.data["summary"]["comparison_status_counts"].values()), len(comparisons))
        self.assertTrue(self.data["integrity"]["all_comparisons_have_source_refs"])
        self.assertTrue(self.data["integrity"]["all_source_record_refs_unique"])
        self.assertEqual(len(self.data["records"]["mud3_maps"]), self.data["integrity"]["record_counts"]["mud3_maps"])
        self.assertEqual(len(self.data["records"]["mud3_spawns"]), self.data["integrity"]["record_counts"]["mud3_spawns"])

        inconsistent = dict(self.data)
        inconsistent["summary"] = dict(self.data["summary"])
        inconsistent["summary"]["comparison_status_counts"] = dict(self.data["summary"]["comparison_status_counts"])
        status = comparisons[0]["status"]
        inconsistent["summary"]["comparison_status_counts"][status] += 1
        with self.assertRaisesRegex(ValueError, "comparison status counts do not close"):
            audit.validate_audit(inconsistent)



if __name__ == "__main__":
    unittest.main()
