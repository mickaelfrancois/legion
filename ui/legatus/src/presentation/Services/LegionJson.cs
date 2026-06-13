using System.Text.Json;
using IA.Legatus.Models;

namespace IA.Legatus.Services;

// Shared deserialization options for every legion JSON file: snake_case wire names,
// case-insensitive, and tolerant enum parsing (unknown -> Unknown).
public static class LegionJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters =
        {
            new TolerantEnumConverter<Phase>(),
            new TolerantEnumConverter<PhaseStatus>(),
            new TolerantEnumConverter<BattleStatus>(),
            new TolerantEnumConverter<Verdict>(),
        },
    };
}
