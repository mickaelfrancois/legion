using IA.Legatus.Models;
using MudBlazor;

namespace IA.Legatus.Ui;

// Single source of truth for how statuses map to colors and French labels in the UI.
// Tweak the switch arms here to change the whole app's visual language.
public static class StatusVisuals
{
    public static Color ColorOf(PhaseStatus status) => status switch
    {
        PhaseStatus.Done => Color.Success,
        PhaseStatus.InProgress => Color.Info,
        PhaseStatus.Blocked => Color.Error,
        PhaseStatus.Pending => Color.Default,
        _ => Color.Default,
    };

    public static Color ColorOf(BattleStatus status) => status switch
    {
        BattleStatus.Active => Color.Info,
        BattleStatus.Blocked => Color.Error,
        BattleStatus.Closed => Color.Default,
        _ => Color.Default,
    };

    public static Color ColorOf(Verdict verdict) => verdict switch
    {
        Verdict.Accept => Color.Success,
        Verdict.AcceptWithOpportunity => Color.Warning,
        Verdict.Revise => Color.Warning,
        Verdict.Reject => Color.Error,
        _ => Color.Default,
    };

    // RETEX severity is a free string from the producer; map known values, default otherwise.
    public static Color RetexColor(string? severity) => severity?.ToLowerInvariant() switch
    {
        "blocker" => Color.Error,
        "friction" => Color.Warning,
        "annoyance" => Color.Info,
        "idea" => Color.Success,
        _ => Color.Default,
    };

    public static string Label(PhaseStatus status) => status switch
    {
        PhaseStatus.Pending => "En attente",
        PhaseStatus.InProgress => "En cours",
        PhaseStatus.Done => "Fait",
        PhaseStatus.Blocked => "Bloqué",
        _ => "Inconnu",
    };

    public static string Label(BattleStatus status) => status switch
    {
        BattleStatus.Active => "En cours",
        BattleStatus.Blocked => "Bloquée",
        BattleStatus.Closed => "Terminée",
        _ => "Inconnu",
    };

    public static string Label(Verdict verdict) => verdict switch
    {
        Verdict.Accept => "Accepté",
        Verdict.AcceptWithOpportunity => "Accepté (opportunité)",
        Verdict.Revise => "À réviser",
        Verdict.Reject => "Rejeté",
        _ => "Inconnu",
    };
}
