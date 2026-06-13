namespace IA.Legatus.Services;

// Resolves on-disk locations of legion artifacts. The only place that knows the index
// location and how to compose a battle directory from an index entry.
public static class LegionPaths
{
    private const string FleetEnvVar = "LEGION_FLEET";

    // Base location: LEGION_FLEET if set, else %USERPROFILE%/.claude/legion.
    public static string BaseDirectory()
    {
        var baseDir = Environment.GetEnvironmentVariable(FleetEnvVar);
        if (!string.IsNullOrWhiteSpace(baseDir))
            return baseDir;

        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".claude", "legion");
    }

    // The sharded index directory <base>/fleet.d/ — one <sha1>.json per battle (doc §2).
    public static string FleetDirectory() => Path.Combine(BaseDirectory(), "fleet.d");

    // <repo_path>/.legion/battles/<id>/ — normalizing host-native separators first (doc §5).
    public static string BattleDirectory(string repoPath, string id)
        => Path.Combine(NormalizeSeparators(repoPath), ".legion", "battles", id);

    // repo_path is stored with the producing host's separators (Windows backslashes);
    // rewrite to the current platform before composing a path (doc §5).
    public static string NormalizeSeparators(string path)
        => path.Replace('\\', Path.DirectorySeparatorChar)
               .Replace('/', Path.DirectorySeparatorChar);
}
