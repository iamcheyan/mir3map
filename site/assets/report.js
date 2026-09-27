const $ = (selector) => document.querySelector(selector);
const nf = new Intl.NumberFormat("zh-CN");
const statusNames = {
  identical: "字段一致",
  explicit_difference: "明确差异",
  format_only: "仅格式差异",
  mapping_unverified: "映射待核实",
  source_missing: "本次源记录缺失",
};
const entityNames = {
  map: "地图定义",
  connection: "地图连接",
  npc: "NPC",
  spawn: "怪物刷新",
};
const issueNames = {
  ambiguous_godot_map_id: "Godot 地图 ID 重复",
  ambiguous_spawn_match: "刷怪候选不唯一",
  duplicate_mud3_map_id: "MUD3 地图 ID 重复",
  incomplete_map_link_endpoint: "地图连接端点不完整",
  malformed_map_link: "地图连接无法解析",
  malformed_map_definition: "地图定义无法解析",
  malformed_minimap_record: "MiniMap 记录无法解析",
  map_option_continuation: "地图选项续行需复核",
  missing_godot_spawn_region: "Godot 刷怪缺少地图区域",
  missing_or_unsafe_generator_file: "刷怪文件无法安全解析",
  generator_reference_case_difference: "刷怪文件名大小写不同",
  unparsed_generator_record: "Gen 刷怪行无法解析",
  unparsed_mongen_record: "Mongen 行无法解析",
  malformed_merchant_record: "商人 / NPC 行无法解析",
};
const labels = {
  identical: "ID 精确匹配",
  format_only: "ID 仅格式差异",
  mapping_unverified: "身份待核实",
  source_missing: "一侧无地图定义",
  explicit_difference: "字段明确不同",
};

let audit = null;
let mapIndex = [];
let comparisonIndex = [];
let mapLimit = 18;
let comparisonLimit = 14;
let mapTimer = 0;
let comparisonTimer = 0;
let websiteIndex = [];
let websiteLimit = 20;
let websiteTimer = 0;
let issueTimer = 0;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}

function number(value) {
  return nf.format(Number(value || 0));
}

function normalize(value) {
  return String(value ?? "").normalize("NFKC").toLocaleLowerCase("zh-CN");
}

function statusChip(status, count) {
  const chip = node("span", `status-chip status-${status}`);
  chip.append(node("span", "", statusNames[status] || status));
  if (count !== undefined) chip.append(node("b", "", number(count)));
  return chip;
}

function identityChip(status) {
  const chip = node("span", `identity-chip status-${status}`);
  chip.textContent = labels[status] || status;
  return chip;
}

function reference(parent, ref) {
  if (!ref) return;
  parent.append(node("span", "record-ref", ref));
}

function recordItem(primary, secondary, ref) {
  const li = node("li", "map-detail-row");
  li.append(node("span", "source-value", primary));
  if (secondary) li.append(node("span", "source-value", secondary));
  reference(li, ref);
  return li;
}

function detailBlock(title, entries) {
  if (!entries.length) return null;
  const section = node("section", "map-detail-block");
  section.append(node("h4", "", title));
  const list = node("ul", "map-detail-list");
  for (const entry of entries) list.append(entry);
  section.append(list);
  return section;
}

function mapSearchText(row) {
  const parts = [row.map_id, row.mapping_status];
  for (const map of row.mud3_records) parts.push(map.display_name, map.map_id, map.source_ref);
  for (const map of row.godot_records) parts.push(map.Description, map.FileName, map.SourceRef);
  for (const item of row.mud3_npcs) parts.push(item.npc_name, item.map_id, item.definition_label, item.source_ref);
  for (const item of row.mud3_spawns) parts.push(item.monster_name, item.map_id, item.source_ref, item.website_monster_matches?.map((match) => match.category).join(" "));
  for (const item of row.mud3_connections) parts.push(item.from_map_id, item.to_map_id, item.source_ref);
  for (const item of row.godot_npcs) parts.push(item.Name, item.MapFileName, item.RegionName, item.SourceRef);
  for (const item of row.godot_spawns) parts.push(item.MonsterName, item.SourceRef, item.Region?.Description);
  for (const item of row.godot_movements) parts.push(item.SourceRegion?.Description, item.DestinationRegion?.Description, item.SourceRef);
  return normalize(parts.filter(Boolean).join(" "));
}

function createMapCard(row) {
  const card = node("article", "map-card");
  const head = node("div", "map-card-head");
  const titleRow = node("div", "map-card-title-row");
  const title = node("h3");
  title.append(node("span", "map-id", row.map_id || "未命名"));
  const definitions = [...row.mud3_records.map((item) => item.display_name), ...row.godot_records.map((item) => item.Description)].filter(Boolean);
  title.append(document.createTextNode(definitions[0] || "地图数据记录"));
  titleRow.append(title, identityChip(row.mapping_status));
  head.append(titleRow);
  if (definitions.length > 1) head.append(node("p", "map-card-name", [...new Set(definitions)].join(" / ")));

  const counts = [
    ["MUD3 NPC", row.mud3_npcs.length], ["Godot NPC", row.godot_npcs.length],
    ["MUD3 刷新", row.mud3_spawns.length], ["Godot 刷新", row.godot_spawns.length],
    ["MUD3 连接", row.mud3_connections.length], ["Godot Movement", row.godot_movements.length],
  ];
  const metrics = node("div", "map-card-stats");
  for (const [label, value] of counts) {
    const item = node("span", "mini-count");
    item.append(document.createTextNode(`${label} `), node("strong", "", number(value)));
    metrics.append(item);
  }
  head.append(metrics);
  card.append(head);

  const details = node("details");
  const summary = node("summary", "", "展开源定义、关联 NPC、刷新与连接");
  details.append(summary);
  const content = node("div", "map-card-content");
  const mud3Maps = row.mud3_records.map((item) => recordItem(
    `${item.display_name || "未命名"} · header=${item.header_value ?? "—"}`,
    `选项：${item.options.map((option) => `${option.raw} [${option.source_ref}]`).join("、") || "无"}`,
    item.source_ref,
  ));
  const godotMaps = row.godot_records.map((item) => recordItem(
    `${item.Description || "无描述"} · MiniMap ${item.MiniMap} · ${item.Weather}/${item.Light}/${item.Fight}`,
    `传送 ${item.AllowRT}/${item.AllowTT} · 骑马 ${item.CanHorse} · 挖矿 ${item.CanMine} · 等级 ${item.MinimumLevel}–${item.MaximumLevel} · 音乐 ${item.Music}`,
    item.SourceRef,
  ));
  const mud3Npcs = row.mud3_npcs.map((item) => recordItem(
    `${item.npc_name} · (${item.x}, ${item.y}) · ${item.definition_label}`,
    `原始附加字段：${item.opaque_fields.join(" / ")}`,
    item.source_ref,
  ));
  const godotNpcs = row.godot_npcs.map((item) => recordItem(
    `${item.Name || "未命名"} · ${item.RegionName || "无区域名"} · ${item.Category} / ${item.MapIcon}`,
    `区域点数 ${item.RegionPointCount} · 单点 (${item.SinglePointX ?? "—"}, ${item.SinglePointY ?? "—"}) · 入口 ${item.EntryPage || "—"}`,
    item.SourceRef,
  ));
  const mud3Spawns = row.mud3_spawns.map((item) => {
    const website = item.website_monster_matches?.length
      ? `网站精确名称辅助：${item.website_monster_matches.map((match) => `${match.category} (${match.source_ref})`).join("、")}`
      : "网站怪物清单无精确名称命中";
    return recordItem(
      `${item.monster_name} · (${item.x}, ${item.y}) · 原值参数 ${item.numeric_parameters.join(" / ")}`,
      `${website} · load ${item.load_order}`,
      `${item.record_id} · ${item.source_ref}`,
    );
  });
  const godotSpawns = row.godot_spawns.map((item) => recordItem(
    `${item.MonsterName || "未命名"} · ${item.Region?.Description || "无区域说明"} · count ${item.Count} · delay ${item.Delay}`,
    `Event ${item.EventSpawn} · pointCount ${item.Region?.PointCount ?? "—"} · 事件索引 ${item.RespawnIndex}`,
    item.SourceRef,
  ));
  const mud3Connections = row.mud3_connections.map((item) => recordItem(
    `${item.from_map_id} (${item.from_x}, ${item.from_y}) → ${item.to_map_id} (${item.to_x}, ${item.to_y})`,
    "MUD3 MapInfo 有向坐标连接；箭头方向按源记录保留",
    item.source_ref,
  ));
  const godotMovements = row.godot_movements.map((item) => {
    const from = item.SourceRegion;
    const to = item.DestinationRegion;
    const source = `${from?.MapFileName || "?"} / ${from?.Description || "无源区域"}`;
    const destination = `${to?.MapFileName || "?"} / ${to?.Description || "无目标区域"}`;
    const requirements = [item.NeedItemIndex != null ? `物品 ${item.NeedItemIndex}` : "", item.NeedMonsterName ? `怪物 ${item.NeedMonsterName}` : "", item.NeedHole ? "NeedHole" : "", item.NeedInstance || ""].filter(Boolean).join(" · ") || "无显式门槛字段";
    return recordItem(
      `${source} → ${destination}`,
      `Region 点数 ${from?.PointCount ?? "—"} → ${to?.PointCount ?? "—"} · 图标 ${item.Icon} · ${requirements}`,
      item.SourceRef,
    );
  });
  for (const block of [
    detailBlock("MUD3 地图定义与选项", mud3Maps), detailBlock("Godot MapInfo", godotMaps),
    detailBlock("MUD3 NPC / Merchant", mud3Npcs), detailBlock("Godot NPCInfo", godotNpcs),
    detailBlock("MUD3 Gen 刷新", mud3Spawns), detailBlock("Godot RespawnInfo", godotSpawns),
    detailBlock("MUD3 MapInfo 连接", mud3Connections), detailBlock("Godot MovementInfo", godotMovements),
  ]) if (block) content.append(block);
  if (!content.children.length) content.append(node("p", "empty-state", "两侧均无可展示的实体明细；请查看比较记录与解析异常。"));
  details.append(content);
  card.append(details);
  return card;
}

function comparisonSearchText(row) {
  return normalize([
    row.comparison_id, row.entity_type, row.entity_key, row.field, row.status,
    row.mapping_basis, row.note, row.mud3_ref, row.godot_ref, row.website_ref,
    JSON.stringify(row.mud3_value), JSON.stringify(row.godot_value), JSON.stringify(row.website_value),
  ].filter(Boolean).join(" "));
}

function valuePanel(title, value) {
  const panel = node("section", "comparison-value");
  panel.append(node("h4", "", title));
  const output = node("pre", "source-value");
  output.textContent = value == null ? "—" : JSON.stringify(value, null, 2);
  panel.append(output);
  return panel;
}

function refText(label, value) {
  const entry = node("span");
  entry.append(node("b", "", `${label}：`), document.createTextNode(value || "—"));
  return entry;
}

function createComparisonCard(row) {
  const card = node("article", "comparison-card");
  const head = node("div", "comparison-card-head");
  const entity = node("div", "comparison-entity");
  entity.append(node("small", "", `${entityNames[row.entity_type] || row.entity_type} · ${row.comparison_id}`));
  entity.append(node("h3", "", row.entity_key || "未命名记录"));
  entity.append(node("p", "", `字段：${row.field}`));
  head.append(entity, statusChip(row.status));
  card.append(head);

  const body = node("div", "comparison-card-body");
  const values = node("div", "comparison-note");
  values.append(valuePanel("MUD3", row.mud3_value), valuePanel("Godot", row.godot_value), valuePanel("网站辅助", row.website_value));
  body.append(values);
  const explanation = node("div", "comparison-explanation");
  if (row.mapping_basis) explanation.append(node("p", "", `匹配依据：${row.mapping_basis}`));
  if (row.note) explanation.append(node("p", "", row.note));
  explanation.append(node("span", "confidence-meter", `映射置信度 ${Math.round(row.mapping_confidence * 100)}%`));
  body.append(explanation);
  card.append(body);

  const refs = node("details");
  refs.append(node("summary", "", "查看来源引用"));
  const refList = node("div", "comparison-refs");
  refList.append(refText("MUD3", row.mud3_ref), refText("Godot", row.godot_ref), refText("网站", row.website_ref));
  refs.append(refList);
  card.append(refs);
  return card;
}

function mapFilter() {
  const query = normalize($("#map-search").value.trim());
  const status = $("#map-status").value;
  const found = mapIndex.filter((entry) => (!status || entry.row.mapping_status === status) && (!query || entry.search.includes(query)));
  const visible = found.slice(0, mapLimit);
  $("#map-results").replaceChildren(...visible.map((entry) => createMapCard(entry.row)));
  $("#map-count").textContent = `显示 ${number(visible.length)} / ${number(found.length)} 张地图卡片 · 全部 ${number(mapIndex.length)}`;
  $("#map-more").hidden = visible.length >= found.length;
  $("#map-empty").hidden = found.length !== 0;
}

function comparisonFilter() {
  const query = normalize($("#comparison-search").value.trim());
  const type = $("#comparison-type").value;
  const status = $("#comparison-status").value;
  const found = comparisonIndex.filter((entry) => (!type || entry.row.entity_type === type) && (!status || entry.row.status === status) && (!query || entry.search.includes(query)));
  const visible = found.slice(0, comparisonLimit);
  $("#comparison-results").replaceChildren(...visible.map((entry) => createComparisonCard(entry.row)));
  $("#comparison-count").textContent = `显示 ${number(visible.length)} / ${number(found.length)} 条比较记录`;
  $("#comparison-more").hidden = visible.length >= found.length;
  $("#comparison-empty").hidden = found.length !== 0;
}

function addOptions(select, values, labelFor) {
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labelFor(value);
    select.append(option);
  }
}

function renderOverview() {
  const summary = audit.summary;
  const counts = summary.record_counts;
  const identical = summary.fully_identical;
  $("#verdict-value").textContent = identical ? "字段范围内一致" : "未完全一致";
  $("#verdict-copy").textContent = identical
    ? "在本次明确覆盖的数据字段中，比较结果均为 identical 或 format_only。"
    : `本次 ${number(summary.comparison_row_count)} 条逐项比较包含明确差异、源记录不对称或映射待核实项；结论不外推至地图格子或游戏运行时。`;
  $("#data-status").textContent = `已验证 ${number(summary.comparison_row_count)} 条比较明细与 ${number(summary.parse_issue_count)} 项复核记录`;
  $("#db-version").textContent = `System.db ${audit.godot_database_version || "版本未标记"}`;
  const cards = [
    ["地图身份候选", `${number(counts.mud3_maps)} / ${number(counts.godot_maps)}`, `${number(summary.exact_unique_map_id_matches)} 个唯一稳定 ID 精确匹配 · 地图卡片 ${number(counts.map_details)}`],
    ["NPC 记录", `${number(counts.mud3_npcs)} / ${number(counts.godot_npcs)}`, "MUD3 Merchant ↔ Godot NPCInfo；精确名称辅助"],
    ["怪物刷新记录", `${number(counts.mud3_spawns)} / ${number(counts.godot_spawns)}`, `${number(counts.unassigned_godot_spawns)} 条 Godot RespawnInfo 缺少地图 Region`],
    ["地图连接记录", `${number(counts.mud3_connections)} / ${number(counts.godot_movements)}`, `${number(summary.mud3_unique_directed_map_pairs)} / ${number(summary.godot_unique_directed_movement_map_pairs)} 个唯一有向地图对`],
  ];
  const grid = $("#stats-grid");
  grid.replaceChildren(...cards.map(([label, value, note]) => {
    const card = node("article", "stat-card");
    card.append(node("span", "stat-label", label), node("strong", "stat-value", value), node("span", "stat-note", note));
    return card;
  }));
  const statuses = Object.entries(summary.comparison_status_counts);
  $("#comparison-total").textContent = `共 ${number(summary.comparison_row_count)} 项`;
  $("#status-counts").replaceChildren(...statuses.map(([status, count]) => statusChip(status, count)));
}

function renderUnassigned() {
  const rows = audit.records.unassigned_godot_spawns || [];
  const target = $("#unassigned-list");
  if (!rows.length) {
    target.append(node("p", "empty-state", "本次快照没有缺少地图 Region 的 Godot 刷怪记录。"));
    return;
  }
  for (const row of rows) {
    const card = node("article", "unassigned-card");
    card.append(node("h3", "", row.MonsterName || "未命名怪物"));
    card.append(node("p", "", `RespawnInfo Index ${row.Index} · Count ${row.Count} · Delay ${row.Delay} · Event ${row.EventSpawn}`));
    card.append(node("p", "", "Region / MapFileName 缺失；无法按地图 ID 与 MUD3 刷新表建立候选。"));
    reference(card, row.SourceRef);
    target.append(card);
  }
}

function initializeWebsiteIndex() {
  const records = audit.records;
  const summaryCards = [
    [records.website_monsters.length, `网站怪物名称 · 精确命中 ${number(audit.summary.website_exact_monster_name_hits_in_mud3_spawn_rows)} 条 MUD3 刷新行`],
    [records.website_map_areas.length, "官网 maps.json 地图区块名称"],
    [records.website_map_research_snapshot.length, "map.json 研究快照候选 · 非客户端权威"],
    [records.website_map_area_research_candidates.length, "map_area.json 待复核候选"],
  ];
  $("#website-stats").replaceChildren(...summaryCards.map(([value, label]) => {
    const item = node("article", "website-stat");
    item.append(node("strong", "", number(value)), node("span", "", label));
    return item;
  }));

  websiteIndex = [
    ...records.website_map_areas.map((row) => ({ kind: "catalog", row })),
    ...records.website_map_research_snapshot.map((row) => ({ kind: "research_map", row })),
    ...records.website_map_area_research_candidates.map((row) => ({ kind: "research_area", row })),
  ].map((entry) => ({ ...entry, search: websiteSearchText(entry) }));
}

function websiteSearchText(entry) {
  const row = entry.row;
  return normalize([
    entry.kind, row.name, row.catalog_title, row.identity_role, row.research_name,
    row.map_file_candidate, row.translation_candidate?.zh, row.candidate_name,
    row.assessment, row.authority, row.source_ref,
  ].filter(Boolean).join(" "));
}

function websiteCandidate(entry) {
  const row = entry.row;
  const card = node("article", "website-candidate");
  const head = node("div", "website-candidate-head");
  const title = entry.kind === "research_area" ? row.candidate_name : row.research_name;
  const kind = entry.kind === "research_area" ? "map_area 候选" : "map 候选";
  head.append(node("h4", "", title || "未命名研究候选"), node("span", "candidate-kind", kind));
  card.append(head);
  card.append(node("p", "", `候选 FileName：${row.map_file_candidate || "—"} · 状态：${row.assessment || "未定"}`));
  if (row.translation_candidate?.zh) card.append(node("p", "", `研究快照 zh 名称：${row.translation_candidate.zh}`));
  if (row.authority) card.append(node("p", "", row.authority));
  reference(card, row.source_ref);
  return card;
}

function renderWebsite() {
  const query = normalize($("#website-search").value.trim());
  const kind = $("#website-kind").value;
  const found = websiteIndex.filter((entry) => (!kind || entry.kind === kind) && (!query || entry.search.includes(query)));
  const catalogs = found.filter((entry) => entry.kind === "catalog");
  const candidates = found.filter((entry) => entry.kind !== "catalog");
  const visibleCandidates = candidates.slice(0, websiteLimit);
  const catalogBlock = $(".catalog-block");
  catalogBlock.hidden = kind === "research_map" || kind === "research_area";
  const catalogTarget = $("#website-area-catalog");
  catalogTarget.replaceChildren(...catalogs.map(({ row }) => {
    const tag = node("span", "catalog-tag");
    tag.append(document.createTextNode(row.name), node("small", "", `${row.catalog_title} · ${row.source_ref}`));
    return tag;
  }));
  $("#website-candidates").replaceChildren(...visibleCandidates.map(websiteCandidate));
  $("#website-count").textContent = kind === "catalog"
    ? `显示 ${number(catalogs.length)} / ${number(catalogs.length)} 个官网目录区块`
    : `显示 ${number(visibleCandidates.length)} / ${number(candidates.length)} 条研究候选`;
  $("#website-more").hidden = visibleCandidates.length >= candidates.length;
  $("#website-empty").hidden = found.length !== 0;
}

function renderIssues() {
  const allRows = audit.issues || [];
  const query = normalize($("#issue-search").value.trim());
  const rows = allRows.filter((row) => !query || normalize([
    row.code, issueNames[row.code], row.source_ref, JSON.stringify(row),
  ].join(" ")).includes(query));
  const counts = audit.summary.parse_issue_severity_counts || {};
  $("#issue-summary").textContent = `${number(rows.length)} / ${number(allRows.length)} 项显示 · ${Object.entries(counts).map(([severity, count]) => `${severity} ${number(count)}`).join(" / ") || "无"}`;
  const target = $("#issue-list");
  target.replaceChildren();
  for (const row of rows) {
    const card = node("article", "issue-card");
    const head = node("div", "issue-head");
    const severity = node("span", "issue-severity", row.severity === "error" ? "解析错误" : "待复核");
    head.append(node("h3", "", issueNames[row.code] || row.code), severity);
    card.append(head, node("p", "issue-ref", row.source_ref));
    const metadata = Object.entries(row).filter(([key]) => !["code", "source_ref", "severity", "record_sha256"].includes(key));
    if (metadata.length) card.append(node("p", "issue-meta", metadata.map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join(" · ")));
    if (row.record_sha256) card.append(node("p", "fingerprint", `SHA-256 ${row.record_sha256} · ${row.record_characters ?? "?"} chars`));
    target.append(card);
  }
  $("#issue-empty").hidden = rows.length !== 0;
}

function renderManifest() {
  const body = $("#manifest-body");
  for (const row of audit.source_manifest) {
    const tr = document.createElement("tr");
    for (const value of [row.source_id, row.file, row.encoding || "—", `${number(row.size_bytes)} B`, row.modified_utc || "—", row.sha256]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    body.append(tr);
  }
  $("#snapshot-note").textContent = `Godot System.db 版本：${audit.godot_database_version || "未标记"}。website map / map_area 的研究快照仅输出候选，不参与地图身份判定。`;
}

function initializeFilters() {
  const mapStatuses = [...new Set(audit.records.map_details.map((row) => row.mapping_status))].sort();
  addOptions($("#map-status"), mapStatuses, (value) => labels[value] || value);
  const types = [...new Set(audit.comparisons.map((row) => row.entity_type))].sort();
  addOptions($("#comparison-type"), types, (value) => entityNames[value] || value);
  const statuses = [...new Set(audit.comparisons.map((row) => row.status))].sort();
  addOptions($("#comparison-status"), statuses, (value) => statusNames[value] || value);
  addOptions($("#website-kind"), ["catalog", "research_map", "research_area"], (value) => ({
    catalog: "官网地图目录",
    research_map: "map.json 研究候选",
    research_area: "map_area.json 待复核",
  })[value]);
}

function bindControls() {
  $("#map-search").addEventListener("input", () => {
    window.clearTimeout(mapTimer);
    mapLimit = 18;
    mapTimer = window.setTimeout(mapFilter, 90);
  });
  $("#map-status").addEventListener("change", () => { mapLimit = 18; mapFilter(); });
  $("#map-reset").addEventListener("click", () => {
    $("#map-search").value = "";
    $("#map-status").value = "";
    mapLimit = 18;
    mapFilter();
    $("#map-search").focus();
  });
  $("#map-more").addEventListener("click", () => { mapLimit += 18; mapFilter(); });
  for (const selector of ["#comparison-search", "#comparison-type", "#comparison-status"]) {
    $(selector).addEventListener(selector === "#comparison-search" ? "input" : "change", () => {
      window.clearTimeout(comparisonTimer);
      comparisonLimit = 14;
      comparisonTimer = window.setTimeout(comparisonFilter, 90);
    });
  }
  $("#comparison-more").addEventListener("click", () => { comparisonLimit += 14; comparisonFilter(); });
  $("#website-search").addEventListener("input", () => {
    window.clearTimeout(websiteTimer);
    websiteLimit = 20;
    websiteTimer = window.setTimeout(renderWebsite, 90);
  });
  $("#website-kind").addEventListener("change", () => { websiteLimit = 20; renderWebsite(); });
  $("#website-more").addEventListener("click", () => { websiteLimit += 20; renderWebsite(); });
  $("#issue-search").addEventListener("input", () => {
    window.clearTimeout(issueTimer);
    issueTimer = window.setTimeout(renderIssues, 70);
  });
}

async function start() {
  try {
    const response = await fetch("data/audit.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    audit = await response.json();
    renderOverview();
    initializeWebsiteIndex();
    initializeFilters();
    mapIndex = audit.records.map_details.map((row) => ({ row, search: mapSearchText(row) }));
    comparisonIndex = audit.comparisons.map((row) => ({ row, search: comparisonSearchText(row) }));
    renderUnassigned();
    renderWebsite();
    renderIssues();
    renderManifest();
    bindControls();
    mapFilter();
    comparisonFilter();
    document.documentElement.dataset.auditReady = "true";
  } catch (error) {
    $("#data-status").textContent = "审计数据读取失败";
    $("#verdict-value").textContent = "数据暂不可用";
    $("#verdict-copy").textContent = `请确认已通过 HTTP(S) 提供静态文件，然后重试。(${error.message})`;
    $("#map-count").textContent = "审计数据读取失败";
    $("#comparison-count").textContent = "审计数据读取失败";
    document.documentElement.dataset.auditReady = "error";
  }
}

start();
