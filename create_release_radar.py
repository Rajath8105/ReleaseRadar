from pathlib import Path
import zipfile

ROOT = Path.cwd() / "ReleaseRadar"

FILES = {
    "backend/ReleaseRadar.Api.csproj": r'''
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>

  <!-- Keep frontend source separate, but include it in build/publish output. -->
  <ItemGroup>
    <Content Include="../frontend/**/*">
      <Link>wwwroot/%(RecursiveDir)%(Filename)%(Extension)</Link>
      <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>
      <CopyToPublishDirectory>PreserveNewest</CopyToPublishDirectory>
    </Content>
  </ItemGroup>
</Project>
''',

    "backend/appsettings.json": r'''
{
  "Mode": "Demo",
  "Scan": {
    "IntervalSeconds": 300,
    "StaleAfterSeconds": 900
  },
  "GitHub": {
    "ApiBaseUrl": "https://api.github.com/"
  },
  "Production": {
    "Source": "File",
    "InventoryPath": "production.json",
    "MaxInventoryAgeMinutes": 30
  },
  "Kubernetes": {
    "ApiUrl": "https://kubernetes.default.svc",
    "Namespaces": ["production"],
    "TokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "CaFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    "IgnoredContainers": ["istio-proxy", "linkerd-proxy"]
  },
  "Logging": {
    "LogLevel": {
      "Default": "Information",
      "Microsoft.AspNetCore": "Warning"
    }
  },
  "AllowedHosts": "*"
}
''',

    "backend/Properties/launchSettings.json": r'''
{
  "$schema": "http://json.schemastore.org/launchsettings.json",
  "profiles": {
    "ReleaseRadar": {
      "commandName": "Project",
      "launchBrowser": true,
      "launchUrl": "",
      "applicationUrl": "http://localhost:5080",
      "environmentVariables": {
        "ASPNETCORE_ENVIRONMENT": "Development"
      }
    }
  }
}
''',

    "backend/production.example.json": r'''
{
  "generatedAtUtc": "REPLACE_WITH_CURRENT_UTC_TIMESTAMP",
  "deployments": [
    {
      "repo": "your-organization/payments-api",
      "sha": "REPLACE_WITH_FULL_40_CHARACTER_PRODUCTION_COMMIT_SHA",
      "releaseBranch": "release/1.0",
      "namespace": "production",
      "workload": "payments-api",
      "container": "app",
      "imageDigest": "sha256:REPLACE_WITH_64_HEX_CHARACTER_DIGEST",
      "evidence": "External inventory"
    }
  ]
}
''',

    "backend/Program.cs": r'''
using System.Collections.Concurrent;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;

var builder = WebApplication.CreateBuilder(new WebApplicationOptions
{
    Args = args,
    WebRootPath = Path.Combine(AppContext.BaseDirectory, "wwwroot")
});

builder.Services.AddHttpClient<GitHubClient>((services, client) =>
{
    var config = services.GetRequiredService<IConfiguration>();

    client.BaseAddress = new Uri(
        config["GitHub:ApiBaseUrl"] ?? "https://api.github.com/");

    client.Timeout = TimeSpan.FromSeconds(30);
    client.DefaultRequestHeaders.UserAgent.ParseAdd("ReleaseRadar/1.0");
    client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
    client.DefaultRequestHeaders.Add("X-GitHub-Api-Version", "2022-11-28");

    var token = config["GitHub:Token"];

    if (!string.IsNullOrWhiteSpace(token))
    {
        client.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", token);
    }
});

builder.Services.AddSingleton<ProductionInventory>();
builder.Services.AddSingleton<ScanService>();
builder.Services.AddHostedService<ScanWorker>();

var app = builder.Build();

app.UseDefaultFiles();
app.UseStaticFiles();

app.MapGet("/api/health", () => Results.Ok(new { status = "healthy" }));

app.MapGet("/api/state", (ScanService scanner, IConfiguration config) =>
{
    var snapshot = scanner.Current;
    var staleSeconds = config.GetValue("Scan:StaleAfterSeconds", 900);

    var stale = snapshot.ObservedAtUtc is null ||
        DateTimeOffset.UtcNow - snapshot.ObservedAtUtc.Value >
        TimeSpan.FromSeconds(staleSeconds);

    return Results.Ok(new
    {
        data = snapshot,
        isScanning = scanner.IsScanning,
        stale,
        serverTimeUtc = DateTimeOffset.UtcNow
    });
});

app.MapPost("/api/scan", (ScanService scanner) =>
{
    if (!scanner.RequestScan())
    {
        return Results.Json(
            new { message = "A refresh was recently requested. Try again shortly." },
            statusCode: 429);
    }

    return Results.Accepted(value: new { message = "Refresh queued." });
});

app.Run();

public sealed record Deployment(
    string Repo,
    string Sha,
    string? ReleaseBranch,
    string Namespace,
    string Workload,
    string Container,
    string ImageDigest,
    string Evidence = "");

public sealed record Provenance(
    string Container,
    string Repo,
    string Sha,
    string ImageDigest,
    string? ReleaseBranch);

public sealed record InventoryDocument(
    DateTimeOffset GeneratedAtUtc,
    Deployment[] Deployments);

public sealed record InventoryResult(
    List<Deployment> Deployments,
    List<string> Warnings);

public sealed record CommitSample(
    string Sha,
    string Message,
    string Url);

public sealed record BranchComparison(
    string Kind,
    string Branch,
    string ProductionSha,
    string HeadSha,
    int Ahead,
    int Behind,
    string State,
    string Url,
    List<CommitSample> Commits,
    string? Error = null);

public sealed record RepositoryResult(
    string Repo,
    string Url,
    string DefaultBranch,
    string State,
    bool Mixed,
    List<Deployment> Deployments,
    List<BranchComparison> Comparisons,
    string? Error = null);

public sealed record ScanSnapshot(
    DateTimeOffset? ObservedAtUtc,
    string Mode,
    string Source,
    List<RepositoryResult> Repositories,
    List<string> Warnings,
    string? Error = null);

public static class Helpers
{
    public static readonly JsonSerializerOptions JsonOptions =
        new(JsonSerializerDefaults.Web)
        {
            PropertyNameCaseInsensitive = true
        };

    public static string Text(JsonElement element, string name)
    {
        return element.TryGetProperty(name, out var value) &&
               value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";
    }

    public static bool ValidIdentity(string? repo, string? sha, string? digest)
    {
        return Regex.IsMatch(repo ?? "", @"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") &&
               Regex.IsMatch(sha ?? "", @"^[a-fA-F0-9]{40}$") &&
               Regex.IsMatch(digest ?? "", @"^sha256:[a-fA-F0-9]{64}$");
    }

    public static string Classify(int ahead, int behind) =>
        (ahead, behind) switch
        {
            (0, 0) => "Aligned",
            (> 0, 0) => "Pending",
            (0, > 0) => "Production ahead",
            _ => "Diverged"
        };

    public static string Aggregate(List<BranchComparison> comparisons)
    {
        var states = comparisons.Select(x => x.State).ToHashSet();

        if (states.Count == 0 || states.Contains("Unknown")) return "Unknown";
        if (states.Contains("Diverged")) return "Diverged";
        if (states.Contains("Pending")) return "Pending";
        if (states.Contains("Production ahead")) return "Production ahead";

        return "Aligned";
    }
}

public sealed class GitHubClient(HttpClient client)
{
    private static string E(string value) => Uri.EscapeDataString(value);

    private async Task<JsonDocument> GetAsync(string path, CancellationToken ct)
    {
        using var response = await client.GetAsync(path, ct);

        if (!response.IsSuccessStatusCode)
        {
            var reset = response.Headers.TryGetValues(
                "X-RateLimit-Reset", out var values)
                ? string.Join(",", values)
                : "not supplied";

            throw new InvalidOperationException(
                $"GitHub returned HTTP {(int)response.StatusCode}. " +
                $"Rate-limit reset epoch: {reset}.");
        }

        return await JsonDocument.ParseAsync(
            await response.Content.ReadAsStreamAsync(ct),
            cancellationToken: ct);
    }

    public async Task<(string Branch, string Url)> RepositoryAsync(
        string repo, CancellationToken ct)
    {
        using var document = await GetAsync($"repos/{repo}", ct);
        var root = document.RootElement;

        return (
            Helpers.Text(root, "default_branch"),
            Helpers.Text(root, "html_url"));
    }

    public async Task<string> HeadAsync(
        string repo, string branch, CancellationToken ct)
    {
        using var document = await GetAsync(
            $"repos/{repo}/commits/{E(branch)}", ct);

        return Helpers.Text(document.RootElement, "sha");
    }

    public async Task<BranchComparison> CompareAsync(
        string repo,
        string repoUrl,
        string productionSha,
        string branch,
        string kind,
        string headSha,
        CancellationToken ct)
    {
        var url = $"{repoUrl}/compare/{productionSha}...{headSha}";

        if (string.Equals(
            productionSha, headSha, StringComparison.OrdinalIgnoreCase))
        {
            return new BranchComparison(
                kind, branch, productionSha, headSha,
                0, 0, "Aligned", url, []);
        }

        using var document = await GetAsync(
            $"repos/{repo}/compare/{E(productionSha)}...{E(headSha)}" +
            "?per_page=5&page=1",
            ct);

        var root = document.RootElement;
        var ahead = root.GetProperty("ahead_by").GetInt32();
        var behind = root.GetProperty("behind_by").GetInt32();

        var commits = new List<CommitSample>();

        if (root.TryGetProperty("commits", out var items))
        {
            foreach (var item in items.EnumerateArray())
            {
                commits.Add(new CommitSample(
                    Helpers.Text(item, "sha"),
                    Helpers.Text(item.GetProperty("commit"), "message")
                        .Split('\n')[0],
                    Helpers.Text(item, "html_url")));
            }
        }

        return new BranchComparison(
            kind, branch, productionSha, headSha,
            ahead, behind, Helpers.Classify(ahead, behind),
            url, commits);
    }
}

public sealed class ProductionInventory(
    IConfiguration config,
    IHostEnvironment environment)
{
    public Task<InventoryResult> ReadAsync(CancellationToken ct)
    {
        var source = config["Production:Source"] ?? "File";

        return source.ToLowerInvariant() switch
        {
            "file" => ReadFileAsync(ct),
            "kubernetes" => ReadKubernetesAsync(ct),
            _ => throw new InvalidOperationException(
                $"Unsupported production inventory source: {source}.")
        };
    }

    private async Task<InventoryResult> ReadFileAsync(CancellationToken ct)
    {
        var configuredPath = config["Production:InventoryPath"] ?? "production.json";

        var path = Path.IsPathRooted(configuredPath)
            ? configuredPath
            : Path.Combine(environment.ContentRootPath, configuredPath);

        await using var stream = File.OpenRead(path);

        var document = await JsonSerializer.DeserializeAsync<InventoryDocument>(
            stream, Helpers.JsonOptions, ct)
            ?? throw new InvalidOperationException("Inventory file is empty.");

        var age = DateTimeOffset.UtcNow - document.GeneratedAtUtc;
        var maxAge = TimeSpan.FromMinutes(
            config.GetValue("Production:MaxInventoryAgeMinutes", 30));

        if (age > maxAge || age < TimeSpan.FromMinutes(-5))
        {
            throw new InvalidOperationException(
                "Inventory timestamp is stale or too far in the future.");
        }

        var warnings = new List<string>
        {
            "File inventory is externally supplied. Runtime image digests " +
            "are not independently verified in File mode."
        };

        var deployments = new List<Deployment>();

        foreach (var item in document.Deployments ?? [])
        {
            if (!Helpers.ValidIdentity(item.Repo, item.Sha, item.ImageDigest))
            {
                warnings.Add(
                    $"Invalid identity for inventory entry: {item.Repo ?? "(unknown)"}.");
                continue;
            }

            deployments.Add(item with
            {
                Sha = item.Sha.ToLowerInvariant(),
                ImageDigest = item.ImageDigest.ToLowerInvariant(),
                Evidence = "External inventory"
            });
        }

        if (deployments.Count == 0)
            warnings.Add("No valid production deployments were discovered.");

        return new InventoryResult(deployments, warnings);
    }

    private async Task<InventoryResult> ReadKubernetesAsync(CancellationToken ct)
    {
        var apiUrl = config["Kubernetes:ApiUrl"]
                     ?? "https://kubernetes.default.svc";

        var tokenFile = config["Kubernetes:TokenFile"]
                        ?? "/var/run/secrets/kubernetes.io/serviceaccount/token";

        var caFile = config["Kubernetes:CaFile"]
                     ?? "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

        var namespaces = config.GetSection("Kubernetes:Namespaces")
            .Get<string[]>() ?? ["production"];

        var ignored = (
            config.GetSection("Kubernetes:IgnoredContainers")
                .Get<string[]>() ?? [])
            .ToHashSet(StringComparer.Ordinal);

        using var handler = new HttpClientHandler();

        using var ca = File.Exists(caFile)
            ? X509Certificate2.CreateFromPemFile(caFile)
            : null;

        if (ca is not null)
        {
            handler.ServerCertificateCustomValidationCallback =
                (_, certificate, _, errors) =>
                {
                    if (certificate is null) return false;

                    if ((errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0)
                        return false;

                    if ((errors & SslPolicyErrors.RemoteCertificateNotAvailable) != 0)
                        return false;

                    using var chain = new X509Chain();

                    chain.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
                    chain.ChainPolicy.CustomTrustStore.Add(ca);
                    chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;

                    return chain.Build(certificate);
                };
        }

        using var http = new HttpClient(handler)
        {
            BaseAddress = new Uri(apiUrl.TrimEnd('/') + "/"),
            Timeout = TimeSpan.FromSeconds(30)
        };

        // Re-read on each scan to support Kubernetes token rotation.
        var token = (await File.ReadAllTextAsync(tokenFile, ct)).Trim();

        http.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", token);

        var deployments = new List<Deployment>();
        var warnings = new List<string>();

        foreach (var ns in namespaces)
        {
            string continuation = "";

            do
            {
                var path =
                    $"api/v1/namespaces/{Uri.EscapeDataString(ns)}/pods?limit=500";

                if (!string.IsNullOrEmpty(continuation))
                    path += "&continue=" + Uri.EscapeDataString(continuation);

                using var response = await http.GetAsync(path, ct);
                response.EnsureSuccessStatusCode();

                using var document = await JsonDocument.ParseAsync(
                    await response.Content.ReadAsStreamAsync(ct),
                    cancellationToken: ct);

                foreach (var pod in document.RootElement
                             .GetProperty("items").EnumerateArray())
                {
                    ReadPod(pod, ns, ignored, deployments, warnings);
                }

                continuation = Helpers.Text(
                    document.RootElement.GetProperty("metadata"), "continue");

            } while (!string.IsNullOrEmpty(continuation));
        }

        if (deployments.Count == 0)
        {
            warnings.Add(
                "No verified ready application containers were discovered. " +
                "An empty inventory does not prove alignment.");
        }

        return new InventoryResult(deployments, warnings);
    }

    private static void ReadPod(
        JsonElement pod,
        string ns,
        HashSet<string> ignored,
        List<Deployment> deployments,
        List<string> warnings)
    {
        var metadata = pod.GetProperty("metadata");
        var podName = Helpers.Text(metadata, "name");

        if (!pod.TryGetProperty("status", out var status) ||
            !status.TryGetProperty("conditions", out var conditions))
            return;

        var ready = conditions.EnumerateArray().Any(x =>
            Helpers.Text(x, "type") == "Ready" &&
            Helpers.Text(x, "status") == "True");

        if (!ready ||
            !status.TryGetProperty("containerStatuses", out var containers))
            return;

        Provenance[] provenance = [];

        if (metadata.TryGetProperty("annotations", out var annotations))
        {
            var raw = Helpers.Text(annotations, "release-alignment/provenance");

            if (!string.IsNullOrWhiteSpace(raw))
            {
                try
                {
                    provenance = JsonSerializer.Deserialize<Provenance[]>(
                        raw, Helpers.JsonOptions) ?? [];
                }
                catch (JsonException)
                {
                    warnings.Add(
                        $"{ns}/{podName}: malformed provenance annotation.");
                }
            }
        }

        foreach (var container in containers.EnumerateArray())
        {
            var name = Helpers.Text(container, "name");

            if (ignored.Contains(name)) continue;

            if (!container.TryGetProperty("ready", out var containerReady) ||
                containerReady.ValueKind != JsonValueKind.True)
                continue;

            var matches = provenance.Where(x => x.Container == name).ToArray();

            if (matches.Length != 1)
            {
                warnings.Add(
                    $"{ns}/{podName}/{name}: missing or duplicate provenance.");
                continue;
            }

            var item = matches[0];

            if (!Helpers.ValidIdentity(item.Repo, item.Sha, item.ImageDigest))
            {
                warnings.Add(
                    $"{ns}/{podName}/{name}: invalid repo, SHA, or digest.");
                continue;
            }

            var actualDigest = Regex.Match(
                Helpers.Text(container, "imageID"),
                @"sha256:[a-fA-F0-9]{64}").Value;

            if (!string.Equals(
                actualDigest, item.ImageDigest, StringComparison.OrdinalIgnoreCase))
            {
                warnings.Add(
                    $"{ns}/{podName}/{name}: runtime digest does not match provenance.");
                continue;
            }

            deployments.Add(new Deployment(
                item.Repo,
                item.Sha.ToLowerInvariant(),
                item.ReleaseBranch,
                ns,
                podName,
                name,
                actualDigest.ToLowerInvariant(),
                "Runtime digest matched"));
        }
    }
}

public sealed class ScanService(
    IConfiguration config,
    ProductionInventory inventory,
    GitHubClient github,
    ILogger<ScanService> logger)
{
    private ScanSnapshot current = new(
        null, "Starting", "", [], [], "Initial scan has not completed.");

    private int scanning;
    private readonly object requestLock = new();
    private DateTimeOffset lastRequest = DateTimeOffset.MinValue;

    private readonly Channel<bool> requests =
        Channel.CreateBounded<bool>(new BoundedChannelOptions(1)
        {
            FullMode = BoundedChannelFullMode.DropWrite,
            SingleReader = true
        });

    public ScanSnapshot Current => Volatile.Read(ref current);
    public bool IsScanning => Volatile.Read(ref scanning) == 1;

    public bool RequestScan()
    {
        lock (requestLock)
        {
            if (DateTimeOffset.UtcNow - lastRequest < TimeSpan.FromSeconds(15))
                return false;

            lastRequest = DateTimeOffset.UtcNow;
            return requests.Writer.TryWrite(true);
        }
    }

    public async Task WaitAsync(CancellationToken stoppingToken)
    {
        using var timeout =
            CancellationTokenSource.CreateLinkedTokenSource(stoppingToken);

        timeout.CancelAfter(TimeSpan.FromSeconds(
            Math.Max(15, config.GetValue("Scan:IntervalSeconds", 300))));

        try
        {
            await requests.Reader.ReadAsync(timeout.Token);
            while (requests.Reader.TryRead(out _)) { }
        }
        catch (OperationCanceledException)
            when (!stoppingToken.IsCancellationRequested)
        {
            // The periodic scan interval elapsed.
        }
    }

    public async Task ScanAsync(CancellationToken ct)
    {
        if (Interlocked.Exchange(ref scanning, 1) == 1) return;

        var mode = config["Mode"] ?? "Demo";

        try
        {
            if (mode.Equals("Demo", StringComparison.OrdinalIgnoreCase))
            {
                Volatile.Write(ref current, DemoData.Create());
                return;
            }

            if (!mode.Equals("Live", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Mode must be Demo or Live.");

            var discovered = await inventory.ReadAsync(ct);
            var observedAt = DateTimeOffset.UtcNow;
            var results = new ConcurrentBag<RepositoryResult>();

            var groups = discovered.Deployments
                .GroupBy(x => x.Repo, StringComparer.OrdinalIgnoreCase)
                .ToArray();

            await Parallel.ForEachAsync(
                groups,
                new ParallelOptions
                {
                    MaxDegreeOfParallelism = 3,
                    CancellationToken = ct
                },
                async (group, token) =>
                {
                    results.Add(await ScanRepositoryAsync(
                        group.Key, group.ToList(), token));
                });

            Volatile.Write(ref current, new ScanSnapshot(
                observedAt,
                "Live",
                config["Production:Source"] ?? "File",
                results.OrderBy(x => x.Repo).ToList(),
                discovered.Warnings));
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            logger.LogError(exception, "Inventory scan failed.");

            Volatile.Write(ref current, Current with
            {
                Mode = mode,
                Source = config["Production:Source"] ?? "File",
                Error = "Latest inventory scan failed. Any previous results may " +
                        "be outdated. Check the backend terminal for details."
            });
        }
        finally
        {
            Interlocked.Exchange(ref scanning, 0);
        }
    }

    private async Task<RepositoryResult> ScanRepositoryAsync(
        string repo,
        List<Deployment> deployments,
        CancellationToken ct)
    {
        var mixed = deployments.Select(x => x.Sha)
            .Distinct(StringComparer.OrdinalIgnoreCase).Count() > 1;

        try
        {
            var metadata = await github.RepositoryAsync(repo, ct);

            var targets = deployments.SelectMany(deployment =>
            {
                var list = new List<(string Sha, string Branch, string Kind)>
                {
                    (deployment.Sha, metadata.Branch, "Trunk")
                };

                if (!string.IsNullOrWhiteSpace(deployment.ReleaseBranch))
                {
                    list.Add((
                        deployment.Sha, deployment.ReleaseBranch!, "Release"));
                }

                return list;
            }).Distinct().ToArray();

            var heads = new Dictionary<string, string>();
            var errors = new Dictionary<string, string>();

            foreach (var branch in targets.Select(x => x.Branch).Distinct())
            {
                try
                {
                    heads[branch] = await github.HeadAsync(repo, branch, ct);
                }
                catch (Exception exception)
                    when (exception is not OperationCanceledException ||
                          !ct.IsCancellationRequested)
                {
                    logger.LogWarning(
                        exception, "Could not resolve {Repo}/{Branch}.", repo, branch);

                    errors[branch] =
                        "Branch could not be resolved. Check GitHub access, " +
                        "branch name, and backend logs.";
                }
            }

            var comparisons = new List<BranchComparison>();

            foreach (var target in targets)
            {
                if (errors.TryGetValue(target.Branch, out var error))
                {
                    comparisons.Add(new BranchComparison(
                        target.Kind, target.Branch, target.Sha, "",
                        0, 0, "Unknown", "", [], error));
                    continue;
                }

                try
                {
                    comparisons.Add(await github.CompareAsync(
                        repo, metadata.Url, target.Sha, target.Branch,
                        target.Kind, heads[target.Branch], ct));
                }
                catch (Exception exception)
                    when (exception is not OperationCanceledException ||
                          !ct.IsCancellationRequested)
                {
                    logger.LogWarning(
                        exception, "Comparison failed for {Repo}.", repo);

                    comparisons.Add(new BranchComparison(
                        target.Kind, target.Branch, target.Sha, heads[target.Branch],
                        0, 0, "Unknown", "", [],
                        "Comparison failed. Check SHA availability, GitHub " +
                        "permissions, rate limits, and backend logs."));
                }
            }

            return new RepositoryResult(
                repo, metadata.Url, metadata.Branch,
                Helpers.Aggregate(comparisons), mixed,
                deployments, comparisons);
        }
        catch (Exception exception)
            when (exception is not OperationCanceledException ||
                  !ct.IsCancellationRequested)
        {
            logger.LogWarning(exception, "Repository scan failed for {Repo}.", repo);

            return new RepositoryResult(
                repo, "", "", "Unknown", mixed, deployments, [],
                "Repository unavailable. Check GitHub permissions, rate limits, " +
                "and backend logs.");
        }
    }
}

public sealed class ScanWorker(ScanService scanner) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Let the web host start before performing the initial scan.
        await Task.Yield();

        while (!stoppingToken.IsCancellationRequested)
        {
            await scanner.ScanAsync(stoppingToken);
            await scanner.WaitAsync(stoppingToken);
        }
    }
}

public static class DemoData
{
    public static ScanSnapshot Create()
    {
        return new ScanSnapshot(
            DateTimeOffset.UtcNow,
            "Demo",
            "Simulated production",
            [
                Repo("payments-api", 8, 0, 3, 0),
                Repo("orders-api", 0, 0, 0, 0),
                Repo("identity-service", 4, 2, 1, 2),
                Repo("notifications-worker", 0, 2, 0, 1),
                Repo("inventory-api", 5, 0, 2, 0, mixed: true),
                Repo("shipping-service", 0, 0, 0, 0, unknown: true)
            ],
            [
                "Demo mode uses simulated repositories and commits. " +
                "Configure Live mode to inspect your own repositories."
            ]);
    }

    private static RepositoryResult Repo(
        string name,
        int trunkAhead,
        int trunkBehind,
        int releaseAhead,
        int releaseBehind,
        bool mixed = false,
        bool unknown = false)
    {
        var repo = $"acme/{name}";
        var sha = new string('a', 40);
        var head = new string('b', 40);

        var deployments = new List<Deployment>
        {
            new(
                repo, sha, "release/1.8",
                "production", $"{name}-7f8c9-x1", "app",
                "sha256:" + new string('1', 64), "Simulated runtime")
        };

        if (mixed)
        {
            deployments.Add(new Deployment(
                repo, new string('c', 40), "release/1.8",
                "production", $"{name}-7f8c9-x2", "app",
                "sha256:" + new string('2', 64), "Simulated runtime"));
        }

        var comparisons = new List<BranchComparison>();

        for (var i = 0; i < deployments.Count; i++)
        {
            var deployment = deployments[i];

            comparisons.Add(Make(
                "Trunk", "main", Math.Max(0, trunkAhead - i),
                trunkBehind, deployment.Sha));

            comparisons.Add(Make(
                "Release", "release/1.8", Math.Max(0, releaseAhead - i),
                releaseBehind, deployment.Sha));
        }

        return new RepositoryResult(
            repo, "", "main", Helpers.Aggregate(comparisons),
            mixed, deployments, comparisons);

        BranchComparison Make(
            string kind, string branch, int ahead, int behind, string productionSha)
        {
            return new BranchComparison(
                kind,
                branch,
                productionSha,
                ahead == 0 && behind == 0 ? productionSha : head,
                ahead,
                behind,
                unknown ? "Unknown" : Helpers.Classify(ahead, behind),
                "",
                ahead > 0
                    ? [
                        new CommitSample(
                            head, "Improve request validation and resilience", ""),
                        new CommitSample(
                            new string('d', 40), "Update service observability", "")
                      ]
                    : [],
                unknown ? "Simulated GitHub permission failure." : null);
        }
    }
}
''',

    "frontend/index.html": r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Release Radar | Production alignment</title>
  <link rel="stylesheet" href="/styles.css">
  <script src="/app.js" defer></script>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <a class="brand" href="/">
        <span class="logo">◎</span>
        <span>Release<span class="brand-light">Radar</span></span>
      </a>

      <div class="nav-label">WORKSPACE</div>
      <div class="nav-item active"><span>▦</span> Release overview</div>
      <a class="nav-item" href="/api/state" target="_blank" rel="noopener">
        <span>⌘</span> Snapshot API
      </a>
      <a class="nav-item" href="/api/health" target="_blank" rel="noopener">
        <span>♡</span> API health
      </a>

      <div class="sidebar-note">
        <div class="note-icon">◉</div>
        <strong>Production-first visibility</strong>
        <p>Know what is running before deciding what needs releasing.</p>
      </div>
      <div class="sidebar-footer">
        <span class="dot"></span> .NET 8 · GitHub · Kubernetes
      </div>
    </aside>

    <main>
      <header class="topbar">
        <span class="breadcrumb">Engineering / <strong>Release alignment</strong></span>
        <div class="topbar-right">
          <span class="environment">● Production scope</span>
          <span id="mode" class="mode">STARTING</span>
        </div>
      </header>

      <div class="content">
        <section class="hero">
          <div>
            <div class="eyebrow">YOUR RELEASE CONTROL CENTER</div>
            <h1>What’s running.<br><span>What’s waiting.</span></h1>
            <p>Close the gap between your repositories and production.</p>
          </div>
          <div class="hero-actions">
            <button id="refresh" class="primary">↻ Refresh inventory</button>
            <span id="scan-status">Preparing first scan…</span>
          </div>
        </section>

        <div id="notices" aria-live="polite"></div>
        <div id="action-message" class="action-message" role="status"></div>

        <section class="metrics" id="metrics" aria-label="Alignment metrics"></section>

        <section class="repositories">
          <div class="panel-heading">
            <div>
              <h2>Production repositories <span id="repo-count">0</span></h2>
              <p>Runtime versions compared against trunk and release branch heads</p>
            </div>
            <span id="source" class="source">—</span>
          </div>

          <div class="toolbar">
            <div class="search-wrap">
              <span>⌕</span>
              <input id="search" type="search"
                placeholder="Search repository, namespace, or branch..."
                aria-label="Search repositories">
            </div>

            <select id="filter" aria-label="Filter alignment state">
              <option value="">All states</option>
              <option>Aligned</option>
              <option>Pending</option>
              <option>Diverged</option>
              <option>Production ahead</option>
              <option>Unknown</option>
              <option>Mixed rollout</option>
            </select>
          </div>

          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Repository</th>
                  <th>Production SHA</th>
                  <th>Trunk drift</th>
                  <th>Release drift</th>
                  <th>Alignment</th>
                  <th><span class="sr-only">Details</span></th>
                </tr>
              </thead>
              <tbody id="rows">
                <tr><td colspan="6" class="empty">Loading your dashboard…</td></tr>
              </tbody>
            </table>
          </div>

          <div class="table-footer">
            <span id="results-count">Waiting for inventory</span>
            <span>Trunk and release counts are not additive.</span>
          </div>
        </section>

        <section class="explainer">
          <div class="explainer-icon">⌁</div>
          <div>
            <strong>Ahead of production ≠ deployed</strong>
            <p>
              “Not live” counts branch commits absent from a production SHA.
              “Production-only” identifies commits missing from the branch.
              Mixed rollouts compare every observed production version.
            </p>
          </div>
        </section>

        <footer>
          <span id="updated">Waiting for first observation…</span>
          <span>Git ancestry visibility · Not a release approval gate</span>
        </footer>
      </div>
    </main>
  </div>
</body>
</html>
''',

    "frontend/styles.css": r'''
:root {
  --bg: #090e1b;
  --panel: #111a2a;
  --line: #233048;
  --text: #eaf0fc;
  --muted: #8a9ab5;
  --blue: #8cacff;
  --green: #65d8b0;
  --amber: #f4c276;
  --red: #fb8799;
  --purple: #b89afd;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
}
button, input, select { font: inherit; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
button { cursor: pointer; }
button:disabled { opacity: .5; cursor: wait; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 3px;
}
.layout { display: flex; min-height: 100vh; }
.sidebar {
  width: 242px;
  flex-shrink: 0;
  background: #0d1422;
  border-right: 1px solid var(--line);
  padding: 29px 18px;
  display: flex;
  flex-direction: column;
  position: fixed;
  height: 100vh;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 21px;
  font-weight: 750;
  color: var(--text);
  letter-spacing: -.6px;
  margin: 0 6px 47px;
}
.brand:hover { text-decoration: none; }
.brand-light { color: #a4b5dc; }
.logo {
  background: linear-gradient(135deg, #719fff, #8853e5);
  color: white;
  border-radius: 11px;
  width: 35px;
  height: 35px;
  display: grid;
  place-items: center;
  box-shadow: 0 0 25px #7894ff25;
}
.nav-label { color: #637591; font-size: 10px; letter-spacing: 1.5px; margin: 0 13px 16px; }
.nav-item {
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 13px;
  color: var(--muted);
  border-radius: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}
.nav-item.active { background: #759dff14; color: #adc4ff; border: 1px solid #759dff20; }
.nav-item span { width: 16px; font-size: 17px; }
.sidebar-note {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: linear-gradient(140deg, #19273c, #111b2b);
  padding: 16px;
  margin-top: auto;
}
.note-icon { color: var(--blue); font-size: 23px; margin-bottom: 10px; }
.sidebar-note strong { font-size: 12px; }
.sidebar-note p { color: var(--muted); font-size: 12px; line-height: 1.7; }
.sidebar-footer { color: #74849d; font-size: 10px; margin: 21px 4px 0; }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 6px; }
main { width: calc(100% - 242px); margin-left: 242px; }
.topbar {
  min-height: 76px;
  border-bottom: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 34px;
  gap: 15px;
}
.breadcrumb { font-size: 12px; color: var(--muted); }
.breadcrumb strong { color: #c2cee4; font-weight: 500; }
.topbar-right { display: flex; gap: 13px; align-items: center; }
.environment { font-size: 11px; color: var(--green); }
.mode, .source {
  font-size: 10px;
  letter-spacing: .6px;
  border: 1px solid #6984bf45;
  padding: 5px 9px;
  border-radius: 6px;
  color: #abc1f2;
  background: #789bff0c;
}
.content {
  padding: 35px;
  max-width: 1580px;
  margin: auto;
  background: radial-gradient(ellipse at 80% 0, #26345d24 0, transparent 40%);
}
.hero {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 22px;
  margin-bottom: 27px;
}
.eyebrow { font-size: 10px; letter-spacing: 2px; color: #9aafe0; margin-bottom: 13px; }
h1 { margin: 0; font-size: clamp(30px, 3.2vw, 45px); line-height: 1.16; letter-spacing: -1.8px; }
h1 span { color: #9bacd3; }
.hero p { color: var(--muted); margin: 14px 0 0; font-size: 13px; line-height: 1.6; }
.hero-actions { display: flex; flex-direction: column; align-items: center; gap: 11px; }
.hero-actions > span { font-size: 10px; color: var(--muted); }
.primary {
  border: 1px solid #99b7ff;
  background: linear-gradient(120deg, #8caaff, #7898ef);
  color: #0b1936;
  border-radius: 8px;
  padding: 12px 18px;
  font-weight: 750;
  box-shadow: 0 4px 20px #698cff20;
  white-space: nowrap;
}
.metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 13px; margin: 22px 0; }
.metric {
  background: linear-gradient(135deg, #172238, #101929);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 18px;
  position: relative;
  overflow: hidden;
}
.metric::after {
  content: "";
  width: 55px;
  height: 55px;
  border-radius: 50%;
  background: var(--accent);
  position: absolute;
  right: -25px;
  top: -25px;
  opacity: .1;
  filter: blur(10px);
}
.metric-top { display: flex; justify-content: space-between; gap: 6px; color: var(--muted); font-size: 11px; }
.metric-icon { color: var(--accent); }
.metric-value { font-size: 33px; font-weight: 750; margin: 10px 0 8px; color: var(--accent); }
.metric-hint { font-size: 10px; color: #7688a4; line-height: 1.5; }
.repositories { background: var(--panel); border: 1px solid var(--line); border-radius: 13px; overflow: hidden; }
.panel-heading { display: flex; justify-content: space-between; gap: 15px; align-items: center; padding: 21px; border-bottom: 1px solid var(--line); }
h2 { font-size: 15px; margin: 0; font-weight: 650; }
h2 span { display: inline-block; margin-left: 6px; background: #263651; color: #bacbec; font-size: 10px; border-radius: 5px; padding: 3px 6px; vertical-align: middle; }
.panel-heading p { color: var(--muted); font-size: 11px; margin: 7px 0 0; }
.toolbar { display: flex; justify-content: space-between; gap: 15px; padding: 15px 21px; }
.search-wrap { position: relative; width: min(440px, 100%); }
.search-wrap > span { position: absolute; top: 6px; left: 12px; color: var(--muted); font-size: 22px; }
input, select { background: #0b1423; border: 1px solid var(--line); color: var(--text); border-radius: 7px; font-size: 12px; padding: 10px 12px; }
input { width: 100%; padding-left: 35px; }
input::placeholder { color: #6f809c; }
select { min-width: 160px; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { background: #0d1727; color: #8192ad; text-transform: uppercase; font-size: 9px; letter-spacing: 1px; text-align: left; padding: 13px 19px; white-space: nowrap; }
td { padding: 19px; border-top: 1px solid #233048bb; font-size: 12px; vertical-align: top; }
.repo-row:hover { background: #19253b80; }
.repo-title { display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.repo-icon { color: #a3b6da; border: 1px solid #33445f; background: #1a283f; border-radius: 7px; padding: 6px 7px; }
.repo-name { font-size: 12px; font-weight: 650; color: #dae5fa; }
.repo-name a { color: inherit; }
.sub { font-size: 10px; color: var(--muted); margin-top: 6px; }
code { font-family: "Cascadia Code", Consolas, monospace; font-size: 11px; color: #bacdff; }
.sha { background: #1b2941; border: 1px solid #2a3c59; padding: 4px 6px; display: inline-block; border-radius: 5px; margin-bottom: 4px; }
.badge { display: inline-flex; align-items: center; padding: 5px 8px; border-radius: 5px; font-size: 10px; font-weight: 600; white-space: nowrap; gap: 5px; }
.aligned { color: var(--green); background: #65d8b012; border: 1px solid #65d8b024; }
.pending { color: var(--amber); background: #f4c27612; border: 1px solid #f4c27624; }
.diverged { color: var(--red); background: #fb879912; border: 1px solid #fb879924; }
.production-ahead { color: var(--purple); background: #b89afd12; border: 1px solid #b89afd24; }
.unknown { color: #b1bed3; background: #b1bed312; border: 1px solid #b1bed324; }
.mixed { color: var(--blue); background: #8cacff12; border: 1px solid #8cacff24; margin-top: 6px; }
.drift-value { font-size: 17px; font-weight: 700; color: #dce7fa; }
.drift-label { font-size: 10px; color: var(--muted); }
.inspect { background: transparent; color: #9fb9f4; border: 1px solid #30425f; border-radius: 6px; padding: 6px 9px; font-size: 10px; white-space: nowrap; }
.detail-cell { background: #0c1524; padding: 18px; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.comparison { min-width: 0; padding: 15px; background: #152137; border: 1px solid var(--line); border-radius: 9px; }
.comparison-header { display: flex; justify-content: space-between; align-items: center; gap: 9px; }
.comparison p { font-size: 11px; line-height: 1.6; margin: 10px 0 0; overflow-wrap: anywhere; }
.comparison ul { font-size: 11px; line-height: 1.8; padding-left: 18px; color: var(--muted); }
.evidence { border-left: 2px solid #81a5ff; background: #14213a; padding: 13px; margin-top: 13px; font-size: 11px; overflow-wrap: anywhere; }
.evidence p { color: var(--muted); line-height: 1.7; margin-bottom: 0; }
.notice { padding: 12px 15px; background: #f4c27609; border: 1px solid #f4c2762b; border-radius: 9px; color: #dabb8c; font-size: 11px; line-height: 1.7; margin-bottom: 12px; }
.notice summary { cursor: pointer; }
.notice ul { padding-left: 20px; }
.error { background: #fb879909; border-color: #fb87993b; color: #f9a8b5; }
.action-message { color: var(--blue); font-size: 12px; min-height: 0; }
.action-message:not(:empty) { padding: 8px 0; }
.table-footer { display: flex; justify-content: space-between; gap: 14px; border-top: 1px solid var(--line); padding: 14px 20px; color: var(--muted); font-size: 10px; }
.explainer { display: flex; align-items: flex-start; gap: 15px; padding: 20px; margin-top: 19px; border: 1px solid var(--line); border-radius: 11px; background: #101b2d; }
.explainer-icon { font-size: 28px; color: var(--blue); }
.explainer strong { font-size: 12px; color: #c5d6f7; }
.explainer p { font-size: 11px; color: var(--muted); line-height: 1.8; margin: 6px 0 0; }
footer { display: flex; justify-content: space-between; gap: 15px; color: #71819a; font-size: 10px; padding: 22px 0; flex-wrap: wrap; }
.empty { text-align: center; color: var(--muted); padding: 45px; }
.muted { color: var(--muted); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }

@media (min-width: 1600px) {
  .repo-name { font-size: 14px; }
  th { font-size: 10px; }
}
@media (max-width: 1200px) {
  .sidebar { width: 210px; }
  main { width: calc(100% - 210px); margin-left: 210px; }
  .content { padding: 25px; }
  .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 850px) {
  .sidebar { display: none; }
  main { width: 100%; margin-left: 0; }
  .topbar { padding: 16px 22px; }
}
@media (max-width: 600px) {
  .content { padding: 20px 14px; }
  .hero { align-items: flex-start; flex-direction: column; }
  .hero-actions { align-items: flex-start; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .toolbar, .panel-heading { align-items: stretch; flex-direction: column; }
  .source { align-self: flex-start; }
  .detail-grid { grid-template-columns: 1fr; }
  .table-footer { flex-direction: column; }
  .environment { display: none; }
  .breadcrumb { font-size: 10px; }
}
''',

    "frontend/app.js": r'''
"use strict";

let snapshot = null;
let loading = false;
const expanded = new Set();
const $ = id => document.getElementById(id);

const escapeHtml = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[character]
);

const stateClass = value => value.toLowerCase().replaceAll(" ", "-");
const shortSha = value => escapeHtml((value || "").slice(0, 7));

function badge(state) {
  return `<span class="badge ${stateClass(state)}">● ${escapeHtml(state)}</span>`;
}

function link(url, label) {
  try {
    const parsed = new URL(url);

    if (!["https:", "http:"].includes(parsed.protocol)) {
      return escapeHtml(label);
    }

    return `<a href="${escapeHtml(parsed.href)}"
               target="_blank"
               rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch {
    return escapeHtml(label);
  }
}

function range(values) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? String(min) : `${min}–${max}`;
}

function drift(repo, kind) {
  const comparisons = repo.comparisons.filter(x => x.kind === kind);

  if (!comparisons.length) {
    return `<span class="muted">${
      repo.state === "Unknown" ? "Unavailable" : "Not tracked"
    }</span>`;
  }

  if (comparisons.some(x => x.state === "Unknown")) {
    return badge("Unknown");
  }

  return `
    <div>
      <span class="drift-value">${range(comparisons.map(x => x.ahead))}</span>
      <span class="drift-label">not live</span>
    </div>
    <div class="sub">
      ${range(comparisons.map(x => x.behind))} production-only
    </div>`;
}

function comparisonHtml(item) {
  return `
    <div class="comparison">
      <div class="comparison-header">
        <strong>${escapeHtml(item.kind)}</strong>
        ${badge(item.state)}
      </div>

      <p><span class="muted">Branch:</span> <code>${escapeHtml(item.branch)}</code></p>

      <p>
        <code>${shortSha(item.productionSha)}</code>
        <span class="muted"> production → </span>
        <code>${shortSha(item.headSha) || "unknown"}</code>
        <span class="muted"> branch HEAD</span>
      </p>

      ${
        item.error
          ? `<p style="color:var(--red)">${escapeHtml(item.error)}</p>`
          : `<p>
               <strong>${item.ahead}</strong> commits not in production ·
               <strong>${item.behind}</strong> production-only commits
             </p>`
      }

      ${item.url ? `<p>${link(item.url, "Open GitHub comparison ↗")}</p>` : ""}

      ${
        item.commits.length
          ? `<p class="muted">Commit sample — not a complete changelog</p>
             <ul>${item.commits.map(commit =>
               `<li>${link(commit.url, commit.message)}</li>`
             ).join("")}</ul>`
          : ""
      }
    </div>`;
}

function detailsHtml(repo) {
  return `
    ${repo.error ? `<div class="notice error">${escapeHtml(repo.error)}</div>` : ""}

    <div class="detail-grid">
      ${repo.comparisons.map(comparisonHtml).join("")}
    </div>

    <div class="evidence">
      <strong>Observed production instances</strong>
      ${repo.deployments.map(item => `
        <p>
          <code>${shortSha(item.sha)}</code> ·
          ${escapeHtml(item.namespace)}/${escapeHtml(item.workload)} ·
          ${escapeHtml(item.container)}
          <br>
          ${escapeHtml(item.evidence)} · ${escapeHtml(item.imageDigest)}
        </p>
      `).join("")}
    </div>`;
}

function renderMetrics(repos) {
  const metrics = [
    {
      label: "Production repos",
      value: repos.length,
      hint: "Within configured discovery scope",
      color: "var(--blue)",
      icon: "▦"
    },
    {
      label: "Fully aligned",
      value: repos.filter(x => x.state === "Aligned").length,
      hint: "Across tracked comparisons",
      color: "var(--green)",
      icon: "✓"
    },
    {
      label: "Changes not live",
      value: repos.filter(x =>
        x.comparisons.some(c => c.state !== "Unknown" && c.ahead > 0)
      ).length,
      hint: "Repos with branch-only commits",
      color: "var(--amber)",
      icon: "↗"
    },
    {
      label: "Mixed rollouts",
      value: repos.filter(x => x.mixed).length,
      hint: "Multiple production versions",
      color: "var(--purple)",
      icon: "◐"
    },
    {
      label: "Needs attention",
      value: repos.filter(x =>
        ["Diverged", "Production ahead", "Unknown"].includes(x.state)
      ).length,
      hint: "History conflict or uncertainty",
      color: "var(--red)",
      icon: "!"
    }
  ];

  $("metrics").innerHTML = metrics.map(item => `
    <article class="metric" style="--accent:${item.color}">
      <div class="metric-top">
        <span>${item.label}</span>
        <span class="metric-icon">${item.icon}</span>
      </div>
      <div class="metric-value">${item.value}</div>
      <div class="metric-hint">${item.hint}</div>
    </article>
  `).join("");
}

function renderRows() {
  if (!snapshot) return;

  const repos = snapshot.data.repositories;
  const query = $("search").value.trim().toLowerCase();
  const filter = $("filter").value;

  const visible = repos.filter(repo => {
    const text = [
      repo.repo,
      repo.defaultBranch,
      ...repo.deployments.map(x => x.namespace),
      ...repo.comparisons.map(x => x.branch)
    ].join(" ").toLowerCase();

    const matchesState =
      !filter ||
      (filter === "Mixed rollout" ? repo.mixed : repo.state === filter);

    return text.includes(query) && matchesState;
  });

  $("results-count").textContent =
    `Showing ${visible.length} of ${repos.length} repositories`;

  if (!visible.length) {
    $("rows").innerHTML = `
      <tr><td colspan="6" class="empty">
        No matching repositories. An empty inventory does not prove alignment.
      </td></tr>`;
    return;
  }

  $("rows").innerHTML = visible.map(repo => {
    const shas = [...new Set(repo.deployments.map(x => x.sha))];
    const isExpanded = expanded.has(repo.repo);
    const splitAt = repo.repo.indexOf("/");
    const owner = repo.repo.slice(0, splitAt);
    const name = repo.repo.slice(splitAt + 1);

    return `
      <tr class="repo-row">
        <td>
          <div class="repo-title">
            <span class="repo-icon">⌘</span>
            <div>
              <div class="repo-name">${link(repo.url, name)}</div>
              <div class="sub">
                ${escapeHtml(owner)} · ${escapeHtml(repo.defaultBranch || "unknown")}
              </div>
            </div>
          </div>
        </td>
        <td>
          ${shas.map(sha => `<code class="sha">${shortSha(sha)}</code>`).join("<br>")}
          <div class="sub">${repo.deployments.length} container instance(s)</div>
        </td>
        <td>${drift(repo, "Trunk")}</td>
        <td>${drift(repo, "Release")}</td>
        <td>
          ${badge(repo.state)}
          ${repo.mixed ? '<br><span class="badge mixed">◐ Mixed rollout</span>' : ""}
        </td>
        <td>
          <button class="inspect"
            data-repo="${escapeHtml(repo.repo)}"
            aria-expanded="${isExpanded}">
            ${isExpanded ? "Hide ↑" : "Inspect ↓"}
          </button>
        </td>
      </tr>
      ${
        isExpanded
          ? `<tr><td colspan="6" class="detail-cell">${detailsHtml(repo)}</td></tr>`
          : ""
      }`;
  }).join("");
}

function render() {
  if (!snapshot) return;

  const data = snapshot.data;
  $("mode").textContent = data.mode.toUpperCase();
  $("source").textContent = data.source || "Waiting for inventory";
  $("repo-count").textContent = data.repositories.length;

  renderMetrics(data.repositories);
  renderRows();

  let notices = "";

  if (data.error) {
    notices += `<div class="notice error">${escapeHtml(data.error)}</div>`;
  }

  if (snapshot.stale) {
    notices += `
      <div class="notice error">
        Data is stale or no successful inventory has been observed.
        Do not interpret this dashboard as current release approval.
      </div>`;
  }

  if (data.warnings.length) {
    notices += `
      <details class="notice">
        <summary>
          ${data.warnings.length} inventory / coverage notice(s) — click to inspect
        </summary>
        <ul>
          ${data.warnings.slice(0, 100).map(x =>
            `<li>${escapeHtml(x)}</li>`
          ).join("")}
        </ul>
        ${data.warnings.length > 100 ? "<p>Showing the first 100 notices.</p>" : ""}
      </details>`;
  }

  $("notices").innerHTML = notices;
  $("refresh").disabled = snapshot.isScanning;
  $("refresh").textContent =
    snapshot.isScanning ? "↻ Scanning..." : "↻ Refresh inventory";

  $("scan-status").textContent =
    snapshot.isScanning ? "Checking production and GitHub" : "Automatic background scanning enabled";

  $("updated").textContent = data.observedAtUtc
    ? `Inventory observed: ${new Date(data.observedAtUtc).toLocaleString()}`
    : "Waiting for first production observation…";
}

async function load() {
  if (loading) return;
  loading = true;

  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");

    snapshot = await response.json();
    render();
  } catch {
    $("notices").innerHTML = `
      <div class="notice error">
        Cannot reach the backend. Displayed results may be outdated.
        Check that the .NET application is running.
      </div>`;

    $("scan-status").textContent = "Backend unavailable";
    $("refresh").disabled = false;
  } finally {
    loading = false;
  }
}

$("search").addEventListener("input", renderRows);
$("filter").addEventListener("change", renderRows);

$("rows").addEventListener("click", event => {
  const button = event.target.closest("button[data-repo]");
  if (!button) return;

  const repo = button.dataset.repo;

  if (expanded.has(repo)) expanded.delete(repo);
  else expanded.add(repo);

  renderRows();
});

$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;
  $("action-message").textContent = "";

  try {
    const response = await fetch("/api/scan", { method: "POST" });
    const body = await response.json();

    $("action-message").textContent = body.message || "Refresh requested.";
  } catch {
    $("action-message").textContent = "Could not reach the refresh API.";
  } finally {
    await load();
    setTimeout(() => { $("action-message").textContent = ""; }, 5000);
  }
});

load();
setInterval(load, 10000);
''',

    ".vscode/extensions.json": r'''
{
  "recommendations": [
    "ms-dotnettools.csharp",
    "ms-dotnettools.csdevkit"
  ]
}
''',

    ".vscode/tasks.json": r'''
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "build",
      "type": "process",
      "command": "dotnet",
      "args": [
        "build",
        "${workspaceFolder}/backend/ReleaseRadar.Api.csproj"
      ],
      "problemMatcher": "$msCompile",
      "group": {
        "kind": "build",
        "isDefault": true
      }
    },
    {
      "label": "run",
      "type": "process",
      "command": "dotnet",
      "args": ["run"],
      "options": {
        "cwd": "${workspaceFolder}/backend"
      },
      "problemMatcher": []
    }
  ]
}
''',

    ".vscode/launch.json": r'''
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Release Radar: Debug",
      "type": "coreclr",
      "request": "launch",
      "preLaunchTask": "build",
      "program": "${workspaceFolder}/backend/bin/Debug/net8.0/ReleaseRadar.Api.dll",
      "args": [],
      "cwd": "${workspaceFolder}/backend",
      "stopAtEntry": false,
      "console": "internalConsole",
      "env": {
        "ASPNETCORE_ENVIRONMENT": "Development",
        "ASPNETCORE_URLS": "http://localhost:5080"
      },
      "serverReadyAction": {
        "action": "openExternally",
        "pattern": "\\bNow listening on:\\s+(https?://\\S+)"
      }
    }
  ]
}
''',

    ".gitignore": r'''
**/bin/
**/obj/
.env
backend/production.json
*.user
*.suo
.DS_Store
''',

    "README.md": r'''
# Release Radar

Production-to-repository release alignment visibility.

## Requirements

- .NET 8 SDK
- VS Code recommended
- No Node.js, PostgreSQL, RabbitMQ, Docker, or GitHub account needed for Demo mode

## Run locally

Open this root folder in VS Code.

In the integrated terminal:

```bash
cd backend
dotnet run
```

Open:

http://localhost:5080

Alternatively, install the recommended C# extension and press F5.

Do not open frontend/index.html directly or run it using Live Server.
The .NET application serves the frontend and API from the same origin.

Frontend assets are copied into the .NET output directory at build time.
After editing frontend files, stop and run the application again, then refresh
your browser.

## Default mode

The application starts in Demo mode, with six simulated repositories.

The data covers:

- Aligned
- Pending
- Diverged
- Production ahead
- Mixed rollout
- Unknown

Refresh rescans the simulated inventory; it does not randomly change the results.

Demo GitHub links are intentionally omitted because the repositories are fictional.

## API

GET /api/health
GET /api/state
POST /api/scan

The frontend polls the snapshot API every 10 seconds.
The backend scans every 300 seconds by default.
Manual refresh requests are throttled to one per 15 seconds.

## Live mode with a local production inventory

1. Copy backend/production.example.json to backend/production.json.
2. Replace every placeholder with a real value.
3. Set generatedAtUtc to a current ISO-8601 UTC timestamp.
4. Supply the full 40-character Git SHA running in production.
5. Supply an image digest with the format sha256:<64 hex characters>.
6. Set releaseBranch to null if release branches are not used.
7. Configure a GitHub token through environment variables.
8. Start the backend in Live mode.

The repository must use the format owner/repository.

Use a fine-grained GitHub token scoped to the relevant repositories with
Contents: read permission. Organization approval or SSO authorization may
also be required.

PowerShell, from the backend folder:

```powershell
$env:Mode = "Live"
$env:Production__Source = "File"
$env:GitHub__Token = "YOUR_TOKEN"
dotnet run
```

Bash, from the backend folder:

```bash
export Mode=Live
export Production__Source=File
export GitHub__Token="YOUR_TOKEN"
dotnet run
```

Do not commit tokens to source control. Environment variables set in a terminal
apply to applications started from that terminal; an already running VS Code
debug session will not automatically inherit them.

The file inventory expires after 30 minutes by default. Your deployment
inventory producer should update generatedAtUtc only after making a fresh
observation, not merely to suppress a stale-data warning.

Update files atomically: write a temporary file, then rename it.

File mode trusts an external inventory. It does not independently verify that
the declared image is actually running.

## Return to Demo mode

PowerShell:

```powershell
$env:Mode = "Demo"
dotnet run
```

Bash:

```bash
export Mode=Demo
dotnet run
```

## Kubernetes mode

The backend also supports direct Kubernetes API discovery.

Set:

Mode=Live
Production__Source=Kubernetes

Configure these values in appsettings.json or environment variables:

- Kubernetes:ApiUrl
- Kubernetes:Namespaces
- Kubernetes:TokenFile
- Kubernetes:CaFile
- Kubernetes:IgnoredContainers

The default token and CA paths are for a pod running inside Kubernetes.

Local Kubernetes access requires a reachable API URL, a readable service-account
token file, and the cluster CA file. This application does not automatically use
your kubectl context or kubeconfig.

Grant its service account the "list" verb on "pods" only in the configured
production namespaces.

Put this annotation on each application pod template:

```yaml
metadata:
  annotations:
    release-alignment/provenance: |
      [
        {
          "container": "app",
          "repo": "your-org/payments-api",
          "sha": "REPLACE_WITH_FULL_40_CHARACTER_GIT_SHA",
          "imageDigest": "sha256:REPLACE_WITH_64_HEX_CHARACTER_DIGEST",
          "releaseBranch": "release/1.0"
        }
      ]
```

Your CI/CD pipeline must replace the placeholders and deploy an immutable image.

The scanner:

1. Lists pods in configured namespaces.
2. Selects Ready pods and ready containers.
3. Ignores explicitly configured sidecars.
4. Reads per-container provenance.
5. Matches the provenance digest against the container's actual imageID.
6. Compares each unique runtime SHA with trunk and its declared release branch.

Missing or mismatched metadata creates coverage warnings, not a green result.

For multi-architecture images, an OCI index digest may differ from the runtime's
platform-specific image digest. Use the exact runtime digest or add registry
manifest resolution before relying on this mode for such images.

A Ready pod is an operational proxy for a live instance, not proof that it
receives HTTP traffic or actively consumes RabbitMQ messages.

## Comparison algorithm

The GitHub comparison is:

production SHA ... branch HEAD SHA

The branch name is resolved to a SHA before comparing, so that each branch
comparison uses an immutable captured head.

ahead_by:
Commits reachable from the branch but not production.

behind_by:
Commits reachable from production but not the branch.

Classification:

| Ahead | Behind | State |
|---|---|---|
| 0 | 0 | Aligned |
| >0 | 0 | Pending |
| 0 | >0 | Production ahead |
| >0 | >0 | Diverged |

API failures and unresolvable branches are Unknown.

Multiple production SHAs produce a separate Mixed rollout flag.
All unique runtime SHAs are compared.

The dashboard uses a range for drift when observed versions differ.
Trunk and release counts are not summed because commits can overlap.

The summary cards are overlapping categories and should not be added together.

## Important limitations

- Git ancestry is not semantic code equivalence.
- Cherry-picks, rebases, and squash merges can show divergence even if code is similar.
- Repositories outside the configured production inventory are not discovered.
- Only explicitly declared release branches are tracked.
- A repository with no release branch is aligned only across its tracked comparisons.
- No deployment changes or repository writes are performed.
- Snapshots are in memory and disappear on restart.
- Run one backend replica for this MVP.
- This MVP has no built-in authentication.
- Do not expose it publicly; use internal access or an SSO-protected ingress.
- Runtime digest matching still trusts CI/CD to map the image to the correct source SHA.
- GitHub API failures are logged, but there is no automatic backoff/retry policy yet.
- Polling at large repository counts can exceed GitHub API limits.

For 100 repositories, two branches, and one production SHA per repository,
a scan can use up to about 500 API requests. Increase the interval, cache
repository metadata and immutable comparisons, or use event-driven refreshes.

## Architecture

Frontend HTML/CSS/JS
    -> .NET snapshot API
    -> in-memory snapshot

.NET background scanner
    -> File or Kubernetes production inventory
    -> GitHub branch resolution
    -> GitHub immutable SHA comparison
    -> snapshot publication

Failed inventory scans keep the previous snapshot with an explicit error.
Failed repository comparisons are Unknown.
Old snapshots are marked stale.

## Why no MassTransit/RabbitMQ/PostgreSQL in this local package?

They are not necessary for a working single-instance visibility dashboard.
Avoiding them makes local startup and the 96-hour hackathon delivery simpler.

After the MVP:

- PostgreSQL: historical observations, audit, lead-time trends, shared snapshots.
- MassTransit/RabbitMQ: deployment/webhook events and comparison worker queues.
- Scheduled reconciliation: recover from missed events and observe real runtime convergence.
- GitHub App: organization-friendly repository access and token management.
- OIDC: internal authentication.
- OCI attestations: verified source-to-image provenance.

## Validation checklist

Build:

```bash
dotnet build backend/ReleaseRadar.Api.csproj
```

With the application running, open:

http://localhost:5080/api/health
http://localhost:5080/api/state

Expected Demo summary:

- Production repositories: 6
- Fully aligned: 1
- Changes not live: 3
- Mixed rollouts: 1
- Needs attention: 3

Verify:

- Search and filters work.
- Inspect expands a repository.
- Refresh queues a scan.
- An immediate repeated refresh returns 429.
- Invalid Live inventory produces a visible failure, not an empty green dashboard.
- Missing GitHub access produces Unknown.
- Mixed production versions are all listed.
- Missing release branch produces Unknown for that comparison.

## Publish

From the project root:

```bash
dotnet publish backend/ReleaseRadar.Api.csproj -c Release -o publish
```

Run from the publish directory:

```bash
cd publish
dotnet ReleaseRadar.Api.dll --urls http://localhost:5080
```

The published wwwroot folder contains the frontend.

## Troubleshooting

### dotnet command not found

Install the .NET 8 SDK and restart your terminal.

### Port 5080 is occupied

From the backend folder:

```bash
dotnet run --no-launch-profile --urls http://localhost:5081
```

Open http://localhost:5081.

### Dashboard does not show frontend edits

Stop and restart dotnet run so frontend files are copied again.
Then hard-refresh the browser.

### Live scan fails

Inspect the backend terminal.
Check production.json location, timestamp, placeholders, token permissions,
repository identity, and full production SHA.

### Kubernetes digest mismatch

Check the ready container's imageID and your provenance annotation.
Do not disable the digest check to hide the mismatch.
'''
}

if ROOT.exists():
    raise SystemExit(
        f"Refusing to overwrite existing folder: {ROOT}\n"
        "Rename or remove that folder, or run this script in another directory."
    )

ZIP_PATH = ROOT.parent / "ReleaseRadar.zip"
if ZIP_PATH.exists():
    raise SystemExit(
        f"Refusing to overwrite existing archive: {ZIP_PATH}\n"
        "Rename or remove it first."
    )

for relative_path, content in FILES.items():
    destination = ROOT / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content.lstrip("\n"), encoding="utf-8")

with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(ROOT.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(ROOT.parent))

print()
print("Release Radar project created successfully.")
print(f"Project folder: {ROOT}")
print(f"ZIP archive:    {ZIP_PATH}")
print()
print("Next steps:")
print("  1. Open the ReleaseRadar folder in VS Code.")
print("  2. Open the integrated terminal.")
print("  3. Run:")
print("       cd backend")
print("       dotnet run")
print("  4. Browse to http://localhost:5080")
print()
print("Starts in Demo mode. No GitHub credentials are required.")