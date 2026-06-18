namespace IA.Legatus.Models;

public sealed record ArtifactSpec(string FileName, string Title, Phase Phase);

// The Markdown artifacts of a battle, in reading order (doc §3). An absent file means
// the phase hasn't been reached yet — not an error. Each artifact is tied to the
// pipeline phase that produces it, so the battle view can fold the others away.
public static class Artifacts
{
    public static readonly IReadOnlyList<ArtifactSpec> Catalog =
    [
        new("spec.md", "Spécification", Phase.Think),
        new("plan.md", "Plan", Phase.Plan),
        new("build-report.md", "Rapport de build", Phase.Build),
        new("gate-review.md", "Gate — revue", Phase.Review),
        new("gate-security.md", "Gate — sécurité", Phase.Review),
        new("gate-test.md", "Gate — tests", Phase.Test),
        new("pr-body.md", "Corps de PR", Phase.Deliver),
        new("wi-comment.md", "Note issue", Phase.Deliver),
        new("pr-feedback.md", "Retours de PR", Phase.Address),
        new("retro.md", "Rétrospective", Phase.Reflect),
    ];
}
