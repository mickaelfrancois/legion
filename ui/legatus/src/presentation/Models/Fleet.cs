namespace IA.Legatus.Models;

// In-memory aggregate of every battle shard in fleet.d/ (doc §2). Built by FleetReader;
// not itself deserialized from disk.
public sealed class Fleet
{
    public List<FleetEntry> Battles { get; set; } = [];
}

// One battle shard (fleet.d/<sha1>.json holds exactly one of these). Nullable members
// may be absent on entries written before a schema enrichment (doc §2): title, profile,
// ticket, pr_url, repo_path, updated.
public sealed class FleetEntry
{
    public string Id { get; set; } = "";
    public string? Repo { get; set; }
    public string? RepoPath { get; set; }      // repo_path — host-native separators (doc §5)
    public string? Ticket { get; set; }
    public string? Title { get; set; }
    public string? Profile { get; set; }
    public Phase Phase { get; set; }            // current phase
    public PhaseStatus Status { get; set; }     // status of the current phase
    // SnakeCaseLower naming maps this to the wire key battle_status (doc §2).
    public BattleStatus BattleStatus { get; set; } // global: active | blocked | closed
    public string? PrUrl { get; set; }
    public long? TokensTotal { get; set; }      // tokens_total = input + output; absent if nothing yet
    public TokenUsage? Tokens { get; set; }     // breakdown (cache tracked separately)
    public List<string> Skills { get; set; } = []; // skills actually used (main + subagents)
    public DateTimeOffset? Updated { get; set; }
}

// Token cost breakdown. tokens_total is input+output; cache is tracked apart (doc §2).
public sealed class TokenUsage
{
    public long Input { get; set; }
    public long Output { get; set; }
    public long CacheRead { get; set; }      // cache_read
    public long CacheCreation { get; set; }  // cache_creation
}
