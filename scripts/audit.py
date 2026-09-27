#!/usr/bin/env python3
"""Extract, compare, and validate the public MUD3/Godot map-data audit.

The input roots are supplied at runtime. This program never writes to a source
repository or copies source databases/configuration files into the output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = {
    "identical",
    "explicit_difference",
    "format_only",
    "mapping_unverified",
    "source_missing",
}
COMPARISON_FIELDS = [
    "comparison_id",
    "entity_type",
    "entity_key",
    "field",
    "mud3_value",
    "godot_value",
    "website_value",
    "mud3_ref",
    "godot_ref",
    "website_ref",
    "status",
    "mapping_confidence",
    "mapping_basis",
    "note",
]
OPTION_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\([^\r\n]*\))?$")
MAP_LINK_ENDPOINT = re.compile(
    r"^\s*(?P<map>\S+)\s+(?P<x>[+-]?\d+)\s*(?:,\s*|\s+)(?P<y>[+-]?\d+)\s*$"
)
GEN_RECORD = re.compile(
    r"^\s*(?P<map>\S+)\s+(?P<x>[+-]?\d+)\s+(?P<y>[+-]?\d+)"
    r"\s+(?P<monster>.+?)\s+(?P<p1>[+-]?\d+)\s+(?P<p2>[+-]?\d+)"
    r"\s+(?P<p3>[+-]?\d+)\s*$"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(path: Path, source_id: str, published_name: str, encoding: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    return {
        "source_id": source_id,
        "file": published_name,
        "sha256": sha256_file(path),
        "size_bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "encoding": encoding,
    }


def line_fingerprint(line: str) -> str:
    return sha256_bytes(line.encode("utf-8"))


def issue(
    issues: list[dict[str, Any]],
    source_ref: str,
    code: str,
    severity: str,
    line: str | None = None,
    **details: Any,
) -> None:
    row: dict[str, Any] = {"source_ref": source_ref, "code": code, "severity": severity}
    if line is not None:
        row["record_sha256"] = line_fingerprint(line)
        row["record_characters"] = len(line)
    row.update(details)
    issues.append(row)


def decode_source(path: Path, public_name: str, issues: list[dict[str, Any]], encoding: str = "gb18030") -> list[str]:
    raw = path.read_bytes()
    try:
        text = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        prefix = raw[: error.start]
        line_number = prefix.count(b"\n") + 1
        issue(
            issues,
            f"{public_name}:{line_number}",
            "decode_error",
            "error",
            byte_offset=error.start,
            encoding=encoding,
            invalid_byte_count=max(1, error.end - error.start),
        )
        raise ValueError(f"Strict {encoding} decoding failed for {public_name} at byte {error.start}") from error
    return text.splitlines()


def is_ignorable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith((";", "//", "#"))


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def normalize_map_id(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


def json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def parse_map_info(path: Path, issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines = decode_source(path, "MapInfo.txt", issues)
    maps: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    previous_map: dict[str, Any] | None = None

    for number, line in enumerate(lines, 1):
        if is_ignorable(line) or re.fullmatch(r"\s*[-=]{3,}\s*", line):
            continue
        source_ref = f"MapInfo.txt:{number}"
        definition = re.match(r"^\s*\[([^\]]+)\]\s*(.*?)\s*$", line)
        if definition:
            header_tokens = definition.group(1).split()
            if len(header_tokens) < 2:
                issue(issues, source_ref, "malformed_map_definition", "error", line)
                previous_map = None
                continue
            map_id = header_tokens[0]
            if len(header_tokens) >= 3:
                display_name = " ".join(header_tokens[1:-1])
                header_value = header_tokens[-1]
            else:
                display_name = header_tokens[1]
                header_value = None
            options = definition.group(2).split()
            row = {
                "map_id": map_id,
                "normalized_map_id": normalize_map_id(map_id),
                "display_name": display_name,
                "normalized_display_name": normalize_text(display_name),
                "header_value": header_value,
                "options": [
                    {"raw": token, "normalized": token.upper(), "source_ref": source_ref}
                    for token in options
                ],
                "source_ref": source_ref,
            }
            maps.append(row)
            previous_map = row
            continue

        if "->" in line:
            pieces = line.split("->")
            source_point = MAP_LINK_ENDPOINT.fullmatch(pieces[0]) if len(pieces) == 2 else None
            destination_point = MAP_LINK_ENDPOINT.fullmatch(pieces[1]) if len(pieces) == 2 else None
            if source_point and destination_point:
                from_map = source_point.group("map")
                to_map = destination_point.group("map")
                links.append(
                    {
                        "from_map_id": from_map,
                        "from_map_key": normalize_map_id(from_map),
                        "from_x": int(source_point.group("x")),
                        "from_y": int(source_point.group("y")),
                        "to_map_id": to_map,
                        "to_map_key": normalize_map_id(to_map),
                        "to_x": int(destination_point.group("x")),
                        "to_y": int(destination_point.group("y")),
                        "source_ref": source_ref,
                    }
                )
            else:
                issue(issues, source_ref, "malformed_map_link", "error", line)
            previous_map = None
            continue

        option_tokens = line.split()
        if previous_map is not None and option_tokens and all(OPTION_TOKEN.fullmatch(token) for token in option_tokens):
            for token in option_tokens:
                previous_map["options"].append(
                    {"raw": token, "normalized": token.upper(), "source_ref": source_ref}
                )
            issue(
                issues,
                source_ref,
                "map_option_continuation",
                "review",
                line,
                attached_map_ref=previous_map["source_ref"],
                parsed_option_count=len(option_tokens),
            )
            previous_map = None
            continue

        partial = MAP_LINK_ENDPOINT.fullmatch(line)
        if partial:
            issue(
                issues,
                source_ref,
                "incomplete_map_link_endpoint",
                "review",
                line,
                from_map_id=partial.group("map"),
                from_x=int(partial.group("x")),
                from_y=int(partial.group("y")),
                destination_missing=True,
            )
        else:
            issue(issues, source_ref, "unparsed_mapinfo_record", "review", line)
        previous_map = None

    map_ids: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in maps:
        map_ids[row["normalized_map_id"]].append(row)
    for rows in map_ids.values():
        if len(rows) > 1:
            issue(
                issues,
                rows[0]["source_ref"],
                "duplicate_mud3_map_id",
                "review",
                duplicate_source_refs=[item["source_ref"] for item in rows],
                map_id=rows[0]["map_id"],
            )
    return maps, links


def parse_minimap(path: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(decode_source(path, "MiniMap.txt", issues), 1):
        if is_ignorable(line):
            continue
        fields = line.split()
        source_ref = f"MiniMap.txt:{number}"
        if len(fields) != 2:
            issue(issues, source_ref, "malformed_minimap_record", "review", line)
            continue
        try:
            minimap_index = int(fields[1])
        except ValueError:
            issue(issues, source_ref, "invalid_minimap_index", "review", line)
            continue
        rows.append(
            {
                "map_id": fields[0],
                "normalized_map_id": normalize_map_id(fields[0]),
                "minimap_index": minimap_index,
                "source_ref": source_ref,
            }
        )
    return rows


def parse_mongen(path: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    loads: list[dict[str, Any]] = []
    for number, line in enumerate(decode_source(path, "Mongen.txt", issues), 1):
        if is_ignorable(line):
            continue
        match = re.fullmatch(r"\s*loadgen\s+[\"']([^\"']+)[\"']\s*", line, re.IGNORECASE)
        source_ref = f"Mongen.txt:{number}"
        if match:
            loads.append({"file": match.group(1), "source_ref": source_ref, "load_order": len(loads)})
        else:
            issue(issues, source_ref, "unparsed_mongen_record", "review", line)
    return loads


def resolve_generator(gen_root: Path, requested: str) -> Path | None:
    candidate = (gen_root / requested).resolve()
    try:
        candidate.relative_to(gen_root.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    if gen_root.is_dir():
        matches = [item for item in gen_root.iterdir() if item.is_file() and item.name.casefold() == requested.casefold()]
        if len(matches) == 1:
            return matches[0]
    return None


def parse_generator(path: Path, published_name: str, load_ref: str, load_order: int, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(decode_source(path, published_name, issues), 1):
        if is_ignorable(line) or re.fullmatch(r"\s*\[[^\]]+\]\s*", line) or re.fullmatch(r"\s*[-=]{3,}\s*", line):
            continue
        match = GEN_RECORD.fullmatch(line)
        source_ref = f"{published_name}:{number}"
        if not match:
            issue(issues, source_ref, "unparsed_generator_record", "review", line, load_ref=load_ref)
            continue
        map_id = match.group("map")
        monster_name = match.group("monster").strip()
        rows.append(
            {
                "map_id": map_id,
                "normalized_map_id": normalize_map_id(map_id),
                "x": int(match.group("x")),
                "y": int(match.group("y")),
                "monster_name": monster_name,
                "normalized_monster_name": normalize_text(monster_name),
                "numeric_parameters": [int(match.group("p1")), int(match.group("p2")), int(match.group("p3"))],
                "load_ref": load_ref,
                "load_order": load_order,
                "source_ref": source_ref,
                "record_id": f"{load_ref}->{source_ref}",
            }
        )
    return rows


def parse_generators(envir: Path, loads: list[dict[str, Any]], issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gen_root = envir / "Mon_Def"
    rows: list[dict[str, Any]] = []
    resolved_loads: list[dict[str, Any]] = []
    for load in loads:
        path = resolve_generator(gen_root, load["file"])
        if path is None:
            issue(issues, load["source_ref"], "missing_or_unsafe_generator_file", "error", requested_file=Path(load["file"]).name)
            resolved_loads.append({**load, "resolved_file": None, "status": "unresolved"})
            continue
        published_name = path.name
        case_adjusted = published_name != load["file"]
        resolved_loads.append(
            {**load, "resolved_file": published_name, "status": "case_adjusted" if case_adjusted else "resolved"}
        )
        if case_adjusted:
            issue(
                issues,
                load["source_ref"],
                "generator_reference_case_difference",
                "review",
                requested_file=Path(load["file"]).name,
                resolved_file=published_name,
            )
        rows.extend(parse_generator(path, published_name, load["source_ref"], load["load_order"], issues))
    return rows, resolved_loads


def parse_merchants(path: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(decode_source(path, "Merchant.txt", issues), 1):
        if is_ignorable(line):
            continue
        fields = line.split()
        source_ref = f"Merchant.txt:{number}"
        if len(fields) != 7:
            issue(issues, source_ref, "malformed_merchant_record", "review", line, field_count=len(fields))
            continue
        try:
            x, y = int(fields[2]), int(fields[3])
        except ValueError:
            issue(issues, source_ref, "invalid_merchant_coordinates", "review", line)
            continue
        rows.append(
            {
                "npc_name": fields[0],
                "normalized_npc_name": normalize_text(fields[0]),
                "map_id": fields[1],
                "normalized_map_id": normalize_map_id(fields[1]),
                "x": x,
                "y": y,
                "definition_label": fields[4],
                "opaque_fields": fields[5:7],
                "source_ref": source_ref,
            }
        )
    return rows


def read_json(path: Path, public_name: str, source_id: str, issues: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        issue(issues, public_name, "invalid_utf8_json", "error", error=str(error).splitlines()[0])
        raise ValueError(f"Could not strictly parse {public_name}") from error
    return value, file_manifest(path, source_id, public_name, "UTF-8")


def parse_website_sources(data_root: Path, issues: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    monsters_raw, _ = read_json(data_root / "monsters.json", "monsters.json", "website-monsters", issues)
    maps_raw, _ = read_json(data_root / "maps.json", "maps.json", "website-map-catalog", issues)
    alignment_root = data_root / "alignment" / "entities"
    research_maps_raw, _ = read_json(alignment_root / "map.json", "alignment/entities/map.json", "website-map-research-snapshot", issues)
    research_areas_raw, _ = read_json(alignment_root / "map_area.json", "alignment/entities/map_area.json", "website-map-area-research-snapshot", issues)

    website_monsters: list[dict[str, Any]] = []
    for index, item in enumerate(monsters_raw):
        source_ref = f"monsters.json#/{index}"
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("id", "name", "category")):
            issue(issues, source_ref, "invalid_website_monster_record", "review")
            continue
        website_monsters.append(
            {
                "id": item["id"],
                "name": item["name"],
                "normalized_name": normalize_text(item["name"]),
                "category": item["category"],
                "source_ref": source_ref,
                "name_ref": f"monsters.json#/{index}/name",
                "category_ref": f"monsters.json#/{index}/category",
            }
        )

    website_areas: list[dict[str, Any]] = []
    for category_index, category in enumerate(maps_raw):
        if not isinstance(category, dict):
            issue(issues, f"maps.json#/{category_index}", "invalid_website_map_category", "review")
            continue
        for area_index, area in enumerate(category.get("areas", [])):
            if not isinstance(area, dict) or not isinstance(area.get("name"), str):
                issue(issues, f"maps.json#/{category_index}/areas/{area_index}", "invalid_website_map_area", "review")
                continue
            website_areas.append(
                {
                    "catalog_id": category.get("id"),
                    "catalog_title": category.get("title"),
                    "name": area["name"],
                    "source_ref": f"maps.json#/{category_index}/areas/{area_index}/name",
                    "identity_role": "辅助名称；不参与地图实体匹配",
                }
            )

    research_maps: list[dict[str, Any]] = []
    for index, item in enumerate(research_maps_raw):
        if not isinstance(item, dict):
            continue
        identity = item.get("identity") or {}
        game_data = item.get("game_data") or {}
        research_maps.append(
            {
                "map_file_candidate": game_data.get("FileName"),
                "research_index": identity.get("zircon_index"),
                "research_name": identity.get("current_game_name"),
                "translation_candidate": identity.get("current_translation"),
                "assessment": item.get("assessment", {}).get("overall_status"),
                "source_ref": f"alignment/entities/map.json#/{index}",
                "authority": "研究快照；不作 Godot 运行时权威数据",
            }
        )

    research_areas: list[dict[str, Any]] = []
    for index, item in enumerate(research_areas_raw):
        if not isinstance(item, dict):
            continue
        game_data = item.get("game_data") or {}
        for candidate_index, candidate in enumerate(game_data.get("ecology_candidate_details", [])):
            if not isinstance(candidate, dict):
                continue
            research_areas.append(
                {
                    "map_file_candidate": candidate.get("file"),
                    "research_index": candidate.get("index"),
                    "candidate_name": candidate.get("description"),
                    "assessment": item.get("assessment", {}).get("overall_status"),
                    "source_ref": f"alignment/entities/map_area.json#/{index}/game_data/ecology_candidate_details/{candidate_index}",
                    "authority": "待复核研究候选；不作地图身份匹配",
                }
            )
    return website_monsters, website_areas, research_maps, research_areas, []


def parse_translations(path: Path, issues: list[dict[str, Any]]) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    raw, manifest = read_json(path, "translations/db_names.json", "godot-name-translations", issues)
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for entity in ("maps", "monsters", "npcs"):
        result[entity] = {}
        section = raw.get(entity, {}) if isinstance(raw, dict) else {}
        if not isinstance(section, dict):
            issue(f"translations/db_names.json#/{entity}", "invalid_translation_section", "review")
            continue
        for name, values in section.items():
            if not isinstance(values, dict):
                issue(f"translations/db_names.json#/{entity}/{json_pointer_token(name)}", "invalid_translation_record", "review")
                continue
            result[entity][name] = {
                "zh": values.get("zh") if isinstance(values.get("zh"), str) else None,
                "ja": values.get("ja") if isinstance(values.get("ja"), str) else None,
                "source_ref": f"translations/db_names.json#/{entity}/{json_pointer_token(name)}",
            }
    return result, manifest


def mapping_by_id(mud3_maps: list[dict[str, Any]], godot_maps: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    godot_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mud3_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in godot_maps:
        if row.get("FileName") is not None:
            godot_by_key[normalize_map_id(row["FileName"])].append(row)
    for row in mud3_maps:
        mud3_by_key[row["normalized_map_id"]].append(row)

    matched: dict[str, dict[str, Any]] = {}
    used_godot: set[str] = set()
    used_mud3: set[str] = set()
    for mud3 in mud3_maps:
        key = mud3["normalized_map_id"]
        m_rows = mud3_by_key[key]
        g_rows = godot_by_key.get(key, [])
        if len(m_rows) == 1 and len(g_rows) == 1:
            godot = g_rows[0]
            matched[mud3["source_ref"]] = godot
            used_godot.add(godot["SourceRef"])
            used_mud3.add(mud3["source_ref"])
    return matched, used_mud3, used_godot


def comparison_row(
    entity_type: str,
    entity_key: str,
    field: str,
    mud3_value: Any,
    godot_value: Any,
    website_value: Any,
    mud3_ref: str | None,
    godot_ref: str | None,
    website_ref: str | None,
    status: str,
    confidence: float,
    basis: str,
    note: str,
) -> dict[str, Any]:
    refs = [reference for reference in (mud3_ref, godot_ref, website_ref) if reference]
    stable = json.dumps(
        [entity_type, entity_key, field, refs, mud3_value, godot_value, website_value, status],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "comparison_id": "cmp-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16],
        "entity_type": entity_type,
        "entity_key": entity_key,
        "field": field,
        "mud3_value": mud3_value,
        "godot_value": godot_value,
        "website_value": website_value,
        "mud3_ref": mud3_ref,
        "godot_ref": godot_ref,
        "website_ref": website_ref,
        "status": status,
        "mapping_confidence": round(confidence, 2),
        "mapping_basis": basis,
        "note": note,
    }


def text_status(left: str | None, right: str | None) -> str:
    if left == right:
        return "identical"
    if normalize_text(left) == normalize_text(right):
        return "format_only"
    return "explicit_difference"


def map_comparisons(
    mud3_maps: list[dict[str, Any]], godot_maps: list[dict[str, Any]], minimaps: list[dict[str, Any]], translations: dict[str, dict[str, dict[str, Any]]], issues: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    godot_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mud3_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    minimap_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in godot_maps:
        godot_by_key[normalize_map_id(row.get("FileName"))].append(row)
    for row in mud3_maps:
        mud3_by_key[row["normalized_map_id"]].append(row)
    for row in minimaps:
        minimap_by_key[row["normalized_map_id"]].append(row)

    mud3_to_godot: dict[str, dict[str, Any]] = {}
    map_summaries: list[dict[str, Any]] = []
    consumed_godot: set[str] = set()

    for mud3 in mud3_maps:
        key = mud3["normalized_map_id"]
        client_rows = godot_by_key.get(key, [])
        same_mud3_id = mud3_by_key[key]
        if len(same_mud3_id) == 1 and len(client_rows) == 1:
            godot = client_rows[0]
            mud3_to_godot[mud3["source_ref"]] = godot
            consumed_godot.add(godot["SourceRef"])
            exact_id = mud3["map_id"] == godot.get("FileName")
            identity_status = "identical" if exact_id else "format_only"
            rows.append(comparison_row(
                "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "identity.map_file_name",
                mud3["map_id"], godot.get("FileName"), None, mud3["source_ref"], godot["SourceRef"], None,
                identity_status, 1.0, "稳定 Map ID / FileName 唯一匹配；名称不参与身份判定",
                "大小写归一仅用于唯一且无冲突的文件名匹配。" if not exact_id else "原始地图 ID 与客户端文件名精确一致。",
            ))
            rows.append(comparison_row(
                "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "display_name.database_raw",
                mud3["display_name"], godot.get("Description"), None, mud3["source_ref"], godot["SourceRef"], None,
                text_status(mud3["display_name"], godot.get("Description")), 1.0,
                "同一稳定地图 ID 下比较原始显示字段",
                "Godot Description 保留数据库原文；客户端中文显示另由 db_names.json 查表。",
            ))
            translation = translations["maps"].get(godot.get("Description"), {})
            translated_name = translation.get("zh")
            if translated_name is None:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "display_name.client_zh",
                    mud3["display_name"], None, None, mud3["source_ref"], godot["SourceRef"], translation.get("source_ref"),
                    "mapping_unverified", 0.85, "地图 ID 已匹配；客户端中文翻译缺项",
                    "客户端按英文 Description 回退显示；不能仅凭名称推断别名。",
                ))
            else:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "display_name.client_zh",
                    mud3["display_name"], translated_name, translated_name,
                    mud3["source_ref"], godot["SourceRef"], translation["source_ref"],
                    text_status(mud3["display_name"], translated_name), 1.0,
                    "稳定地图 ID 匹配后，引用客户端实际 zh 翻译键",
                    "名称仅比较字段值，不参与地图身份判定。",
                ))

            options = mud3["options"]
            option_names = [option["normalized"].split("(", 1)[0] for option in options]
            known = {"HORSE", "NORECALL", "NORECONNECT", "DARK", "DAY", "FIGHT", "SNOW"}
            if "HORSE" in option_names:
                matched = bool(godot.get("CanHorse"))
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.CanHorse",
                    True, bool(godot.get("CanHorse")), None, mud3["source_ref"], godot["SourceRef"], None,
                    "identical" if matched else "explicit_difference", 0.9,
                    "MUD3 HORSE 显式允许骑乘 ↔ Godot CanHorse",
                    "仅对显式 HORSE 标记比较；MUD3 未写 HORSE 时不推断 false。",
                ))
            if "NORECALL" in option_names:
                allowed = bool(godot.get("AllowRecall"))
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.AllowRecall",
                    False, allowed, None, mud3["source_ref"], godot["SourceRef"], None,
                    "identical" if not allowed else "explicit_difference", 0.85,
                    "MUD3 NORECALL 的反向布尔语义 ↔ Godot AllowRecall",
                    "只比较显式 NORECALL；没有标记不等于确认允许。",
                ))
            reconnect = [re.fullmatch(r"NORECONNECT\((.*?)\)", option["normalized"], re.IGNORECASE) for option in options]
            reconnect = [match.group(1) for match in reconnect if match]
            if len(reconnect) == 1:
                target = reconnect[0]
                godot_target = godot.get("ReconnectMap")
                if normalize_map_id(target) == normalize_map_id(godot_target):
                    status = "identical" if target == godot_target else "format_only"
                else:
                    status = "explicit_difference"
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.ReconnectMap",
                    target, godot_target, None, mud3["source_ref"], godot["SourceRef"], None,
                    status, 0.85, "MUD3 NORECONNECT(target) ↔ Godot ReconnectMap.FileName",
                    "按同一地图 ID 空间比较；语义映射按字段名作候选，不代表所有复活/重连行为。",
                ))
            elif len(reconnect) > 1:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.ReconnectMap",
                    reconnect, godot.get("ReconnectMap"), None, mud3["source_ref"], godot["SourceRef"], None,
                    "mapping_unverified", 0.5, "MUD3 MapInfo 含多个 NORECONNECT 候选",
                    "保留全部原值；未假设重复选项的优先级。",
                ))

            environment_flags = [name for name in option_names if name in {"DARK", "DAY", "SNOW", "FIGHT"}]
            if environment_flags:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.environment_flags",
                    environment_flags,
                    {"Light": godot.get("Light"), "Weather": godot.get("Weather"), "Fight": godot.get("Fight")},
                    None, mud3["source_ref"], godot["SourceRef"], None,
                    "mapping_unverified", 0.55,
                    "MUD3 环境/战斗选项与 Godot Light/Weather/Fight 候选字段",
                    "未找到足以证明这些旧版选项与客户端枚举逐值等价的字段映射；仅保留候选对照。",
                ))
            mm_rows = minimap_by_key.get(key, [])
            if mm_rows:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "setting.MiniMap_candidate",
                    [row["minimap_index"] for row in mm_rows], godot.get("MiniMap"), None,
                    ";".join(row["source_ref"] for row in mm_rows), godot["SourceRef"], None,
                    "mapping_unverified", 0.5,
                    "MUD3 MiniMap.txt 与 Godot MapInfo.MiniMap 数值候选",
                    "资源索引语义未验证；没有检查贴图、图库或渲染。",
                ))
            unknown_options = [option for option in options if option["normalized"].split("(", 1)[0] not in known]
            client_unpaired = {
                name: godot.get(name)
                for name in ("AllowRT", "AllowTT", "CanMine", "CanMarriageRecall", "SkillDelay", "MinimumLevel", "MaximumLevel", "Background", "RequiredClass")
            }
            if unknown_options or any(value not in (False, 0, "None", "All", None) for value in client_unpaired.values()):
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "metadata.without_proven_field_mapping",
                    unknown_options, client_unpaired, None, mud3["source_ref"], godot["SourceRef"], None,
                    "mapping_unverified", 0.3,
                    "MUD3 未配对选项与 Godot 其他 MapInfo 字段",
                    "不把未解释的旧版 token、客户端属性默认值或空值静默视为等价。",
                ))
            if mud3.get("header_value") is not None:
                rows.append(comparison_row(
                    "map", f"{mud3['map_id']} ↔ {godot.get('FileName')}", "metadata.mud3_header_value",
                    mud3["header_value"], None, None, mud3["source_ref"], godot["SourceRef"], None,
                    "mapping_unverified", 0.25, "MUD3 方括号记录尾部字段未找到 Godot 对应字段",
                    "保留原字符串；语义未知。",
                ))
            map_summaries.append({"mud3": mud3, "godot": godot, "mapping_status": identity_status})
        else:
            candidates = client_rows
            status = "source_missing" if not candidates and len(same_mud3_id) == 1 else "mapping_unverified"
            godot_ref = candidates[0]["SourceRef"] if len(candidates) == 1 else None
            rows.append(comparison_row(
                "map", mud3["map_id"], "entity.presence", True,
                [row.get("FileName") for row in candidates] if candidates else None,
                None, mud3["source_ref"], godot_ref, None, status, 0.4 if candidates else 1.0,
                "仅按稳定 Map ID / FileName 查找；名称不参与匹配",
                "对侧表未找到唯一对应行；不据此断言游戏运行时不存在该地图。",
            ))
            map_summaries.append({"mud3": mud3, "godot": candidates[0] if len(candidates) == 1 else None, "mapping_status": status})

    for godot in godot_maps:
        if godot["SourceRef"] in consumed_godot:
            continue
        key = normalize_map_id(godot.get("FileName"))
        if not mud3_by_key.get(key):
            rows.append(comparison_row(
                "map", str(godot.get("FileName")), "entity.presence", None,
                godot.get("FileName"), None, None, godot["SourceRef"], None,
                "source_missing", 1.0, "Godot FileName 在 MUD3 MapInfo 定义中无同 ID",
                "仅表示本次 MUD3 MapInfo 文件没有同 ID 定义，不代表 MUD3 游戏里不存在。",
            ))
            map_summaries.append({"mud3": None, "godot": godot, "mapping_status": "source_missing"})
        else:
            issue(issues, godot["SourceRef"], "ambiguous_godot_map_id", "review", map_file=godot.get("FileName"))
    return rows, mud3_to_godot, map_summaries


def exact_name_key(value: str | None) -> str | None:
    return normalize_text(value)


def add_entity_comparison(
    rows: list[dict[str, Any]], entity: str, key: str, field: str, left: Any, right: Any,
    mud3_ref: str | None, godot_ref: str | None, status: str, confidence: float, basis: str, note: str,
    website_value: Any = None, website_ref: str | None = None,
) -> None:
    rows.append(comparison_row(entity, key, field, left, right, website_value, mud3_ref, godot_ref, website_ref, status, confidence, basis, note))


def compare_npcs(
    mud3_npcs: list[dict[str, Any]], godot_npcs: list[dict[str, Any]], map_matches: dict[str, dict[str, Any]],
    translations: dict[str, dict[str, dict[str, Any]]], rows: list[dict[str, Any]], issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    godot_by_map_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for npc in godot_npcs:
        localized = translations["npcs"].get(npc.get("Name"), {}).get("zh") or npc.get("Name")
        key = (normalize_map_id(npc.get("MapFileName")) or "", exact_name_key(localized) or "")
        godot_by_map_name[key].append(npc)
    matched_godot: set[str] = set()
    output: list[dict[str, Any]] = []
    for npc in mud3_npcs:
        godot_map = next((value for source_ref, value in map_matches.items() if source_ref and normalize_map_id(value.get("FileName")) == npc["normalized_map_id"]), None)
        candidates = godot_by_map_name.get((npc["normalized_map_id"], exact_name_key(npc["npc_name"]) or ""), [])
        if len(candidates) == 1 and godot_map is not None:
            candidate = candidates[0]
            matched_godot.add(candidate["SourceRef"])
            key = f"{npc['map_id']} / {npc['npc_name']}"
            add_entity_comparison(
                rows, "npc", key, "presence.map_and_localized_name", npc["npc_name"], candidate.get("Name"),
                npc["source_ref"], candidate["SourceRef"], "identical", 0.9,
                "稳定地图 ID + 客户端实际 zh NPC 名称精确匹配；NPC 英文名由 db_names.json 映射",
                "Godot raw Name 与 MUD3 名称可处于不同语言；identity 不使用近似名称。",
            )
            point = (candidate.get("SinglePointX"), candidate.get("SinglePointY"))
            if point[0] is not None and point[1] is not None:
                same = point == (npc["x"], npc["y"])
                add_entity_comparison(
                    rows, "npc", key, "location.single_region_point", [npc["x"], npc["y"]], list(point),
                    npc["source_ref"], candidate["SourceRef"], "identical" if same else "explicit_difference", 0.9,
                    "一对一地图/中文名映射且 Godot NPC Region 恰有一个 PointRegion 坐标",
                    "仅比较单点 Region；未计算多点 Region 的中心或形状。",
                )
            else:
                add_entity_comparison(
                    rows, "npc", key, "location.coordinate", [npc["x"], npc["y"]],
                    {"region_name": candidate.get("RegionName"), "region_point_count": candidate.get("RegionPointCount")},
                    npc["source_ref"], candidate["SourceRef"], "mapping_unverified", 0.65,
                    "Godot NPC 关联 Region，不一定保存单一 NPC 坐标",
                    "不将多点区域的质心当作 NPC 原始坐标。",
                )
            output.append({"mud3": npc, "godot": candidate, "mapping_status": "matched_by_map_and_exact_zh_name"})
        else:
            status = "mapping_unverified"
            if not candidates and godot_map is not None:
                translation = next((entry for entry in translations["npcs"].values() if entry.get("zh") == npc["npc_name"]), None)
                if translation is None:
                    status = "mapping_unverified"
            if len(candidates) > 1:
                issue(issues, npc["source_ref"], "ambiguous_npc_match", "review", candidate_refs=[item["SourceRef"] for item in candidates])
            add_entity_comparison(
                rows, "npc", f"{npc['map_id']} / {npc['npc_name']}", "presence.map_and_localized_name",
                npc["npc_name"], [item.get("Name") for item in candidates] or None,
                npc["source_ref"], ";".join(item["SourceRef"] for item in candidates) if candidates else None,
                status, 0.35, "没有唯一的稳定地图 ID + 精确客户端 zh 名称对应项",
                "列为映射待核实；不把未匹配写成游戏中不存在。",
            )
            output.append({"mud3": npc, "godot_candidates": candidates, "mapping_status": status})
    for npc in godot_npcs:
        if npc["SourceRef"] in matched_godot:
            continue
        localized = translations["npcs"].get(npc.get("Name"), {}).get("zh") or npc.get("Name")
        name_matches = [
            source for source in mud3_npcs
            if source["normalized_map_id"] == normalize_map_id(npc.get("MapFileName"))
            and exact_name_key(source["npc_name"]) == exact_name_key(localized)
        ]
        if not name_matches:
            add_entity_comparison(
                rows, "npc", f"{npc.get('MapFileName')} / {localized}", "presence.map_and_localized_name",
                None, npc.get("Name"), None, npc["SourceRef"], "mapping_unverified", 0.35,
                "Godot NPC 未在 Merchant.txt 找到同地图同中文显示名",
                "MUD3 的其他 NPC 文件或别名未穷尽；不是游戏存在性结论。",
            )
    return output, matched_godot


def compare_spawns(
    mud3_spawns: list[dict[str, Any]], godot_spawns: list[dict[str, Any]], website_monsters: list[dict[str, Any]],
    map_matches: dict[str, dict[str, Any]], translations: dict[str, dict[str, dict[str, Any]]],
    rows: list[dict[str, Any]], issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], dict[str, list[dict[str, Any]]]]:
    website_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for monster in website_monsters:
        website_by_name[exact_name_key(monster["name"]) or ""].append(monster)
    godot_by_map_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for spawn in godot_spawns:
        monster_name = spawn.get("MonsterName")
        localized = translations["monsters"].get(monster_name, {}).get("zh") or monster_name
        region = spawn.get("Region") or {}
        godot_by_map_name[(normalize_map_id(region.get("MapFileName")) or "", exact_name_key(localized) or "")].append(spawn)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for spawn in mud3_spawns:
        grouped[(spawn["normalized_map_id"], exact_name_key(spawn["monster_name"]) or "")].append(spawn)
    result: list[dict[str, Any]] = []
    consumed: set[str] = set()
    mapped_mud3_sources: set[str] = set()
    for (map_key, name_key), source_rows in sorted(grouped.items()):
        first = source_rows[0]
        candidates = godot_by_map_name.get((map_key, name_key), [])
        website_matches = website_by_name.get(name_key, [])
        exact_map = any(normalize_map_id(value.get("FileName")) == map_key for value in map_matches.values())
        website_value = [{"id": row["id"], "name": row["name"], "category": row["category"]} for row in website_matches]
        website_ref = ";".join(row["source_ref"] for row in website_matches) if website_matches else None
        if len(candidates) == 1 and exact_map:
            godot = candidates[0]
            consumed.add(godot["SourceRef"])
            mapped_mud3_sources.update(row["record_id"] for row in source_rows)
            key = f"{first['map_id']} / {first['monster_name']}"
            add_entity_comparison(
                rows, "spawn", key, "presence.map_and_localized_monster_name",
                {"mud3_record_count": len(source_rows), "monster_name": first["monster_name"]},
                {"godot_respawn_record_count": len(candidates), "monster_name": godot.get("MonsterName")},
                first["source_ref"], godot["SourceRef"], "identical", 0.88,
                "稳定地图 ID + db_names.json 提供的客户端 zh 怪物名精确匹配",
                "此状态只表示地图内出现同名实体记录；不等于刷新参数/区域完全相同。",
                website_value, website_ref,
            )
            add_entity_comparison(
                rows, "spawn", key, "spawn_parameters_and_location",
                [{"xy": [row["x"], row["y"]], "parameters": row["numeric_parameters"]} for row in source_rows],
                {"region": (godot.get("Region") or {}).get("Description"), "point_count": (godot.get("Region") or {}).get("PointCount"), "count": godot.get("Count"), "delay": godot.get("Delay")},
                ";".join(row["source_ref"] for row in source_rows), godot["SourceRef"], "mapping_unverified", 0.55,
                "MUD3 Gen 坐标/三个原值参数与 Godot RespawnInfo 区域/Count/Delay 候选",
                "MUD3 旧格式数字列语义与单位未由本次数据源证明；没有用区域点集推导地图格子或位置。",
                website_value, website_ref,
            )
            result.append({"map_id": first["map_id"], "monster_name": first["monster_name"], "mud3_records": source_rows, "godot_candidates": candidates, "website_monsters": website_matches, "mapping_status": "matched_by_map_and_exact_zh_name"})
        else:
            status = "mapping_unverified"
            if len(candidates) > 1:
                issue(issues, first["source_ref"], "ambiguous_spawn_match", "review", mud3_record_refs=[row["source_ref"] for row in source_rows], godot_record_refs=[row["SourceRef"] for row in candidates])
            add_entity_comparison(
                rows, "spawn", f"{first['map_id']} / {first['monster_name']}", "presence.map_and_localized_monster_name",
                {"mud3_record_count": len(source_rows), "monster_name": first["monster_name"]},
                [{"name": item.get("MonsterName"), "ref": item["SourceRef"]} for item in candidates] or None,
                ";".join(row["source_ref"] for row in source_rows),
                ";".join(item["SourceRef"] for item in candidates) if candidates else None,
                status, 0.3, "无唯一的稳定地图 ID + 精确客户端 zh 怪物名候选",
                "映射待核实；未把未匹配写成游戏中不存在。网站分类只作怪物名称辅助。",
                website_value, website_ref,
            )
            result.append({"map_id": first["map_id"], "monster_name": first["monster_name"], "mud3_records": source_rows, "godot_candidates": candidates, "website_monsters": website_matches, "mapping_status": status})

    for spawn in godot_spawns:
        if spawn["SourceRef"] in consumed:
            continue
        region = spawn.get("Region") or {}
        name = spawn.get("MonsterName")
        map_file = region.get("MapFileName")
        if not map_file:
            issue(issues, spawn["SourceRef"], "missing_godot_spawn_region", "review")
            add_entity_comparison(
                rows, "spawn", f"unassigned / {name}", "presence.map_and_localized_monster_name",
                None, {"name": name, "count": spawn.get("Count")}, None, spawn["SourceRef"],
                "mapping_unverified", 0.3,
                "Godot System.db RespawnInfo 没有可用 Region/MapFileName，无法建立地图映射",
                "记录保留在未分配刷怪清单；不能据此与 MUD3 判定缺失或不存在。",
            )
            continue
        localized = translations["monsters"].get(name, {}).get("zh")
        display_name = localized or name
        present_in_mud3 = any(
            key == (normalize_map_id(map_file) or "", exact_name_key(display_name) or "")
            for key in grouped
        )
        if not present_in_mud3:
            status = "source_missing" if localized else "mapping_unverified"
            add_entity_comparison(
                rows, "spawn", f"{map_file} / {display_name}", "presence.map_and_localized_monster_name",
                None, {"name": name, "count": spawn.get("Count")}, None, spawn["SourceRef"],
                status, 0.88 if localized else 0.3,
                "Godot System.db RespawnInfo 地图/中文名组合在加载的 MUD3 Gen 记录中无同键" if localized else "Godot 怪物缺少精确 zh 翻译，无法建立 MUD3 名称映射",
                "仅表示本次 Mongen.txt 加载的 Gen 数据无此同键记录；不代表 MUD3 游戏中没有其他脚本刷怪来源。",
            )
    return result, consumed, dict(grouped)


def compare_connections(
    mud3_links: list[dict[str, Any]], godot_movements: list[dict[str, Any]], mud3_maps: list[dict[str, Any]],
    godot_maps: list[dict[str, Any]], rows: list[dict[str, Any]], issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    mud3_ids = {row["normalized_map_id"] for row in mud3_maps}
    godot_ids = {normalize_map_id(row.get("FileName")) for row in godot_maps}
    source_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    target_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for link in mud3_links:
        if link["from_map_key"] not in mud3_ids or link["to_map_key"] not in mud3_ids:
            issue(issues, link["source_ref"], "map_link_endpoint_without_definition", "review", from_map=link["from_map_id"], to_map=link["to_map_id"])
        source_groups[(link["from_map_key"], link["to_map_key"])].append(link)
    for movement in godot_movements:
        source, destination = movement.get("SourceRegion"), movement.get("DestinationRegion")
        if not source or not destination or not source.get("MapFileName") or not destination.get("MapFileName"):
            issue(issues, movement["SourceRef"], "movement_missing_map_region", "review")
            continue
        target_groups[(normalize_map_id(source["MapFileName"]), normalize_map_id(destination["MapFileName"]))].append(movement)

    all_keys = sorted(set(source_groups) | set(target_groups))
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for from_key, to_key in all_keys:
        source_records = source_groups.get((from_key, to_key), [])
        target_records = target_groups.get((from_key, to_key), [])
        from_id = source_records[0]["from_map_id"] if source_records else next(
            (movement["SourceRegion"]["MapFileName"] for movement in target_records), from_key
        )
        to_id = source_records[0]["to_map_id"] if source_records else next(
            (movement["DestinationRegion"]["MapFileName"] for movement in target_records), to_key
        )
        if source_records and target_records:
            status = "identical"
            confidence = 0.85
            note = "同一稳定地图 ID 的有向地图对在两套连接表中都出现；坐标/Region 粒度仍未对齐。"
            field = "directed_map_pair.presence"
        else:
            status = "source_missing"
            confidence = 1.0
            note = "该有向地图对只出现在本次读取的 MapInfo/Mongen 数据或 Godot MovementInfo；不代表游戏运行时不存在其他传送途径。"
            field = "directed_map_pair.presence"
        add_entity_comparison(
            rows, "connection", f"{from_id} → {to_id}", field,
            {"record_count": len(source_records), "references": [item["source_ref"] for item in source_records]} if source_records else None,
            {"record_count": len(target_records), "references": [item["SourceRef"] for item in target_records]} if target_records else None,
            ";".join(item["source_ref"] for item in source_records) if source_records else None,
            ";".join(item["SourceRef"] for item in target_records) if target_records else None,
            status, confidence,
            "MUD3 MapInfo 明确坐标箭头 ↔ Godot MovementInfo.SourceRegion/DestinationRegion 的稳定地图 ID 有向图",
            note,
        )
        if source_records and target_records:
            add_entity_comparison(
                rows, "connection", f"{from_id} → {to_id}", "endpoint_coordinate_mapping",
                [{"from": [item["from_x"], item["from_y"]], "to": [item["to_x"], item["to_y"]]} for item in source_records],
                [{"source_region": item["SourceRegion"].get("Description"), "source_point_count": item["SourceRegion"].get("PointCount"), "destination_region": item["DestinationRegion"].get("Description"), "destination_point_count": item["DestinationRegion"].get("PointCount")} for item in target_records],
                ";".join(item["source_ref"] for item in source_records),
                ";".join(item["SourceRef"] for item in target_records), "mapping_unverified", 0.5,
                "MUD3 单点坐标与 Godot Region/MovementInfo 的结构不同",
                "不计算区域质心或地图格子；只确认地图对出现。",
            )
        pair_row = {
            "from_map_id": from_id,
            "to_map_id": to_id,
            "from_map_key": from_key,
            "to_map_key": to_key,
            "status": status,
            "mud3_records": source_records,
            "godot_records": target_records,
        }
        pairs[from_key].append(pair_row)
        pairs[to_key].append(pair_row)
    return rows, pairs


def make_maps(
    mud3_maps: list[dict[str, Any]], godot_maps: list[dict[str, Any]], mud3_npcs: list[dict[str, Any]],
    mud3_spawns: list[dict[str, Any]], mud3_links: list[dict[str, Any]], godot_npcs: list[dict[str, Any]],
    godot_spawns: list[dict[str, Any]], godot_movements: list[dict[str, Any]], map_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    maps_by_key: dict[str, dict[str, Any]] = {}

    def add_map(key: str | None, map_id: str | None) -> None:
        if key:
            maps_by_key.setdefault(key, {"map_id": map_id, "mud3": [], "godot": []})

    for row in mud3_maps:
        key = row["normalized_map_id"]
        add_map(key, row["map_id"])
        maps_by_key[key]["mud3"].append(row)
    for row in godot_maps:
        key = normalize_map_id(row.get("FileName")) or f"godot-index-{row.get('Index')}"
        add_map(key, row.get("FileName"))
        maps_by_key[key]["godot"].append(row)
    for row in mud3_npcs + mud3_spawns:
        add_map(row.get("normalized_map_id"), row.get("map_id"))
    for row in mud3_links:
        add_map(row.get("from_map_key"), row.get("from_map_id"))
        add_map(row.get("to_map_key"), row.get("to_map_id"))
    for row in godot_npcs:
        add_map(normalize_map_id(row.get("MapFileName")), row.get("MapFileName"))
    for row in godot_spawns:
        map_id = (row.get("Region") or {}).get("MapFileName")
        add_map(normalize_map_id(map_id), map_id)
    for row in godot_movements:
        for region in (row.get("SourceRegion") or {}, row.get("DestinationRegion") or {}):
            map_id = region.get("MapFileName")
            add_map(normalize_map_id(map_id), map_id)

    summary_by_mud3_ref = {row["mud3"]["source_ref"]: row["mapping_status"] for row in map_summaries if row.get("mud3")}
    summary_by_godot_ref = {row["godot"]["SourceRef"]: row["mapping_status"] for row in map_summaries if row.get("godot") and not row.get("mud3")}
    result: list[dict[str, Any]] = []
    for key in sorted(maps_by_key):
        data = maps_by_key[key]
        mud3_records = data["mud3"]
        godot_records = data["godot"]
        status = "identical" if mud3_records and godot_records and len(mud3_records) == len(godot_records) == 1 else (
            "mapping_unverified" if mud3_records and godot_records else "source_missing"
        )
        if mud3_records and len(mud3_records) == len(godot_records) == 1:
            status = summary_by_mud3_ref.get(mud3_records[0]["source_ref"], status)
        elif godot_records and not mud3_records:
            status = summary_by_godot_ref.get(godot_records[0]["SourceRef"], "source_missing")
        key_match = lambda value: normalize_map_id(value) == key
        result.append(
            {
                "map_id": data["map_id"],
                "normalized_map_id": key,
                "mapping_status": status,
                "mud3": mud3_records[0] if len(mud3_records) == 1 else None,
                "mud3_records": mud3_records,
                "godot": godot_records[0] if len(godot_records) == 1 else None,
                "godot_records": godot_records,
                "mud3_npcs": [row for row in mud3_npcs if row["normalized_map_id"] == key],
                "mud3_spawns": [row for row in mud3_spawns if row["normalized_map_id"] == key],
                "mud3_connections": [row for row in mud3_links if row["from_map_key"] == key or row["to_map_key"] == key],
                "godot_npcs": [row for row in godot_npcs if key_match(row.get("MapFileName"))],
                "godot_spawns": [row for row in godot_spawns if key_match((row.get("Region") or {}).get("MapFileName"))],
                "godot_movements": [
                    row for row in godot_movements
                    if key_match((row.get("SourceRegion") or {}).get("MapFileName"))
                    or key_match((row.get("DestinationRegion") or {}).get("MapFileName"))
                ],
            }
        )
    return result


def source_inventory(
    envir: Path, website_data: Path, db_path: Path, translations_path: Path, gen_loads: list[dict[str, Any]],
    resolved_loads: list[dict[str, Any]], db_version: str | None,
) -> list[dict[str, Any]]:
    entries = [
        file_manifest(envir / "MapInfo.txt", "mud3-map-info", "MapInfo.txt", "GB18030 strict"),
        file_manifest(envir / "Mongen.txt", "mud3-monster-loader", "Mongen.txt", "GB18030 strict"),
        file_manifest(envir / "Merchant.txt", "mud3-npc-index", "Merchant.txt", "GB18030 strict"),
        file_manifest(envir / "MiniMap.txt", "mud3-minimap-index", "MiniMap.txt", "GB18030 strict"),
        file_manifest(website_data / "monsters.json", "website-monsters", "monsters.json", "UTF-8"),
        file_manifest(website_data / "maps.json", "website-map-catalog", "maps.json", "UTF-8"),
        file_manifest(website_data / "alignment/entities/map.json", "website-map-research-snapshot", "alignment/entities/map.json", "UTF-8"),
        file_manifest(website_data / "alignment/entities/map_area.json", "website-map-area-research-snapshot", "alignment/entities/map_area.json", "UTF-8"),
        file_manifest(translations_path, "godot-name-translations", "translations/db_names.json", "UTF-8"),
        file_manifest(db_path, "godot-system-db", "System.db", "MirDB binary"),
    ]
    seen: set[str] = set()
    for load in resolved_loads:
        if not load.get("resolved_file") or load["resolved_file"] in seen:
            continue
        seen.add(load["resolved_file"])
        entries.append(
            file_manifest(envir / "Mon_Def" / load["resolved_file"], "mud3-generator", load["resolved_file"], "GB18030 strict")
        )
    for entry in entries:
        entry["version"] = db_version if entry["source_id"] == "godot-system-db" else None
    return entries


def validate_audit(audit: dict[str, Any], csv_rows: list[dict[str, str]] | None = None) -> None:
    comparisons = audit.get("comparisons")
    if not isinstance(comparisons, list):
        raise ValueError("audit.comparisons must be a list")
    status_counts = Counter(row.get("status") for row in comparisons)
    if any(status not in STATUSES for status in status_counts):
        raise ValueError("comparison row has an unknown status")
    reported = audit.get("summary", {}).get("comparison_status_counts", {})
    if dict(sorted(status_counts.items())) != reported:
        raise ValueError("comparison status counts do not close")
    if sum(reported.values()) != len(comparisons):
        raise ValueError("comparison status total does not equal detail rows")

    ids = [row.get("comparison_id") for row in comparisons]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("comparison IDs are missing or duplicated")
    for row in comparisons:
        refs = [row.get(key) for key in ("mud3_ref", "godot_ref", "website_ref") if row.get(key)]
        if not refs:
            raise ValueError(f"comparison has no source reference: {row.get('comparison_id')}")
        if not 0 <= row.get("mapping_confidence", -1) <= 1:
            raise ValueError(f"invalid mapping confidence: {row.get('comparison_id')}")

    integrity = audit.get("integrity", {})
    records = audit.get("records", {})
    for entity, key in (("mud3_maps", "mud3_maps"), ("mud3_connections", "mud3_connections"), ("mud3_npcs", "mud3_npcs"), ("mud3_spawns", "mud3_spawns"), ("godot_maps", "godot_maps"), ("godot_npcs", "godot_npcs"), ("godot_movements", "godot_movements"), ("godot_spawns", "godot_spawns")):
        values = records.get(key, [])
        if len(values) != integrity.get("record_counts", {}).get(key):
            raise ValueError(f"record count mismatch for {key}")
        record_refs = []
        for value in values:
            record_ref = value.get("record_id") or value.get("source_ref") or value.get("SourceRef")
            if not record_ref:
                raise ValueError(f"record missing source reference in {key}")
            record_refs.append(record_ref)
        if len(record_refs) != len(set(record_refs)):
            raise ValueError(f"source records collapsed or duplicated in {key}")

    issues = audit.get("issues", [])
    if any(not row.get("source_ref") for row in issues):
        raise ValueError("parse issue missing source reference")
    serialized = json.dumps(audit, ensure_ascii=False)
    for forbidden in ("/home/", "/data/NAS/", "/Users/"):
        if forbidden in serialized:
            raise ValueError(f"public output contains a private path pattern: {forbidden}")

    if csv_rows is not None:
        if len(csv_rows) != len(comparisons):
            raise ValueError("CSV comparison row count does not equal JSON details")
        csv_by_id = {row.get("comparison_id"): row for row in csv_rows}
        if set(csv_by_id) != set(ids):
            raise ValueError("CSV comparison IDs do not match JSON")
        for row in comparisons:
            if csv_by_id[row["comparison_id"]].get("status") != row["status"]:
                raise ValueError("CSV comparison status does not match JSON")


def build_audit(
    mud3_maps: list[dict[str, Any]], mud3_links: list[dict[str, Any]], minimaps: list[dict[str, Any]],
    loads: list[dict[str, Any]], resolved_loads: list[dict[str, Any]], mud3_spawns: list[dict[str, Any]],
    mud3_npcs: list[dict[str, Any]], godot: dict[str, Any], website_monsters: list[dict[str, Any]],
    website_areas: list[dict[str, Any]], research_maps: list[dict[str, Any]], research_areas: list[dict[str, Any]],
    translations: dict[str, dict[str, dict[str, Any]]], issues: list[dict[str, Any]], manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    godot_maps = godot["Maps"]
    godot_npcs = godot["Npcs"]
    godot_movements = godot["Movements"]
    godot_spawns = godot["Respawns"]
    comparisons: list[dict[str, Any]] = []
    map_compare_rows, map_matches, map_summaries = map_comparisons(mud3_maps, godot_maps, minimaps, translations, issues)
    comparisons.extend(map_compare_rows)
    npc_details, _ = compare_npcs(mud3_npcs, godot_npcs, map_matches, translations, comparisons, issues)
    spawn_details, _, _ = compare_spawns(mud3_spawns, godot_spawns, website_monsters, map_matches, translations, comparisons, issues)
    _, connection_pairs = compare_connections(mud3_links, godot_movements, mud3_maps, godot_maps, comparisons, issues)
    comparisons.sort(key=lambda row: (row["entity_type"], row["entity_key"], row["field"], row["comparison_id"]))

    for connection in mud3_links:
        source_maps = [row for row in mud3_maps if row["normalized_map_id"] == connection["from_map_key"]]
        target_maps = [row for row in mud3_maps if row["normalized_map_id"] == connection["to_map_key"]]
        connection["from_map_name"] = source_maps[0]["display_name"] if len(source_maps) == 1 else None
        connection["to_map_name"] = target_maps[0]["display_name"] if len(target_maps) == 1 else None

    website_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for monster in website_monsters:
        website_by_name[exact_name_key(monster["name"]) or ""].append(monster)
    for spawn in mud3_spawns:
        matches = website_by_name.get(exact_name_key(spawn["monster_name"]) or "", [])
        spawn["website_monster_matches"] = [
            {"id": row["id"], "name": row["name"], "category": row["category"], "source_ref": row["source_ref"], "name_ref": row["name_ref"], "category_ref": row["category_ref"]}
            for row in matches
        ]
        spawn["website_match_confidence"] = 1.0 if len(matches) == 1 else (0.5 if matches else 0.0)
        spawn["website_match_basis"] = "精确中文名匹配；仅用于名称/分类辅助，不是 MUD3 刷怪证据" if matches else "无精确名称命中；未推断别名"

    maps = make_maps(mud3_maps, godot_maps, mud3_npcs, mud3_spawns, mud3_links, godot_npcs, godot_spawns, godot_movements, map_summaries)
    status_counts = dict(sorted(Counter(row["status"] for row in comparisons).items()))
    exact_map_keys = {
        mud3["normalized_map_id"] for mud3 in mud3_maps
        if sum(row["normalized_map_id"] == mud3["normalized_map_id"] for row in mud3_maps) == 1
        and sum(row.get("FileName") == mud3["map_id"] for row in godot_maps) == 1
    }
    mud3_unique_pairs = {(row["from_map_key"], row["to_map_key"]) for row in mud3_links}
    godot_unique_pairs = {
        (normalize_map_id(row["SourceRegion"]["MapFileName"]), normalize_map_id(row["DestinationRegion"]["MapFileName"]))
        for row in godot_movements if row.get("SourceRegion") and row.get("DestinationRegion")
        and row["SourceRegion"].get("MapFileName") and row["DestinationRegion"].get("MapFileName")
    }
    exact_name_website_matches = sum(bool(spawn["website_monster_matches"]) for spawn in mud3_spawns)
    all_equivalent_statuses = {"identical", "format_only"}
    fully_identical = bool(comparisons) and all(row["status"] in all_equivalent_statuses for row in comparisons)

    records = {
        "mud3_maps": mud3_maps,
        "mud3_connections": mud3_links,
        "mud3_minimaps": minimaps,
        "mud3_generator_loads": resolved_loads,
        "mud3_npcs": mud3_npcs,
        "mud3_spawns": mud3_spawns,
        "godot_maps": godot_maps,
        "godot_npcs": godot_npcs,
        "godot_movements": godot_movements,
        "godot_spawns": godot_spawns,
        "unassigned_godot_spawns": [
            row for row in godot_spawns
            if not (row.get("Region") or {}).get("MapFileName")
        ],
        "website_monsters": website_monsters,
        "website_map_areas": website_areas,
        "website_map_research_snapshot": research_maps,
        "website_map_area_research_candidates": research_areas,
        "map_details": maps,
        "npc_matches": npc_details,
        "spawn_matches": spawn_details,
    }
    record_counts = {key: len(records[key]) for key in records if isinstance(records[key], list)}
    for key in ("mud3_maps", "mud3_connections", "mud3_npcs", "mud3_spawns", "godot_maps", "godot_npcs", "godot_movements", "godot_spawns"):
        record_counts.setdefault(key, len(records[key]))
    public_issues = sorted(issues, key=lambda row: (row.get("source_ref", ""), row.get("code", ""), row.get("record_sha256", "")))

    audit = {
        "schema_version": 1,
        "decision": {
            "fully_identical_in_audited_fields": fully_identical,
            "conclusion": "本次字段范围内未发现未等价字段。" if fully_identical else "未通过完全一致判定：存在明确差异、源记录不对称或映射待核实项。",
            "scope": "配置/客户端数据库中的地图定义、连接、NPC 与怪物刷新数据；不含 .map 文件、地图格子、贴图或渲染。",
        },
        "method": {
            "map_identity": "MUD3 MapInfo 方括号首字段 ↔ Godot MapInfo.FileName；只按唯一稳定 ID 匹配。大小写折叠仅用于无冲突候选并记为格式差异。地图名称只作辅助，不建立身份映射。",
            "npc_identity": "MUD3 Merchant.txt 地图 ID + NPC 名；Godot NPCInfo 地图 FileName + NPC 名称的实际 zh 翻译（db_names.json）。仅精确且唯一候选可建立名称关联。",
            "spawn_identity": "MUD3 loadgen 所引用 Gen 记录的地图 ID + 怪物名；Godot System.db RespawnInfo 地图 FileName + MonsterInfo 的实际 zh 翻译。Website 怪物分类只作精确名称辅助，不作刷怪事实。",
            "connection_identity": "MUD3 MapInfo.txt 的有向坐标箭头；Godot System.db MovementInfo 的 SourceRegion/DestinationRegion 地图 ID 有向对。地图对可以比较；区域/端点坐标粒度不同的项目标为待核实。",
            "deduplication": "源行按文件/行号保留，不对地图、NPC、Gen 刷新或连接记录去重。只有有向地图对统计采用集合口径；重复边在源记录明细中仍逐行保留。",
            "empty_values": "空字段、缺失字段、false、0 分开处理。MapInfo 未写某选项不推断其含义；对侧表无行标记 source_missing，不解释为游戏运行时不存在。",
            "formatting": "保留所有原始标识/显示值；另提供 NFKC+空白折叠+casefold 的名称辅助规范值，以及地图 ID 的 casefold 键。",
            "encoding": "MUD3 配置按 GB18030 严格解码；本次 MapInfo/Mongen/Merchant/MiniMap 字节均可严格解码，GBK 也能解码这些字节，无法仅据当前样本区分两者。网站与 Godot JSON 按 UTF-8 严格解析。",
            "godot_runtime_source": "当前客户端 NetworkManager 调用 DatabaseLoader；其 System.db 加载器读取 LibraryCore MirDB MapInfo/NPCInfo/MovementInfo 等表。静态审计使用相同 MirDB 模型读取实际 System.db；SessionMode.None 避免加载 Users.db 或触发 System 表迁移写入。GodotClient DataLayer 的 frame/effect/sound JSON 不含地图主数据。",
            "research_snapshot": "website alignment/entities/map.json 与 map_area.json 的身份候选仅作为带待复核状态的辅助资料输出，不参与 Godot 权威数据读取或地图 ID 匹配。",
        },
        "source_manifest": manifest,
        "godot_database_version": godot.get("SystemDatabaseVersion"),
        "summary": {
            "record_counts": record_counts,
            "mud3_unique_directed_map_pairs": len(mud3_unique_pairs),
            "godot_unique_directed_movement_map_pairs": len(godot_unique_pairs),
            "shared_directed_map_pairs": len(mud3_unique_pairs & godot_unique_pairs),
            "exact_unique_map_id_matches": len(exact_map_keys),
            "website_exact_monster_name_hits_in_mud3_spawn_rows": exact_name_website_matches,
            "comparison_row_count": len(comparisons),
            "comparison_status_counts": status_counts,
            "parse_issue_count": len(public_issues),
            "parse_issue_severity_counts": dict(sorted(Counter(row["severity"] for row in public_issues).items())),
            "fully_identical": fully_identical,
        },
        "integrity": {
            "record_counts": {key: record_counts[key] for key in ("mud3_maps", "mud3_connections", "mud3_npcs", "mud3_spawns", "godot_maps", "godot_npcs", "godot_movements", "godot_spawns")},
            "comparison_statuses_close": sum(status_counts.values()) == len(comparisons),
            "all_comparisons_have_source_refs": all(any(row.get(field) for field in ("mud3_ref", "godot_ref", "website_ref")) for row in comparisons),
            "source_record_deduplication": "none",
        },
        "records": records,
        "comparisons": comparisons,
        "issues": public_issues,
    }
    audit["integrity"]["all_source_record_refs_unique"] = all(
        len([row.get("record_id") or row.get("source_ref") or row.get("SourceRef") for row in records[key]])
        == len(set(row.get("record_id") or row.get("source_ref") or row.get("SourceRef") for row in records[key]))
        for key in audit["integrity"]["record_counts"]
    )
    validate_audit(audit)
    return audit


def export_godot_snapshot(db_path: Path, zircon_repo: Path, repo_root: Path) -> dict[str, Any]:
    if not db_path.is_file():
        raise ValueError("Godot System.db input does not exist")
    library_project = zircon_repo / "LibraryCore" / "LibraryCore.csproj"
    if not library_project.is_file():
        raise ValueError("Zircon repository does not contain LibraryCore/LibraryCore.csproj")
    before = sha256_file(db_path)
    project = repo_root / "tools" / "system_db_exporter" / "Program.csproj"
    artifacts = repo_root / ".artifacts"
    build = subprocess.run(
        [
            "dotnet", "build", str(project), "--configuration", "Release",
            f"-p:ZIRCON_REPO={zircon_repo}", "-p:UseArtifactsOutput=true",
            f"-p:ArtifactsPath={artifacts}", "--verbosity", "quiet",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if build.returncode:
        raise RuntimeError("Godot DB exporter build failed:\n" + (build.stdout + build.stderr)[-12000:])
    assembly = artifacts / "bin" / "Program" / "release" / "Program.dll"
    if not assembly.is_file():
        raise RuntimeError("Godot DB exporter assembly was not created under the repository artifact directory")
    run = subprocess.run(["dotnet", str(assembly), "--database", str(db_path)], cwd=repo_root, text=True, capture_output=True, check=False)
    if run.returncode:
        raise RuntimeError("Godot DB exporter failed:\n" + (run.stderr or run.stdout)[-12000:])
    after = sha256_file(db_path)
    if before != after:
        raise RuntimeError("Godot System.db changed while it was being read; rerun after source writers stop")
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Godot DB exporter produced invalid JSON") from error


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def write_csv(path: Path, comparisons: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=COMPARISON_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in comparisons:
            writer.writerow({
                **row,
                "mud3_value": json.dumps(row["mud3_value"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "godot_value": json.dumps(row["godot_value"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "website_value": json.dumps(row["website_value"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            })


def extract(args: argparse.Namespace) -> None:
    envir = args.mud3_envir.resolve()
    website_data = args.website_data.resolve()
    db_path = args.godot_system_db.resolve()
    zircon_repo = args.zircon_repo.resolve()
    repo_root = Path(__file__).resolve().parents[1]
    issues: list[dict[str, Any]] = []

    mud3_maps, mud3_links = parse_map_info(envir / "MapInfo.txt", issues)
    minimaps = parse_minimap(envir / "MiniMap.txt", issues)
    loads = parse_mongen(envir / "Mongen.txt", issues)
    mud3_spawns, resolved_loads = parse_generators(envir, loads, issues)
    mud3_npcs = parse_merchants(envir / "Merchant.txt", issues)
    website_monsters, website_areas, research_maps, research_areas, _ = parse_website_sources(website_data, issues)
    translations, _ = parse_translations(zircon_repo / "GodotClient" / "translations" / "db_names.json", issues)
    godot = export_godot_snapshot(db_path, zircon_repo, repo_root)
    manifest = source_inventory(envir, website_data, db_path, zircon_repo / "GodotClient" / "translations" / "db_names.json", loads, resolved_loads, godot.get("SystemDatabaseVersion"))
    audit = build_audit(
        mud3_maps, mud3_links, minimaps, loads, resolved_loads, mud3_spawns, mud3_npcs,
        godot, website_monsters, website_areas, research_maps, research_areas, translations, issues, manifest,
    )
    output = args.output.resolve()
    write_json(output / "audit.json", audit)
    write_json(output / "issues.json", audit["issues"])
    write_csv(output / "comparisons.csv", audit["comparisons"])
    validate_file(output / "audit.json", output / "comparisons.csv", output / "issues.json")
    print(json.dumps({"output": "audit.json, comparisons.csv, issues.json", "summary": audit["summary"]}, ensure_ascii=False, sort_keys=True))


def validate_file(audit_path: Path, csv_path: Path, issues_path: Path | None = None) -> None:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    with csv_path.open(newline="", encoding="utf-8") as source:
        csv_rows = list(csv.DictReader(source))
    validate_audit(audit, csv_rows)
    if issues_path is not None:
        published_issues = json.loads(issues_path.read_text(encoding="utf-8"))
        if published_issues != audit.get("issues", []):
            raise ValueError("issues.json does not match audit.json issue details")
    print(json.dumps({"valid": True, "comparisons": len(csv_rows), "status_counts": audit["summary"]["comparison_status_counts"], "issues": len(audit.get("issues", []))}, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="MUD3/Godot map-data audit extractor and validator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract_parser = subparsers.add_parser("extract", help="read source data and create public audit outputs")
    extract_parser.add_argument("--mud3-envir", type=Path, required=True)
    extract_parser.add_argument("--website-data", type=Path, required=True)
    extract_parser.add_argument("--godot-system-db", type=Path, required=True)
    extract_parser.add_argument("--zircon-repo", type=Path, required=True)
    extract_parser.add_argument("--output", type=Path, default=Path("site/data"))
    extract_parser.set_defaults(func=extract)

    validate_parser = subparsers.add_parser("validate", help="validate committed machine-readable outputs")
    validate_parser.add_argument("--data", type=Path, default=Path("site/data/audit.json"))
    validate_parser.add_argument("--csv", type=Path, default=Path("site/data/comparisons.csv"))
    validate_parser.add_argument("--issues", type=Path, default=Path("site/data/issues.json"))
    validate_parser.set_defaults(func=lambda args: validate_file(args.data, args.csv, args.issues))

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
