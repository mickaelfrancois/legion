using System.Text.Json;
using IA.Legatus.Models;

namespace IA.Legatus.Services;

// Reads the sharded index, read-only: every *.json in fleet.d/ holds one battle entry
// (doc §2). Entries are aggregated and deduped by repo_path::id (most recently updated
// wins). Every failure mode degrades gracefully: a missing directory means "no battles
// tracked yet", and a JSON/IO error on one shard skips that shard (caught mid-write by
// its owning session) without losing the others.
public sealed class FleetReader
{
    public async Task<Fleet> ReadAsync(CancellationToken ct = default)
    {
        var directory = LegionPaths.FleetDirectory();
        if (!Directory.Exists(directory))
            return new Fleet();

        string[] files;
        try
        {
            files = Directory.GetFiles(directory, "*.json");
        }
        catch (IOException)
        {
            return new Fleet();
        }

        var entries = new List<FleetEntry>();
        foreach (var file in files)
        {
            var entry = await ReadShardAsync(file, ct);
            if (entry is { Id.Length: > 0 })
                entries.Add(entry);
        }

        // Shards are one-per-battle, but dedup defensively: same repo_path::id keeps the freshest.
        var deduped = entries
            .GroupBy(e => $"{e.RepoPath}::{e.Id}", StringComparer.OrdinalIgnoreCase)
            .Select(g => g.OrderByDescending(e => e.Updated ?? DateTimeOffset.MinValue).First())
            .ToList();

        return new Fleet { Battles = deduped };
    }

    private static async Task<FleetEntry?> ReadShardAsync(string path, CancellationToken ct)
    {
        try
        {
            await using var stream = File.OpenRead(path);
            return await JsonSerializer.DeserializeAsync<FleetEntry>(stream, LegionJson.Options, ct);
        }
        catch (JsonException)   // partial/corrupt content during a shard rewrite
        {
            return null;
        }
        catch (IOException)     // shard locked while its session writes
        {
            return null;
        }
    }
}
