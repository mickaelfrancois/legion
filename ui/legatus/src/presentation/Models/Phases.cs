namespace IA.Legatus.Models;

// The fixed pipeline order (doc §4):
// THINK → PLAN → BUILD → REVIEW → TEST → DELIVER → REFLECT.
public static class Phases
{
    public static readonly IReadOnlyList<Phase> Pipeline =
    [
        Phase.Think, Phase.Plan, Phase.Build, Phase.Review,
        Phase.Test, Phase.Deliver, Phase.Reflect,
    ];

    public static int IndexOf(Phase phase) => Array.IndexOf(Pipeline as Phase[] ?? [.. Pipeline], phase);

    public static string Label(Phase phase) => phase.ToString().ToUpperInvariant();
}
