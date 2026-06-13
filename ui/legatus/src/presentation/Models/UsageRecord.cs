namespace IA.Legatus.Models;

// One line of usage.jsonl (append-only): a single contribution to a battle's cost,
// from the main agent or a subagent (doc §2/§3). The UI aggregates these for live detail.
public sealed class UsageRecord
{
    public string? Scope { get; set; }       // "main" | "subagent" (free string — read defensively)
    public string? AgentType { get; set; }   // agent_type, present for subagent contributions
    public List<string> Skills { get; set; } = [];
    public TokenUsage Tokens { get; set; } = new();
}
