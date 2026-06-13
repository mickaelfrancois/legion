using System.Text.Json;
using IA.Legatus.Models;

namespace IA.Legatus.Services;

// Reads a single battle's artifacts, read-only, from <repo_path>/.legion/battles/<id>/.
// Distinguishes a missing repo (moved/deleted host, doc §5) from a missing battle so the
// UI can show the right message, and never throws on absent/locked/corrupt files.
public sealed class BattleReader
{
    public bool RepoExists(string repoPath)
        => Directory.Exists(LegionPaths.NormalizeSeparators(repoPath));

    public async Task<BattleDetail?> ReadDetailAsync(string repoPath, string id, CancellationToken ct = default)
    {
        var path = Path.Combine(LegionPaths.BattleDirectory(repoPath, id), "battle.json");
        if (!File.Exists(path))
            return null;

        try
        {
            await using var stream = File.OpenRead(path);
            return await JsonSerializer.DeserializeAsync<BattleDetail>(stream, LegionJson.Options, ct);
        }
        catch (JsonException) { return null; }
        catch (IOException) { return null; }
    }

    // Reads a single artifact's raw Markdown; null if the file is absent (phase not reached).
    public async Task<string?> ReadArtifactAsync(string repoPath, string id, string fileName, CancellationToken ct = default)
    {
        var path = Path.Combine(LegionPaths.BattleDirectory(repoPath, id), fileName);
        if (!File.Exists(path))
            return null;

        try
        {
            return await File.ReadAllTextAsync(path, ct);
        }
        catch (IOException) { return null; }
    }
}
