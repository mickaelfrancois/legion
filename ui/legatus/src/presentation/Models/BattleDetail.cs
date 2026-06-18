namespace IA.Legatus.Models;

// The per-battle source of truth: <repo_path>/.legion/battles/<id>/battle.json (doc §3.1).
// The fleet shard is only a projection of this; per-phase detail (verdicts, artifacts) lives here.
public sealed class BattleDetail
{
    public string Id { get; set; } = "";
    public string? Repo { get; set; }
    public string? Ticket { get; set; }
    public string? Title { get; set; }
    public string? Profile { get; set; }
    public List<string> RequiredGates { get; set; } = [];
    public Dictionary<string, PhaseState> Phases { get; set; } = [];
    public Guard? Guard { get; set; }
    public Delivery? Delivery { get; set; }

    // Phase keys are stored as snake_case strings (think, plan, …); look them up by enum.
    public PhaseState? PhaseFor(Phase phase)
        => Phases.TryGetValue(phase.ToString().ToLowerInvariant(), out var state) ? state : null;
}

public sealed class PhaseState
{
    public PhaseStatus Status { get; set; }
    public string? Artifact { get; set; }
    public Verdict Verdict { get; set; }   // only meaningful for gate phases (plan/review/test/security)

    // ADDRESS phase only (doc §4): post-deliver, repeatable. No single verdict — a wave
    // counter and the PR review threads. Both stay default/empty for every other phase.
    public int Round { get; set; }
    public List<FeedbackThread> Threads { get; set; } = [];
}

// One PR review thread tracked by the ADDRESS phase (doc §4). Read defensively — the
// producer guarantees none of these in particular.
public sealed class FeedbackThread
{
    public string? Id { get; set; }
    public string? Target { get; set; }
    public string? Kind { get; set; }
    public string? Commit { get; set; }
    public string? Resolution { get; set; }   // fixed | active | wontFix
}

public sealed class Guard
{
    public List<string> Allow { get; set; } = [];
    public List<string> Deny { get; set; } = [];
    public bool Careful { get; set; }
}

public sealed class Delivery
{
    public string? PrUrl { get; set; }
}
