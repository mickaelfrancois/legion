using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using IA.Legatus.Models;

namespace IA.Legatus.Services;

// Reads the central tooling RETEX journal (plugin-retex.jsonl, append-only), read-only and
// resilient: a missing file yields no entries and a malformed line is skipped. Mirrors the
// producer's plugin_retex.py: tombstones {type:"resolved", id} mark entries resolved, and an
// entry's id is derived from sha1("ts|plugin|observation")[:12] when not stored — so legacy
// lines without an id still match their tombstones.
public sealed class RetexJournalReader
{
    public async Task<IReadOnlyList<RetexJournalEntry>> ReadAsync(CancellationToken ct = default)
    {
        var path = LegionPaths.PluginRetexJournal();
        if (!File.Exists(path))
            return [];

        string[] lines;
        try
        {
            lines = await File.ReadAllLinesAsync(path, ct);
        }
        catch (IOException)
        {
            return [];
        }

        var entries = new List<RetexJournalEntry>();
        var resolved = new HashSet<string>(StringComparer.Ordinal);
        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
                continue;

            RetexJournalEntry? record;
            try
            {
                record = JsonSerializer.Deserialize<RetexJournalEntry>(line, LegionJson.Options);
            }
            catch (JsonException)
            {
                continue;   // skip a malformed/partial line; the rest stays valid
            }
            if (record is null)
                continue;

            if (string.Equals(record.Type, "resolved", StringComparison.Ordinal))
                resolved.Add(DeriveId(record));
            else
            {
                record.Id = DeriveId(record);
                entries.Add(record);
            }
        }

        foreach (var entry in entries)
            entry.IsResolved = resolved.Contains(entry.Id!);

        return entries;
    }

    // Stable id, mirroring plugin_retex.py: an explicit id wins, else sha1 of
    // "ts|plugin|observation" truncated to 12 hex chars (deterministic, legacy lines included).
    private static string DeriveId(RetexJournalEntry e)
    {
        if (!string.IsNullOrWhiteSpace(e.Id))
            return e.Id.Trim();

        var seed = $"{e.Ts}|{e.Plugin}|{e.Observation}";
        var hash = SHA1.HashData(Encoding.UTF8.GetBytes(seed));
        return Convert.ToHexString(hash).ToLowerInvariant()[..12];
    }
}
