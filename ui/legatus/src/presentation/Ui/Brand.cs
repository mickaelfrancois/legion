namespace IA.Legatus.Ui;

// Roman legionary helmet (galea) with a crest plume — the Legatus brand mark.
// Inner SVG for a 0 0 24 24 viewBox; consumed by MudIcon (inherits currentColor).
// The same shapes back wwwroot/favicon.svg — keep them in sync if either changes.
public static class Brand
{
    public const string Helmet =
        "<ellipse cx='12' cy='4.5' rx='2.2' ry='3.4'/>" +                          // crest plume
        "<path d='M5 13a7 7 0 0 1 14 0Z'/>" +                                       // dome
        "<path d='M4 13.4h16v2.3H4z'/>" +                                          // brow band
        "<path d='M6 15.7h3.3V19c0 .6-.5 1.1-1.1 1.1H7.1C6.5 20.1 6 19.6 6 19z'/>" + // left cheek guard
        "<path d='M14.7 15.7H18V19c0 .6-.5 1.1-1.1 1.1h-1.1c-.6 0-1.1-.5-1.1-1.1z'/>"; // right cheek guard
}
