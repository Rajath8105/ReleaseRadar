using System.Net.Http.Headers;

var webRoot = Path.Combine(AppContext.BaseDirectory, "wwwroot");

var builder = WebApplication.CreateBuilder(new WebApplicationOptions
{
    Args = args,
    WebRootPath = webRoot
});

builder.Services.AddSingleton<KubectlClient>();

builder.Services.AddHttpClient<GitHubClient>((services, client) =>
{
    var config = services.GetRequiredService<IConfiguration>();

    client.BaseAddress = new Uri(
        (config["GitHub:ApiBaseUrl"] ?? "https://api.github.com/")
        .TrimEnd('/') + "/");

    client.Timeout = TimeSpan.FromSeconds(
        config.GetValue("GitHub:TimeoutSeconds", 45));

    client.DefaultRequestHeaders.UserAgent.ParseAdd("ReleaseRadar/2.0");
    client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");

    var token = config["GitHub:Token"];

    if (!string.IsNullOrWhiteSpace(token))
    {
        client.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", token);
    }
});

builder.Services.AddSingleton<Scanner>();
builder.Services.AddHostedService<ScanWorker>();

var app = builder.Build();

app.UseDefaultFiles();
app.UseStaticFiles();

app.MapGet("/api/health", () => Results.Ok(new { status = "healthy" }));

app.MapGet("/api/state", (Scanner scanner, IConfiguration config) =>
{
    var data = scanner.Current;

    var stale = data.ObservedAtUtc is null ||
        DateTimeOffset.UtcNow - data.ObservedAtUtc.Value >
        TimeSpan.FromSeconds(config.GetValue("Scan:StaleAfterSeconds", 1200));

    return Results.Ok(new
    {
        data,
        stale,
        isScanning = scanner.IsScanning,
        configuredMode = config["Mode"] ?? "Demo",
        intervalSeconds = config.GetValue("Scan:IntervalSeconds", 600)
    });
});

app.MapPost("/api/scan", (Scanner scanner) =>
{
    if (!scanner.RequestScan())
    {
        return Results.Json(
            new { message = "A refresh was recently requested. Try again shortly." },
            statusCode: 429);
    }

    return Results.Accepted(value: new { message = "Refresh requested." });
});

app.Run();
