using System.Collections.Concurrent;
using System.Net.Http.Headers;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;

var frontendPath = Path.Combine(AppContext.BaseDirectory, "wwwroot");

if (!Directory.Exists(frontendPath))
{
    throw new DirectoryNotFoundException(
        $"Frontend output directory is missing: {frontendPath}. " +
        "Run dotnet build and verify that the frontend files were copied.");
}
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
