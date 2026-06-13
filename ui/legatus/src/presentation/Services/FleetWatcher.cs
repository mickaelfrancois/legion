using Microsoft.Extensions.Logging;

namespace IA.Legatus.Services;

// Watches the fleet.d/ shard directory and raises Changed after a quiet period. The
// orchestrator's hook rewrites a battle's shard on every phase transition (doc §5),
// sometimes in bursts — debouncing collapses those into a single UI refresh. Read-only:
// the watcher never touches any file.
public sealed class FleetWatcher : IDisposable
{
    private static readonly TimeSpan Quiet = TimeSpan.FromMilliseconds(300);

    private readonly ILogger<FleetWatcher> _logger;
    private readonly FileSystemWatcher? _watcher;
    private readonly Timer _debounce;

    public event Action? Changed;

    public FleetWatcher(ILogger<FleetWatcher> logger)
    {
        _logger = logger;
        _debounce = new Timer(_ => OnDebounced(), null, Timeout.Infinite, Timeout.Infinite);

        var directory = LegionPaths.FleetDirectory();

        // The watcher needs an existing directory. If the index has never been written,
        // we simply run without live refresh — the page still loads an empty fleet.
        if (!Directory.Exists(directory))
        {
            _logger.LogInformation("fleet.d directory absent ({Directory}); live refresh disabled.", directory);
            return;
        }

        _watcher = new FileSystemWatcher(directory, "*.json")
        {
            NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size
                         | NotifyFilters.FileName | NotifyFilters.CreationTime,
            EnableRaisingEvents = true,
        };
        // A shard add/rewrite/rename (new battle, phase transition) and a delete (/fleet prune).
        _watcher.Changed += OnFileEvent;
        _watcher.Created += OnFileEvent;
        _watcher.Renamed += OnFileEvent;
        _watcher.Deleted += OnFileEvent;
    }

    private void OnFileEvent(object sender, FileSystemEventArgs e)
        => _debounce.Change(Quiet, Timeout.InfiniteTimeSpan);

    private void OnDebounced()
    {
        _logger.LogInformation("fleet.d changed; notifying subscribers.");
        Changed?.Invoke();
    }

    public void Dispose()
    {
        _watcher?.Dispose();
        _debounce.Dispose();
    }
}
