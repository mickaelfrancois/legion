using System.Globalization;

namespace IA.Legatus.Ui;

public static class Format
{
    // Compact token count: 184320 -> "184 k", 1850000 -> "1.8 M", null/0 -> "—".
    public static string Tokens(long? value)
    {
        if (value is not { } v || v == 0)
            return "—";
        if (v >= 1_000_000)
            return (v / 1_000_000d).ToString("0.#", CultureInfo.InvariantCulture) + " M";
        if (v >= 1_000)
            return (v / 1_000d).ToString("0.#", CultureInfo.InvariantCulture) + " k";
        return v.ToString(CultureInfo.InvariantCulture);
    }

    // Full grouped count: 184320 -> "184 320".
    public static string TokensFull(long value)
        => value.ToString("#,0", CultureInfo.InvariantCulture).Replace(',', ' ');
}
