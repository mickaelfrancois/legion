using Markdig;
using Microsoft.AspNetCore.Components;

namespace IA.Legatus.Services;

// Renders GitHub-flavored Markdown (tables, task lists, autolinks, …) to HTML.
// Content comes from the user's own local repos (trusted), so raw HTML pass-through is
// acceptable; the app is read-only and bound to localhost.
public sealed class MarkdownRenderer
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder()
        .UseAdvancedExtensions()
        .Build();

    public MarkupString ToHtml(string markdown)
        => (MarkupString)Markdown.ToHtml(markdown ?? "", Pipeline);
}
