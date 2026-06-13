using System.Text.Json;
using IA.Legatus.Models;

namespace IA.Legatus.Services;

// Reads usage.jsonl (append-only, one JSON object per line) for live cost/skills detail
// (doc §3). Read-only and resilient: a missing file yields no records, and a malformed
// line (e.g. a half-written final append) is skipped without losing the rest.
public sealed class UsageReader
{
    public async Task<IReadOnlyList<UsageRecord>> ReadAsync(string repoPath, string id, CancellationToken ct = default)
    {
        var path = Path.Combine(LegionPaths.BattleDirectory(repoPath, id), "usage.jsonl");
        if (!File.Exists(path))
            return [];

        var records = new List<UsageRecord>();
        string[] lines;
        try
        {
            lines = await File.ReadAllLinesAsync(path, ct);
        }
        catch (IOException)
        {
            return records;
        }

        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
                continue;
            try
            {
                var record = JsonSerializer.Deserialize<UsageRecord>(line, LegionJson.Options);
                if (record is not null)
                    records.Add(record);
            }
            catch (JsonException)
            {
                // Skip a malformed/partial line; the rest stays valid.
            }
        }

        return records;
    }
}
