using System.Reflection;
using System.Text.Json;
using Library.MirDB;
using Library.SystemModels;
using MirDB;

if (args.Length != 2 || args[0] != "--database")
{
    Console.Error.WriteLine("Usage: SystemDbExporter --database <System.db>");
    return 2;
}

string databasePath = Path.GetFullPath(args[1]);
if (!File.Exists(databasePath) || !string.Equals(Path.GetFileName(databasePath), "System.db", StringComparison.OrdinalIgnoreCase))
{
    Console.Error.WriteLine("Expected an existing file named System.db.");
    return 2;
}

string root = Path.GetDirectoryName(databasePath)! + Path.DirectorySeparatorChar;
var session = new Session(SessionMode.None, root) { BackUp = false };
session.Initialize(Assembly.GetAssembly(typeof(MapInfo))!);
if (!session.SystemDatabaseExists)
{
    Console.Error.WriteLine("MirDB did not load a System database.");
    return 1;
}

var maps = session.GetCollection<MapInfo>().Binding
    .OrderBy(x => x.Index)
    .Select(x => new MapRow(
        x.Index, x.FileName, x.Description, x.MiniMap, x.Light.ToString(), x.Weather.ToString(),
        x.Fight.ToString(), x.AllowRT, x.AllowTT, x.CanHorse, x.CanMine, x.CanMarriageRecall,
        x.AllowRecall, x.SkillDelay, x.MinimumLevel, x.MaximumLevel,
        x.ReconnectMap?.FileName, x.Music.ToString(), x.Background, x.RequiredClass.ToString(),
        $"System.db#MapInfo.Index={x.Index}"))
    .ToArray();

var regions = session.GetCollection<MapRegion>().Binding
    .OrderBy(x => x.Index)
    .Select(x => ToRegion(x)!)
    .ToArray();

var npcs = session.GetCollection<NPCInfo>().Binding
    .OrderBy(x => x.Index)
    .Select(x => new NpcRow(
        x.Index, x.NPCName, x.Region?.Map?.FileName, x.Region?.Index, x.Region?.Description,
        x.Region?.RegionType.ToString(), PointCount(x.Region), SingleX(x.Region), SingleY(x.Region),
        x.Category.ToString(), x.MapIcon.ToString(), x.GoodsIndex, x.EntryPage?.Description,
        $"System.db#NPCInfo.Index={x.Index}"))
    .ToArray();

var movements = session.GetCollection<MovementInfo>().Binding
    .OrderBy(x => x.Index)
    .Select(x => new MovementRow(
        x.Index, ToRegion(x.SourceRegion), ToRegion(x.DestinationRegion), x.Icon.ToString(),
        x.NeedItem?.Index, x.NeedSpawn?.Monster?.MonsterName, x.NeedHole,
        x.NeedInstance?.Name, x.Effect.ToString(), x.RequiredClass.ToString(), x.SkipValidation,
        $"System.db#MovementInfo.Index={x.Index}"))
    .ToArray();

var respawns = session.GetCollection<RespawnInfo>().Binding
    .OrderBy(x => x.Index)
    .Select(x => new SpawnRow(
        x.Index, x.Monster?.MonsterName, ToRegion(x.Region), x.EventSpawn, x.Delay, x.Count,
        x.DropSet, x.Announce, x.EasterEventChance, x.RespawnIndex,
        $"System.db#RespawnInfo.Index={x.Index}"))
    .ToArray();

var snapshot = new Snapshot(session.SystemDatabaseVersion, maps, regions, npcs, movements, respawns);
var options = new JsonSerializerOptions { WriteIndented = true };
Console.OutputEncoding = System.Text.Encoding.UTF8;
Console.Write(JsonSerializer.Serialize(snapshot, options));
return 0;

static RegionRow? ToRegion(MapRegion? region)
{
    if (region is null) return null;
    return new RegionRow(
        region.Index, region.Map?.FileName, region.Description, region.RegionType.ToString(),
        region.Size, PointCount(region), SingleX(region), SingleY(region),
        $"System.db#MapRegion.Index={region.Index}");
}

static int PointCount(MapRegion? region) => region?.PointRegion?.Length ?? 0;
static int? SingleX(MapRegion? region) => PointCount(region) == 1 ? region!.PointRegion![0].X : null;
static int? SingleY(MapRegion? region) => PointCount(region) == 1 ? region!.PointRegion![0].Y : null;

sealed record Snapshot(
    string? SystemDatabaseVersion,
    MapRow[] Maps,
    RegionRow[] Regions,
    NpcRow[] Npcs,
    MovementRow[] Movements,
    SpawnRow[] Respawns);

sealed record MapRow(
    int Index, string? FileName, string? Description, int MiniMap, string Light, string Weather,
    string Fight, bool AllowRT, bool AllowTT, bool CanHorse, bool CanMine, bool CanMarriageRecall,
    bool AllowRecall, int SkillDelay, int MinimumLevel, int MaximumLevel,
    string? ReconnectMap, string Music, int Background, string RequiredClass, string SourceRef);

sealed record RegionRow(
    int Index, string? MapFileName, string? Description, string RegionType, int Size, int PointCount,
    int? SinglePointX, int? SinglePointY, string SourceRef);

sealed record NpcRow(
    int Index, string? Name, string? MapFileName, int? RegionIndex, string? RegionName,
    string? RegionType, int RegionPointCount, int? SinglePointX, int? SinglePointY,
    string Category, string MapIcon, int GoodsIndex, string? EntryPage, string SourceRef);

sealed record MovementRow(
    int Index, RegionRow? SourceRegion, RegionRow? DestinationRegion, string Icon,
    int? NeedItemIndex, string? NeedMonsterName, bool NeedHole, string? NeedInstance,
    string Effect, string RequiredClass, bool SkipValidation, string SourceRef);

sealed record SpawnRow(
    int Index, string? MonsterName, RegionRow? Region, bool EventSpawn, int Delay, int Count,
    int DropSet, bool Announce, int EasterEventChance, int RespawnIndex, string SourceRef);
