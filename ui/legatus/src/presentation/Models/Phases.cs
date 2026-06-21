namespace IA.Legatus.Models;

// The fixed pipeline order (doc §4):
// THINK → PLAN → BUILD → LINT → REVIEW → TEST → DELIVER → ADDRESS → REFLECT.
// LINT is the first review-cascade gate (.NET formatting check, before REVIEW).
// ADDRESS is optional + repeatable (post-deliver PR-review loop); it stays in the
// frieze as a slot that is simply pending when a battle never draws review comments.
public static class Phases
{
    public static readonly IReadOnlyList<Phase> Pipeline =
    [
        Phase.Think, Phase.Plan, Phase.Build, Phase.Lint, Phase.Review,
        Phase.Test, Phase.Deliver, Phase.Address, Phase.Reflect,
    ];

    public static int IndexOf(Phase phase) => Array.IndexOf(Pipeline as Phase[] ?? [.. Pipeline], phase);

    public static string Label(Phase phase) => phase.ToString().ToUpperInvariant();
}
