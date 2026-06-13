using System.Text.Json;
using System.Text.Json.Serialization;

namespace IA.Legatus.Models;

// Maps snake_case wire values (think, in_progress, accept_with_opportunity) to enum
// members, case- and underscore-insensitively. Anything unknown or non-string yields
// default(T) — i.e. the Unknown member — instead of throwing.
public sealed class TolerantEnumConverter<T> : JsonConverter<T> where T : struct, Enum
{
    private static readonly Dictionary<string, T> Map = Build();

    private static Dictionary<string, T> Build()
    {
        var map = new Dictionary<string, T>(StringComparer.Ordinal);
        foreach (var value in Enum.GetValues<T>())
            map[Normalize(Enum.GetName(value)!)] = value;
        return map;
    }

    private static string Normalize(string s) => s.Replace("_", "").ToLowerInvariant();

    public override T Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        if (reader.TokenType != JsonTokenType.String)
        {
            reader.Skip();
            return default;
        }
        var raw = reader.GetString();
        return raw is not null && Map.TryGetValue(Normalize(raw), out var value) ? value : default;
    }

    public override void Write(Utf8JsonWriter writer, T value, JsonSerializerOptions options)
        => writer.WriteStringValue(value.ToString());
}
