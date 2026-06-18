namespace IA.Legatus.Services;

// Per-circuit UI state for the fleet list (scoped, not bound to a component instance).
// Survives navigation between pages; resets only on a full reload (new circuit).
public sealed class FleetViewState
{
    // false: active/blocked battles + the ones closed today. true: also the older closed
    // ones (the history). Default false — the list stays focused on what's still live.
    public bool ShowHistory { get; set; }

    // RETEX backlog: false shows only open tooling friction (the actionable loop); true also
    // shows tombstoned (resolved) entries. Default false — the backlog stays actionable.
    public bool ShowResolvedRetex { get; set; }
}
