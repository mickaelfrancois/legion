using IA.Legatus.Components;
using IA.Legatus.Services;
using MudBlazor.Services;

var builder = WebApplication.CreateBuilder(args);

// Add services to the container.
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();
builder.Services.AddMudServices();
builder.Services.AddSingleton<FleetReader>();
builder.Services.AddSingleton<BattleReader>();
builder.Services.AddSingleton<UsageReader>();
builder.Services.AddSingleton<RetexJournalReader>();
builder.Services.AddSingleton<MarkdownRenderer>();
builder.Services.AddSingleton<FleetWatcher>();
builder.Services.AddScoped<FleetViewState>();

var app = builder.Build();

// Configure the HTTP request pipeline.
if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/Error", createScopeForErrors: true);
    // The default HSTS value is 30 days. You may want to change this for production scenarios, see https://aka.ms/aspnetcore-hsts.
    app.UseHsts();
}
app.UseStatusCodePagesWithReExecute("/not-found", createScopeForStatusCodePages: true);

app.UseAntiforgery();

app.MapStaticAssets();
app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
