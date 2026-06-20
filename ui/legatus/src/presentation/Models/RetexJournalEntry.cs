using System.Text.Json.Serialization;

namespace IA.Legatus.Models;

// One line of the central tooling RETEX journal (plugin-retex.jsonl) written by
// scripts/plugin_retex.py. A line is either a RETEX entry or a tombstone
// {type:"resolved", id} that closes one. The producer stamps each entry with a stable
// Id derived from content (sha1("ts|plugin|observation")[:12]); the reader fills Id and
// IsResolved. Read defensively — only plugin/observation/suggestion are guaranteed.
public sealed class RetexJournalEntry
{
    public string? Id { get; set; }
    public string? Type { get; set; }          // "resolved" => tombstone; null/absent => entry
    public string? Ts { get; set; }
    public string? Plugin { get; set; }         // legion | dotnet-claude-kit | ...
    public string? Battle { get; set; }         // battle id that surfaced this friction
    public string? Repo { get; set; }
    // Battle context (optional) — keeps the entry self-contained once the repo/worktree
    // is gone. Set by plugin_retex.py from battle.json (Title/Profile) + spec.md (Intent);
    // Phase is per-entry (where the friction surfaced). None of these affect the derived Id.
    public string? Title { get; set; }          // battle title
    public string? Intent { get; set; }         // one-line battle intent (from spec.md)
    public string? Phase { get; set; }          // phase where the friction surfaced
    public string? Profile { get; set; }        // battle profile (feature|hotfix|...)
    public string? Area { get; set; }           // gate:/hook:/command:/skill:/script:
    public string? Severity { get; set; }       // blocker | friction | annoyance | idea
    public string? Observation { get; set; }
    public string? Suggestion { get; set; }
    public string? Note { get; set; }           // tombstone-only resolution note

    // Set by the reader (matched against tombstones), not deserialized.
    [JsonIgnore]
    public bool IsResolved { get; set; }
}
