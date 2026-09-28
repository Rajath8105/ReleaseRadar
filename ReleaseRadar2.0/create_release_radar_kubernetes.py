from pathlib import Path
import zipfile

ROOT = Path.cwd() / "ReleaseRadar-Kubernetes"
ZIP_PATH = ROOT.with_suffix(".zip")

FILES = {}

FILES["backend/ReleaseRadar.Api.csproj"] = r'''
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <StaticWebAssetsEnabled>false</StaticWebAssetsEnabled>
  </PropertyGroup>

  <ItemGroup>
    <None Include="../frontend/**/*">
      <TargetPath>wwwroot/%(RecursiveDir)%(Filename)%(Extension)</TargetPath>
      <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>
      <CopyToPublishDirectory>PreserveNewest</CopyToPublishDirectory>
    </None>
  </ItemGroup>
</Project>
'''

FILES["backend/appsettings.json"] = r'''
{
  "Mode": "Demo",
  "Scan": {
    "IntervalSeconds": 600,
    "StaleAfterSeconds": 1200
  },
  "Kubernetes": {
    "Executable": "kubectl",
    "Context": "",
    "Namespace": "ns-gop-199419-prd-k8s",
    "TimeoutSeconds": 60
  },
  "GitHub": {
    "ApiBaseUrl": "https://sgithub.fr.world.socgen/api/v3/",
    "TimeoutSeconds": 45
  },
  "Defaults": {
    "TrunkBranch": "master",
    "ImageTagShaPattern": "^\\d{8}-\\d{6}-(?<sha>[0-9a-fA-F]{10})$"
  },
  "Services": [
    {
      "Id": "sequence-orchestrator",
      "Name": "Sequence Orchestrator",
      "DeploymentName": "gop-ipsequenceorchestrator-prd-deployment",
      "ContainerName": "ip.sequenceorchestrator",
      "Repository": "ITEC-GTB-ARE/Sgcib.Ip.SequenceOrchestrator",
      "TrunkBranch": "master",
      "ReleaseBranch": "release/August2026HotFix-OnlyMemoryFix"
    }
  ],
  "Logging": {
    "LogLevel": {
      "Default": "Information",
      "Microsoft.AspNetCore": "Warning"
    }
  },
  "AllowedHosts": "*"
}
'''

FILES["backend/Properties/launchSettings.json"] = r'''
{
  "$schema": "http://json.schemastore.org/launchsettings.json",
  "profiles": {
    "ReleaseRadar": {
      "commandName": "Project",
      "launchBrowser": true,
      "applicationUrl": "http://localhost:5080",
      "environmentVariables": {
        "ASPNETCORE_ENVIRONMENT": "Development"
      }
    }
  }
}
'''

FILES["backend/Models.cs"] = r'''
public sealed class ServiceConfig
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string DeploymentName { get; set; } = "";
    public string ContainerName { get; set; } = "";
    public string Repository { get; set; } = "";
    public string? TrunkBranch { get; set; }
    public string? ReleaseBranch { get; set; }
    public string? ImageTagShaPattern { get; set; }
}

public sealed record PodObservation(
    string Pod,
    string Container,
    string Image,
    string ImageTag,
    string ImageId,
    string ShortSha,
    string? Error);

public sealed record CommitSample(string Sha, string Message, string Url);

public sealed record Comparison(
    string Kind,
    string Branch,
    string ProductionSha,
    string HeadSha,
    int? Ahead,
    int? Behind,
    string State,
    string Url,
    List<CommitSample> Commits,
    string? Error = null);

public sealed record ServiceResult(
    string Id,
    string Name,
    string Deployment,
    string Namespace,
    string Repository,
    string RepositoryUrl,
    string State,
    bool Mixed,
    int DesiredReplicas,
    int ObservedPods,
    int ReadyContainers,
    List<PodObservation> Observations,
    List<Comparison> Comparisons,
    List<string> Warnings,
    string? Error = null);

public sealed record Snapshot(
    DateTimeOffset? ObservedAtUtc,
    DateTimeOffset? CompletedAtUtc,
    string Mode,
    string Context,
    string Namespace,
    int DiscoveredDeployments,
    List<ServiceResult> Services,
    List<string> CoverageWarnings,
    string? Error = null);

public static class Alignment
{
    public static string Classify(int ahead, int behind) =>
        (ahead, behind) switch
        {
            (0, 0) => "Aligned",
            (> 0, 0) => "Pending",
            (0, > 0) => "Production ahead",
            _ => "Diverged"
        };

    public static string Aggregate(IEnumerable<Comparison> comparisons)
    {
        var states = comparisons.Select(x => x.State).ToHashSet();

        if (states.Count == 0 || states.Contains("Unknown")) return "Unknown";
        if (states.Contains("Diverged")) return "Diverged";
        if (states.Contains("Pending")) return "Pending";
        if (states.Contains("Production ahead")) return "Production ahead";

        return "Aligned";
    }
}
'''

FILES["backend/KubectlClient.cs"] = r'''
using System.Diagnostics;
using System.Text.Json;

public sealed class KubectlClient(IConfiguration config)
{
    public async Task<string> GetContextAsync(CancellationToken ct)
    {
        var configured = config["Kubernetes:Context"];

        if (!string.IsNullOrWhiteSpace(configured))
            return configured;

        return (await RunAsync(["config", "current-context"], ct)).Trim();
    }

    public async Task<JsonDocument> ListAsync(
        string context,
        string ns,
        string resource,
        CancellationToken ct)
    {
        var output = await RunAsync(
            [
                "--context", context,
                "--namespace", ns,
                "--request-timeout=45s",
                "get", resource,
                "-o", "json"
            ],
            ct);

        return JsonDocument.Parse(output);
    }

    private async Task<string> RunAsync(
        string[] arguments,
        CancellationToken ct)
    {
        var start = new ProcessStartInfo
        {
            FileName = config["Kubernetes:Executable"] ?? "kubectl",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        // ArgumentList avoids shell interpolation and quoting problems.
        foreach (var argument in arguments)
            start.ArgumentList.Add(argument);

        using var process = new Process { StartInfo = start };

        if (!process.Start())
            throw new InvalidOperationException("Could not start kubectl.");

        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);

        timeout.CancelAfter(TimeSpan.FromSeconds(
            config.GetValue("Kubernetes:TimeoutSeconds", 60)));

        try
        {
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            Kill(process);
            throw new TimeoutException(
                "kubectl timed out. Check network access and authentication.");
        }
        catch
        {
            Kill(process);
            throw;
        }

        var output = await stdout;
        var error = await stderr;

        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"kubectl returned exit code {process.ExitCode}: {error}");
        }

        return output;
    }

    private static void Kill(Process process)
    {
        try
        {
            if (!process.HasExited)
                process.Kill(entireProcessTree: true);
        }
        catch
        {
            // Preserve the original error.
        }
    }
}

public static class KubeJson
{
    public static string Text(JsonElement element, string name)
    {
        return element.ValueKind == JsonValueKind.Object &&
               element.TryGetProperty(name, out var value) &&
               value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";
    }

    public static IEnumerable<JsonElement> Array(
        JsonElement element, string name)
    {
        return element.ValueKind == JsonValueKind.Object &&
               element.TryGetProperty(name, out var value) &&
               value.ValueKind == JsonValueKind.Array
            ? value.EnumerateArray().ToArray()
            : [];
    }

    public static JsonElement Object(JsonElement element, string name)
    {
        return element.ValueKind == JsonValueKind.Object &&
               element.TryGetProperty(name, out var value)
            ? value
            : default;
    }

    public static int Number(JsonElement element, string name, int fallback = 0)
    {
        return element.ValueKind == JsonValueKind.Object &&
               element.TryGetProperty(name, out var value) &&
               value.TryGetInt32(out var result)
            ? result
            : fallback;
    }

    public static bool True(JsonElement element, string name)
    {
        return element.ValueKind == JsonValueKind.Object &&
               element.TryGetProperty(name, out var value) &&
               value.ValueKind == JsonValueKind.True;
    }

    public static bool OwnedBy(
        JsonElement item, string kind, HashSet<string> ownerIds)
    {
        return Array(Object(item, "metadata"), "ownerReferences").Any(owner =>
            True(owner, "controller") &&
            Text(owner, "kind") == kind &&
            ownerIds.Contains(Text(owner, "uid")));
    }
}
'''

FILES["backend/GitHubClient.cs"] = r'''
using System.Text.Json;

public sealed class GitHubClient(HttpClient http)
{
    // Scanner is single-threaded. These dictionaries are only used by that scanner.
    private readonly Dictionary<string, string> resolvedCommits = new();
    private readonly Dictionary<string, ComparisonData> comparisons = new();

    private sealed record ComparisonData(
        int Ahead,
        int Behind,
        string Url,
        List<CommitSample> Commits);

    private async Task<JsonDocument> GetAsync(string path, CancellationToken ct)
    {
        using var response = await http.GetAsync(path, ct);

        if (!response.IsSuccessStatusCode)
        {
            var remaining = response.Headers.TryGetValues(
                "X-RateLimit-Remaining", out var values)
                ? string.Join(",", values)
                : "unknown";

            throw new InvalidOperationException(
                $"GitHub HTTP {(int)response.StatusCode}. " +
                $"Rate-limit remaining: {remaining}. " +
                "Check repository access, ref names, and API endpoint.");
        }

        await using var stream = await response.Content.ReadAsStreamAsync(ct);

        return await JsonDocument.ParseAsync(stream, cancellationToken: ct);
    }

    public async Task<string> ResolveAsync(
        string repo,
        string reference,
        bool immutable,
        CancellationToken ct)
    {
        var key = $"{repo}|{reference}";

        if (immutable && resolvedCommits.TryGetValue(key, out var cached))
            return cached;

        using var document = await GetAsync(
            $"repos/{repo}/commits/{Uri.EscapeDataString(reference)}", ct);

        var sha = KubeJson.Text(document.RootElement, "sha");

        if (!System.Text.RegularExpressions.Regex.IsMatch(
                sha, "^[a-fA-F0-9]{40}$"))
        {
            throw new InvalidOperationException(
                "GitHub did not return a full 40-character commit SHA.");
        }

        if (immutable)
        {
            if (resolvedCommits.Count >= 5000) resolvedCommits.Clear();
            resolvedCommits[key] = sha;
        }

        return sha;
    }

    public async Task<Comparison> CompareAsync(
        string repo,
        string repoUrl,
        string productionSha,
        string branch,
        string kind,
        string headSha,
        CancellationToken ct)
    {
        var key = $"{repo}|{productionSha}|{headSha}";

        if (!comparisons.TryGetValue(key, out var data))
        {
            if (productionSha.Equals(headSha, StringComparison.OrdinalIgnoreCase))
            {
                data = new ComparisonData(
                    0, 0,
                    $"{repoUrl}/compare/{productionSha}...{headSha}",
                    []);
            }
            else
            {
                using var document = await GetAsync(
                    $"repos/{repo}/compare/{productionSha}...{headSha}" +
                    "?per_page=5&page=1",
                    ct);

                var root = document.RootElement;

                // Required fields: do not convert missing counts into false alignment.
                var ahead = root.GetProperty("ahead_by").GetInt32();
                var behind = root.GetProperty("behind_by").GetInt32();

                var commits = KubeJson.Array(root, "commits")
                    .Select(item => new CommitSample(
                        KubeJson.Text(item, "sha"),
                        KubeJson.Text(KubeJson.Object(item, "commit"), "message")
                            .Split('\n')[0],
                        KubeJson.Text(item, "html_url")))
                    .ToList();

                data = new ComparisonData(
                    ahead,
                    behind,
                    KubeJson.Text(root, "html_url"),
                    commits);
            }

            if (comparisons.Count >= 5000) comparisons.Clear();
            comparisons[key] = data;
        }

        return new Comparison(
            kind, branch, productionSha, headSha,
            data.Ahead, data.Behind,
            Alignment.Classify(data.Ahead, data.Behind),
            data.Url, data.Commits);
    }

    public async Task<string> RepositoryUrlAsync(string repo, CancellationToken ct)
    {
        using var document = await GetAsync($"repos/{repo}", ct);
        return KubeJson.Text(document.RootElement, "html_url");
    }
}
'''

FILES["backend/Scanner.cs"] = r'''
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Channels;

public sealed class Scanner(
    IConfiguration config,
    KubectlClient kubectl,
    GitHubClient github,
    ILogger<Scanner> logger)
{
    private Snapshot current = new(
        null, null, "Starting", "", "", 0, [], [],
        "Initial scan has not completed.");

    private int scanning;
    private readonly object requestLock = new();
    private DateTimeOffset lastRequest = DateTimeOffset.MinValue;

    private readonly Channel<bool> requests =
        Channel.CreateBounded<bool>(new BoundedChannelOptions(1)
        {
            FullMode = BoundedChannelFullMode.DropWrite,
            SingleReader = true
        });

    public Snapshot Current => Volatile.Read(ref current);
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

    public async Task WaitAsync(CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);

        timeout.CancelAfter(TimeSpan.FromSeconds(
            Math.Max(30, config.GetValue("Scan:IntervalSeconds", 600))));

        try
        {
            await requests.Reader.ReadAsync(timeout.Token);
            while (requests.Reader.TryRead(out _)) { }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            // Time for scheduled reconciliation.
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

            var services = config.GetSection("Services").Get<ServiceConfig[]>() ?? [];
            Validate(services);

            var ns = config["Kubernetes:Namespace"]
                     ?? throw new InvalidOperationException("Namespace is required.");

            var context = await kubectl.GetContextAsync(ct);

            if (string.IsNullOrWhiteSpace(context))
                throw new InvalidOperationException("Kubernetes context is empty.");

            // Exactly three namespace list operations per scan.
            // These are read-only and use the explicitly captured context.
            using var deploymentsDoc = await kubectl.ListAsync(
                context, ns, "deployments", ct);

            using var replicaSetsDoc = await kubectl.ListAsync(
                context, ns, "replicasets", ct);

            using var podsDoc = await kubectl.ListAsync(
                context, ns, "pods", ct);

            var observedAt = DateTimeOffset.UtcNow;

            var deployments = KubeJson.Array(
                deploymentsDoc.RootElement, "items").ToArray();

            var replicaSets = KubeJson.Array(
                replicaSetsDoc.RootElement, "items").ToArray();

            var pods = KubeJson.Array(podsDoc.RootElement, "items").ToArray();

            var warnings = new List<string>
            {
                "Source identity is inferred from Jenkins image tags. " +
                "This assumes tags identify the actual built commit and are immutable."
            };

            if (string.IsNullOrWhiteSpace(config["Kubernetes:Context"]))
            {
                warnings.Add(
                    "Kubernetes context is taken from your local current-context. " +
                    "Pin Kubernetes:Context to avoid accidental cluster switching.");
            }

            var configuredNames = services.Select(x => x.DeploymentName).ToHashSet();

            foreach (var deployment in deployments)
            {
                var name = KubeJson.Text(
                    KubeJson.Object(deployment, "metadata"), "name");

                if (!configuredNames.Contains(name))
                    warnings.Add($"Unmapped Deployment: {name}");
            }

            // Branch heads are resolved once per repo/ref during this scan.
            var heads = new Dictionary<string, string>();
            var urls = new Dictionary<string, string>();
            var results = new List<ServiceResult>();

            foreach (var service in services)
            {
                try
                {
                    results.Add(await InspectServiceAsync(
                        service, ns, deployments, replicaSets, pods,
                        heads, urls, ct));
                }
                catch (Exception exception)
                    when (exception is not OperationCanceledException ||
                          !ct.IsCancellationRequested)
                {
                    logger.LogWarning(
                        exception, "Service scan failed for {Service}.", service.Id);

                    results.Add(new ServiceResult(
                        service.Id, service.Name, service.DeploymentName,
                        ns, service.Repository, "", "Unknown", false,
                        0, 0, 0, [], [], [],
                        "Service inspection failed. Check backend logs."));
                }
            }

            Volatile.Write(ref current, new Snapshot(
                observedAt, DateTimeOffset.UtcNow,
                "Live", context, ns, deployments.Length,
                results, warnings));
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            logger.LogError(exception, "Release Radar scan failed.");

            // Preserve previous observations, clearly marked as failed/outdated.
            Volatile.Write(ref current, Current with
            {
                Error = "Latest scan failed. Previous results, if any, are retained. " +
                        "Check the backend terminal for details."
            });
        }
        finally
        {
            Interlocked.Exchange(ref scanning, 0);
        }
    }

    private static void Validate(ServiceConfig[] services)
    {
        if (services.Length == 0)
            throw new InvalidOperationException("Configure at least one service.");

        if (services.Select(x => x.Id).Distinct().Count() != services.Length)
            throw new InvalidOperationException("Service IDs must be unique.");

        foreach (var service in services)
        {
            if (string.IsNullOrWhiteSpace(service.Id) ||
                string.IsNullOrWhiteSpace(service.DeploymentName) ||
                string.IsNullOrWhiteSpace(service.ContainerName) ||
                !Regex.IsMatch(service.Repository, @"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"))
            {
                throw new InvalidOperationException(
                    $"Invalid service mapping: {service.Id}");
            }
        }
    }

    private async Task<ServiceResult> InspectServiceAsync(
        ServiceConfig service,
        string ns,
        JsonElement[] deployments,
        JsonElement[] replicaSets,
        JsonElement[] pods,
        Dictionary<string, string> heads,
        Dictionary<string, string> urls,
        CancellationToken ct)
    {
        var deployment = deployments.FirstOrDefault(item =>
            KubeJson.Text(KubeJson.Object(item, "metadata"), "name") ==
            service.DeploymentName);

        if (deployment.ValueKind == JsonValueKind.Undefined)
        {
            return Empty(service, ns, "Missing workload",
                "Configured Deployment was not found.");
        }

        var spec = KubeJson.Object(deployment, "spec");
        var desired = KubeJson.Number(spec, "replicas", 1);
        var podSpec = KubeJson.Object(KubeJson.Object(spec, "template"), "spec");

        var containerNames = KubeJson.Array(podSpec, "containers")
            .Select(x => KubeJson.Text(x, "name")).ToArray();

        if (!containerNames.Contains(service.ContainerName))
        {
            return Empty(service, ns, "Unknown",
                $"Container '{service.ContainerName}' not found. Available: " +
                string.Join(", ", containerNames), desired);
        }

        var deploymentUid = KubeJson.Text(
            KubeJson.Object(deployment, "metadata"), "uid");

        var deploymentIds = new HashSet<string> { deploymentUid };

        var replicaSetIds = replicaSets
            .Where(x => KubeJson.OwnedBy(x, "Deployment", deploymentIds))
            .Select(x => KubeJson.Text(KubeJson.Object(x, "metadata"), "uid"))
            .ToHashSet();

        var ownedPods = pods
            .Where(x => KubeJson.OwnedBy(x, "ReplicaSet", replicaSetIds))
            .ToArray();

        var observations = new List<PodObservation>();
        var warnings = new List<string>();

        var pattern = service.ImageTagShaPattern
            ?? config["Defaults:ImageTagShaPattern"]
            ?? @"^\d{8}-\d{6}-(?<sha>[0-9a-fA-F]{10})$";

        var regex = new Regex(pattern, RegexOptions.CultureInvariant,
            TimeSpan.FromSeconds(1));

        if (!regex.GetGroupNames().Contains("sha"))
            throw new InvalidOperationException("Tag pattern needs named group 'sha'.");

        foreach (var pod in ownedPods)
        {
            var metadata = KubeJson.Object(pod, "metadata");
            var name = KubeJson.Text(metadata, "name");
            var status = KubeJson.Object(pod, "status");

            var phase = KubeJson.Text(status, "phase");
            if (phase is "Succeeded" or "Failed") continue;

            var ready = KubeJson.Array(status, "conditions").Any(x =>
                KubeJson.Text(x, "type") == "Ready" &&
                KubeJson.Text(x, "status") == "True");

            var container = KubeJson.Array(status, "containerStatuses")
                .FirstOrDefault(x =>
                    KubeJson.Text(x, "name") == service.ContainerName);

            if (!ready || !KubeJson.True(container, "ready"))
            {
                warnings.Add($"Pod not ready: {name}");
                continue;
            }

            if (!string.IsNullOrWhiteSpace(
                    KubeJson.Text(metadata, "deletionTimestamp")))
            {
                warnings.Add(
                    $"Terminating pod still reports Ready and is included: {name}");
            }

            // Prefer runtime-reported image information.
            var image = KubeJson.Text(container, "image");
            var imageId = KubeJson.Text(container, "imageID");

            var tag = ExtractTag(image);
            var match = regex.Match(tag);

            var sha = match.Success
                ? match.Groups["sha"].Value.ToLowerInvariant()
                : "";

            string? error = null;

            if (!Regex.IsMatch(sha, @"^[a-f0-9]{7,40}$"))
            {
                error = "Runtime image tag does not match the configured SHA pattern.";
                sha = "";
            }

            if (string.IsNullOrWhiteSpace(imageId))
                warnings.Add($"Runtime imageID is missing for pod: {name}");

            observations.Add(new PodObservation(
                name, service.ContainerName, image, tag, imageId, sha, error));
        }

        if (observations.Count == 0)
        {
            return new ServiceResult(
                service.Id, service.Name, service.DeploymentName, ns,
                service.Repository, "", "No live version", false,
                desired, ownedPods.Length, 0, [], [], warnings,
                desired == 0
                    ? "Deployment is scaled to zero."
                    : "No ready application container was observed.");
        }

        var mixed = observations.Select(x =>
                string.IsNullOrWhiteSpace(x.ImageId) ? x.Image : x.ImageId)
            .Distinct().Count() > 1 ||
            observations.Where(x => x.ShortSha != "")
                .Select(x => x.ShortSha).Distinct().Count() > 1;

        if (mixed)
        {
            warnings.Add(
                "Multiple production image identities or source SHAs observed. " +
                "Inspect rollout state; multi-architecture images can also have different digests.");
        }

        var comparisons = new List<Comparison>();
        var repoUrl = "";

        try
        {
            if (!urls.TryGetValue(service.Repository, out repoUrl))
            {
                repoUrl = await github.RepositoryUrlAsync(service.Repository, ct);
                urls[service.Repository] = repoUrl;
            }
        }
        catch (Exception exception)
            when (exception is not OperationCanceledException ||
                  !ct.IsCancellationRequested)
        {
            logger.LogWarning(exception, "Repository lookup failed.");

            return new ServiceResult(
                service.Id, service.Name, service.DeploymentName, ns,
                service.Repository, "", "Unknown", mixed,
                desired, ownedPods.Length, observations.Count,
                observations, [], warnings,
                "Repository unavailable. Check GitHub access and backend logs.");
        }

        var trunk = service.TrunkBranch
                    ?? config["Defaults:TrunkBranch"]
                    ?? "master";

        var branches = new List<(string Kind, string Branch)> { ("Trunk", trunk) };

        if (!string.IsNullOrWhiteSpace(service.ReleaseBranch))
            branches.Add(("Release", service.ReleaseBranch!));

        foreach (var shortSha in observations
                     .Where(x => x.ShortSha != "")
                     .Select(x => x.ShortSha)
                     .Distinct())
        {
            foreach (var branch in branches)
            {
                var productionSha = shortSha;
                var headSha = "";

                try
                {
                    productionSha = await github.ResolveAsync(
                        service.Repository, shortSha, true, ct);

                    var key = $"{service.Repository}|{branch.Branch}";

                    if (!heads.TryGetValue(key, out headSha))
                    {
                        headSha = await github.ResolveAsync(
                            service.Repository, branch.Branch, false, ct);

                        heads[key] = headSha;
                    }

                    comparisons.Add(await github.CompareAsync(
                        service.Repository, repoUrl, productionSha,
                        branch.Branch, branch.Kind, headSha, ct));
                }
                catch (Exception exception)
                    when (exception is not OperationCanceledException ||
                          !ct.IsCancellationRequested)
                {
                    logger.LogWarning(
                        exception, "Comparison failed for {Service}/{Branch}.",
                        service.Id, branch.Branch);

                    comparisons.Add(new Comparison(
                        branch.Kind, branch.Branch, productionSha, headSha,
                        null, null, "Unknown", "", [],
                        "Could not resolve or compare commits. Check backend logs."));
                }
            }
        }

        var state = observations.Any(x => x.Error is not null)
            ? "Unknown"
            : Alignment.Aggregate(comparisons);

        return new ServiceResult(
            service.Id, service.Name, service.DeploymentName, ns,
            service.Repository, repoUrl, state, mixed,
            desired, ownedPods.Length, observations.Count,
            observations, comparisons, warnings);
    }

    private static string ExtractTag(string image)
    {
        var withoutDigest = image.Split('@', 2)[0];
        var slash = withoutDigest.LastIndexOf('/');
        var colon = withoutDigest.LastIndexOf(':');

        return colon > slash ? withoutDigest[(colon + 1)..] : "";
    }

    private static ServiceResult Empty(
        ServiceConfig service,
        string ns,
        string state,
        string error,
        int desired = 0)
    {
        return new ServiceResult(
            service.Id, service.Name, service.DeploymentName,
            ns, service.Repository, "", state, false,
            desired, 0, 0, [], [], [], error);
    }
}

public sealed class ScanWorker(Scanner scanner) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Yield();

        while (!stoppingToken.IsCancellationRequested)
        {
            await scanner.ScanAsync(stoppingToken);
            await scanner.WaitAsync(stoppingToken);
        }
    }
}
'''

FILES["backend/DemoData.cs"] = r'''
public static class DemoData
{
    public static Snapshot Create()
    {
        return new Snapshot(
            DateTimeOffset.UtcNow,
            DateTimeOffset.UtcNow,
            "Demo",
            "demo-cluster",
            "demo-prd",
            6,
            [
                Make("Sequence Orchestrator", "sequence-orchestrator", 8, 0, 3, 0),
                Make("Orders API", "orders-api", 0, 0, 0, 0),
                Make("Identity API", "identity-api", 4, 2, 1, 2),
                Make("Notification Worker", "notification-worker", 0, 2, 0, 1),
                Make("Inventory API", "inventory-api", 5, 0, 2, 0, mixed: true),
                new ServiceResult(
                    "shipping", "Shipping API", "shipping-api", "demo-prd",
                    "demo/shipping-api", "", "No live version", false,
                    1, 1, 0, [], [],
                    ["Pod not ready: shipping-api-example"],
                    "No ready application container was observed.")
            ],
            ["Simulated data. Start Live mode to inspect your actual namespace."]);
    }

    private static ServiceResult Make(
        string name,
        string id,
        int trunkAhead,
        int trunkBehind,
        int releaseAhead,
        int releaseBehind,
        bool mixed = false)
    {
        var shas = mixed
            ? new[] { new string('a', 40), new string('b', 40) }
            : new[] { new string('a', 40) };

        var observations = new List<PodObservation>();
        var comparisons = new List<Comparison>();

        for (var i = 0; i < shas.Length; i++)
        {
            var sha = shas[i];
            var tag = "20260827-092923-" + sha[..10];

            observations.Add(new PodObservation(
                $"{id}-example-{i}",
                "app",
                $"registry.example/{id}:{tag}",
                tag,
                "sha256:" + new string(i == 0 ? '1' : '2', 64),
                sha[..10],
                null));

            comparisons.Add(MakeComparison(
                "Trunk", "master", sha, Math.Max(0, trunkAhead - i), trunkBehind));

            comparisons.Add(MakeComparison(
                "Release", "release/1.8", sha,
                Math.Max(0, releaseAhead - i), releaseBehind));
        }

        return new ServiceResult(
            id, name, id + "-deployment", "demo-prd",
            "demo/" + id, "", Alignment.Aggregate(comparisons),
            mixed, observations.Count, observations.Count, observations.Count,
            observations, comparisons, []);

        static Comparison MakeComparison(
            string kind, string branch, string sha, int ahead, int behind)
        {
            return new Comparison(
                kind, branch, sha,
                ahead == 0 && behind == 0 ? sha : new string('c', 40),
                ahead, behind, Alignment.Classify(ahead, behind),
                "",
                ahead > 0
                    ? [new CommitSample(
                        new string('c', 40),
                        "Improve resilience and request handling", "")]
                    : []);
        }
    }
}
'''

FILES["backend/Program.cs"] = r'''
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
'''

FILES["frontend/index.html"] = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Release Radar</title>
  <link rel="stylesheet" href="/styles.css">
  <script src="/app.js" defer></script>
</head>
<body>
  <div class="shell">
    <aside>
      <a class="brand" href="/"><span class="logo">◎</span> Release<span>Radar</span></a>
      <div class="nav-label">ENGINEERING WORKSPACE</div>
      <div class="nav active">▦ &nbsp; Release overview</div>
      <a class="nav" href="/api/state" target="_blank" rel="noopener">⌘ &nbsp; Snapshot API</a>
      <a class="nav" href="/api/health" target="_blank" rel="noopener">♡ &nbsp; API health</a>
      <div class="aside-note">
        <strong>Production-first visibility</strong>
        <p>Actual runtime images.<br>Immutable Git comparisons.<br>Read-only discovery.</p>
      </div>
      <div class="aside-footer">.NET 8 · Kubernetes · GitHub</div>
    </aside>

    <main>
      <header>
        <span>Engineering / <strong>Release alignment</strong></span>
        <span class="pill" id="mode">STARTING</span>
      </header>

      <div class="content">
        <section class="hero">
          <div>
            <div class="eyebrow">PRODUCTION → REPOSITORY</div>
            <h1>What’s running.<br><span>What’s waiting.</span></h1>
            <p>Compare production source versions with trunk and release branches.</p>
          </div>
          <div class="actions">
            <button id="refresh">↻ Refresh inventory</button>
            <span id="scan-info">Preparing first scan…</span>
          </div>
        </section>

        <div class="scope">
          <span>Context <strong id="context">—</strong></span>
          <span>Namespace <strong id="namespace">—</strong></span>
          <span>Deployments discovered <strong id="discovered">—</strong></span>
        </div>

        <div id="notices" aria-live="polite"></div>
        <div id="action-message" role="status"></div>

        <section id="metrics" class="metrics"></section>

        <section class="panel">
          <div class="panel-title">
            <div>
              <h2>Production services</h2>
              <p>Ready container images mapped to Git source commits</p>
            </div>
            <span class="pill" id="count">0 services</span>
          </div>

          <div class="toolbar">
            <input id="search" type="search"
              placeholder="Search service, repository, or deployment…"
              aria-label="Search services">

            <select id="filter" aria-label="Filter status">
              <option value="">All states</option>
              <option>Aligned</option>
              <option>Pending</option>
              <option>Diverged</option>
              <option>Production ahead</option>
              <option>Unknown</option>
              <option>No live version</option>
              <option>Missing workload</option>
              <option>Mixed versions</option>
            </select>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Service / Repository</th>
                  <th>Production image tag</th>
                  <th>Trunk drift</th>
                  <th>Release drift</th>
                  <th>Status</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody id="rows">
                <tr><td colspan="6" class="empty">Loading dashboard…</td></tr>
              </tbody>
            </table>
          </div>

          <div class="table-footer">
            <span id="results">Waiting for data</span>
            <span>Trunk and release counts overlap; do not add them.</span>
          </div>
        </section>

        <div class="explanation">
          <strong>How to read this dashboard</strong>
          <p>
            “Not live” means branch commits absent from production Git history.
            “Production-only” means production commits absent from that branch.
            Counts describe ancestry, not semantic code equivalence or feature flags.
            Image-to-source identity relies on your Jenkins tag convention.
          </p>
        </div>

        <footer>
          <span id="updated">Waiting for first observation…</span>
          <span>Read-only visibility · Not a release approval gate</span>
        </footer>
      </div>
    </main>
  </div>
</body>
</html>
'''

FILES["frontend/styles.css"] = r'''
:root {
  --bg:#090f1c;
  --panel:#121d30;
  --line:#26344d;
  --text:#eaf0fc;
  --muted:#91a2bc;
  --blue:#91afff;
  --green:#6bdfb1;
  --amber:#f4c77a;
  --red:#ff8fa1;
  --purple:#bd9cff;
}
* { box-sizing:border-box; }
body {
  margin:0;
  font-family:Inter,"Segoe UI",system-ui,sans-serif;
  background:var(--bg);
  color:var(--text);
  font-size:14px;
}
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
button,input,select { font:inherit; }
button { cursor:pointer; }
button:disabled { opacity:.5; cursor:wait; }
button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible {
  outline:2px solid var(--blue); outline-offset:3px;
}
.shell { display:flex; min-height:100vh; }
aside {
  width:230px; position:fixed; height:100vh;
  padding:28px 18px; background:#0d1625;
  border-right:1px solid var(--line);
  display:flex; flex-direction:column;
}
.brand {
  display:flex; align-items:center; gap:5px;
  font-size:20px; font-weight:750; color:var(--text);
  margin-bottom:45px; letter-spacing:-.7px;
}
.brand > span:last-child { color:#a3b6df; }
.logo {
  background:linear-gradient(135deg,#7c9fff,#956ce9);
  display:grid; place-items:center;
  border-radius:10px; width:35px; height:35px;
  color:white; margin-right:5px;
}
.nav-label { font-size:9px; letter-spacing:1.5px; color:#7386a4; margin:0 8px 17px; }
.nav { padding:13px 11px; margin-bottom:7px; color:var(--muted); border-radius:8px; font-size:12px; }
.nav.active { background:#86a6ff13; color:#bbceff; border:1px solid #86a6ff25; }
.aside-note {
  margin-top:auto; border:1px solid var(--line);
  background:linear-gradient(135deg,#1b2a43,#121c2d);
  padding:16px; border-radius:11px; font-size:12px;
}
.aside-note p { color:var(--muted); line-height:1.9; font-size:11px; }
.aside-footer { font-size:10px; color:#7488a8; margin:20px 5px 0; }
main { margin-left:230px; width:calc(100% - 230px); }
header {
  display:flex; justify-content:space-between; align-items:center;
  padding:23px 32px; border-bottom:1px solid var(--line);
  font-size:12px; color:var(--muted);
}
header strong { color:#c2d1ed; font-weight:500; }
.pill {
  padding:5px 9px; border:1px solid #7397e445;
  background:#729fff0c; border-radius:6px;
  font-size:10px; color:#b0c6f7; white-space:nowrap;
}
.content {
  max-width:1600px; margin:auto; padding:32px;
  background:radial-gradient(ellipse at 90% 0,#24376240,transparent 40%);
}
.hero { display:flex; justify-content:space-between; align-items:center; gap:25px; margin-bottom:26px; }
.eyebrow { color:#a5bcea; letter-spacing:2px; font-size:10px; margin-bottom:13px; }
h1 { font-size:42px; line-height:1.15; letter-spacing:-1.7px; margin:0; }
h1 span { color:#9eafd1; }
.hero p { font-size:12px; color:var(--muted); margin-top:15px; line-height:1.7; }
.actions { display:flex; flex-direction:column; gap:11px; align-items:center; }
.actions span { font-size:10px; color:var(--muted); }
button {
  border:1px solid #a0baff; color:#0a1937;
  background:linear-gradient(135deg,#9db8ff,#7f9aee);
  padding:11px 16px; border-radius:8px; font-weight:700;
}
.scope {
  display:flex; gap:25px; flex-wrap:wrap; font-size:11px;
  padding:14px 0 20px; color:var(--muted);
}
.scope strong { display:block; color:#c3d2ee; margin-top:5px; font-weight:500; overflow-wrap:anywhere; }
.metrics { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:13px; margin:22px 0; }
.metric {
  border:1px solid var(--line); border-radius:12px;
  padding:18px; background:linear-gradient(135deg,#1b2941,#121d30);
}
.metric-label { font-size:11px; color:var(--muted); }
.metric-value { font-size:33px; font-weight:750; margin:10px 0 7px; color:var(--accent); }
.metric-hint { font-size:10px; color:#7b8fab; line-height:1.6; }
.panel { border:1px solid var(--line); border-radius:13px; background:var(--panel); overflow:hidden; }
.panel-title,.toolbar,.table-footer { display:flex; justify-content:space-between; align-items:center; gap:16px; padding:19px; }
.panel-title { border-bottom:1px solid var(--line); }
h2 { margin:0; font-size:16px; }
.panel-title p { font-size:11px; color:var(--muted); margin:7px 0 0; }
input,select {
  padding:10px 12px; border:1px solid var(--line);
  background:#0c1627; color:var(--text); border-radius:7px; font-size:12px;
}
input { width:min(440px,100%); }
select { min-width:175px; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; }
th {
  color:#8d9fba; background:#0e1829; font-size:9px;
  text-transform:uppercase; letter-spacing:1px;
  text-align:left; padding:13px 17px; white-space:nowrap;
}
td { padding:18px 17px; border-top:1px solid #26344daa; font-size:12px; vertical-align:top; }
.main-row:hover { background:#1a294280; }
.service-name { font-weight:650; color:#dce7fb; white-space:nowrap; }
.sub { font-size:10px; color:var(--muted); margin-top:7px; }
code { font-family:Consolas,monospace; color:#c1d3ff; font-size:11px; }
.image-tag { background:#1c2c46; border:1px solid #304460; border-radius:5px; padding:4px 6px; display:inline-block; margin-bottom:4px; white-space:nowrap; }
.badge { display:inline-flex; padding:5px 8px; font-size:10px; border-radius:5px; white-space:nowrap; font-weight:600; }
.aligned { color:var(--green); background:#6bdfb113; border:1px solid #6bdfb125; }
.pending { color:var(--amber); background:#f4c77a13; border:1px solid #f4c77a25; }
.diverged { color:var(--red); background:#ff8fa113; border:1px solid #ff8fa125; }
.production-ahead { color:var(--purple); background:#bd9cff13; border:1px solid #bd9cff25; }
.unknown,.no-live-version,.missing-workload { color:#bfcbdd; background:#bfcbdd13; border:1px solid #bfcbdd25; }
.mixed { color:var(--blue); background:#91afff13; border:1px solid #91afff25; margin-top:6px; }
.drift { font-size:18px; font-weight:700; }
.muted { color:var(--muted); }
.inspect { background:transparent; color:#b1c8ff; border:1px solid #344966; font-size:10px; padding:6px 9px; }
.detail-cell { background:#0b1525; padding:18px; }
.detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.comparison { background:#16243a; border:1px solid var(--line); padding:15px; border-radius:9px; min-width:0; }
.comparison-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.comparison p { font-size:11px; line-height:1.7; overflow-wrap:anywhere; }
.comparison ul { font-size:11px; color:var(--muted); line-height:1.9; padding-left:18px; }
.evidence { background:#14233b; border-left:2px solid #88aaff; margin-top:13px; padding:14px; overflow-wrap:anywhere; }
.evidence p { font-size:11px; line-height:1.8; margin-bottom:0; }
.notice {
  border:1px solid #f4c77a35; background:#f4c77a09;
  padding:12px 15px; border-radius:9px; color:#ddc397;
  font-size:11px; line-height:1.8; margin-bottom:12px;
}
.notice summary { cursor:pointer; }
.error { color:#ffb2bf; border-color:#ff8fa140; background:#ff8fa109; }
.table-footer { border-top:1px solid var(--line); color:var(--muted); font-size:10px; }
.explanation { padding:18px; border:1px solid var(--line); border-radius:11px; margin-top:20px; background:#101d31; }
.explanation strong { font-size:12px; color:#c1d4f7; }
.explanation p { font-size:11px; line-height:1.8; color:var(--muted); margin-bottom:0; }
footer { display:flex; justify-content:space-between; gap:14px; flex-wrap:wrap; color:#7c8eaa; font-size:10px; padding:22px 0; }
.empty { text-align:center; color:var(--muted); padding:45px; }
#action-message { color:var(--blue); font-size:12px; }
#action-message:not(:empty) { padding:10px 0; }

@media(max-width:1200px) {
  .metrics { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .content { padding:24px; }
}
@media(max-width:900px) {
  aside { display:none; }
  main { margin-left:0; width:100%; }
}
@media(max-width:600px) {
  .content { padding:18px 12px; }
  .hero,.toolbar,.panel-title { flex-direction:column; align-items:stretch; }
  .actions { align-items:flex-start; }
  h1 { font-size:34px; }
  .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .detail-grid { grid-template-columns:1fr; }
  .table-footer { align-items:flex-start; flex-direction:column; }
}
'''

FILES["frontend/app.js"] = r'''
"use strict";

let state = null;
let loading = false;
const expanded = new Set();
const $ = id => document.getElementById(id);

const escapeHtml = value => String(value ?? "").replace(
  /[&<>"']/g,
  c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[c]
);

const shortSha = sha => escapeHtml((sha || "").slice(0, 10));

function badge(value) {
  const css = value.toLowerCase().replaceAll(" ", "-");
  return `<span class="badge ${escapeHtml(css)}">● ${escapeHtml(value)}</span>`;
}

function link(url, label) {
  try {
    const parsed = new URL(url);

    if (!["https:", "http:"].includes(parsed.protocol))
      return escapeHtml(label);

    return `<a href="${escapeHtml(parsed.href)}"
      target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch {
    return escapeHtml(label);
  }
}

function range(values) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? String(min) : `${min}–${max}`;
}

function drift(service, kind) {
  const items = service.comparisons.filter(x => x.kind === kind);

  if (service.observations.some(x => x.error))
    return badge("Unknown");

  if (!items.length) {
    return `<span class="muted">${
      kind === "Release" && service.comparisons.length
        ? "Not tracked"
        : "Unavailable"
    }</span>`;
  }

  if (items.some(x => x.state === "Unknown"))
    return badge("Unknown");

  return `
    <span class="drift">${range(items.map(x => x.ahead))}</span>
    <span class="muted">not live</span>
    <div class="sub">
      ${range(items.map(x => x.behind))} production-only
    </div>`;
}

function comparisonHtml(item) {
  return `
    <article class="comparison">
      <div class="comparison-head">
        <strong>${escapeHtml(item.kind)}</strong>
        ${badge(item.state)}
      </div>

      <p>Branch: <code>${escapeHtml(item.branch)}</code></p>

      <p>
        <code>${shortSha(item.productionSha)}</code>
        <span class="muted">production →</span>
        <code>${shortSha(item.headSha) || "unknown"}</code>
        <span class="muted">branch HEAD</span>
      </p>

      ${
        item.error
          ? `<p style="color:var(--red)">${escapeHtml(item.error)}</p>`
          : `<p><strong>${item.ahead}</strong> commits not live ·
               <strong>${item.behind}</strong> production-only</p>`
      }

      ${
        item.url
          ? `<p>${link(item.url, "Open GitHub comparison ↗")}</p>`
          : ""
      }

      ${
        item.commits.length
          ? `<p class="muted">Commit sample, not a complete changelog:</p>
             <ul>${item.commits.map(c =>
               `<li>${link(c.url, c.message)}</li>`
             ).join("")}</ul>`
          : ""
      }
    </article>`;
}

function detailsHtml(service) {
  return `
    ${
      service.error
        ? `<div class="notice error">${escapeHtml(service.error)}</div>`
        : ""
    }

    ${
      service.warnings.length
        ? `<div class="notice"><ul>${service.warnings.map(w =>
            `<li>${escapeHtml(w)}</li>`
          ).join("")}</ul></div>`
        : ""
    }

    <div class="detail-grid">
      ${service.comparisons.map(comparisonHtml).join("")}
    </div>

    <div class="evidence">
      <strong>Production evidence</strong>
      <p>
        Deployment: <code>${escapeHtml(service.deployment)}</code><br>
        Desired replicas: ${service.desiredReplicas} ·
        Observed pods: ${service.observedPods} ·
        Ready application containers: ${service.readyContainers}
      </p>

      ${service.observations.map(item => `
        <p>
          <strong>${escapeHtml(item.pod)}</strong> ·
          ${escapeHtml(item.container)}<br>
          Image: <code>${escapeHtml(item.image)}</code><br>
          Runtime imageID: <code>${escapeHtml(item.imageId || "Unavailable")}</code><br>
          Source SHA suffix: <code>${escapeHtml(item.shortSha || "Unknown")}</code>
          ${
            item.error
              ? `<br><span style="color:var(--red)">${escapeHtml(item.error)}</span>`
              : ""
          }
        </p>
      `).join("")}
    </div>`;
}

function renderRows() {
  if (!state) return;

  const services = state.data.services;
  const search = $("search").value.toLowerCase().trim();
  const filter = $("filter").value;

  const visible = services.filter(service => {
    const text = [
      service.name, service.repository, service.deployment, service.namespace
    ].join(" ").toLowerCase();

    return text.includes(search) &&
      (!filter ||
        (filter === "Mixed versions" ? service.mixed : service.state === filter));
  });

  $("results").textContent =
    `Showing ${visible.length} of ${services.length} configured services`;

  $("rows").innerHTML = visible.length
    ? visible.map(service => {
        const tags = [...new Set(service.observations.map(x => x.imageTag))];
        const open = expanded.has(service.id);

        return `
          <tr class="main-row">
            <td>
              <div class="service-name">${escapeHtml(service.name)}</div>
              <div class="sub">${link(service.repositoryUrl, service.repository)}</div>
              ${
                service.warnings.length
                  ? `<div class="sub">${service.warnings.length} operational notice(s)</div>`
                  : ""
              }
            </td>
            <td>
              ${
                tags.length
                  ? tags.map(tag =>
                      `<code class="image-tag">${escapeHtml(tag || "Unresolved tag")}</code>`
                    ).join("<br>")
                  : '<span class="muted">No live version</span>'
              }
              <div class="sub">${service.readyContainers} ready application container(s)</div>
            </td>
            <td>${drift(service, "Trunk")}</td>
            <td>${drift(service, "Release")}</td>
            <td>
              ${badge(service.state)}
              ${
                service.mixed
                  ? '<br><span class="badge mixed">◐ Mixed versions</span>'
                  : ""
              }
            </td>
            <td>
              <button class="inspect"
                data-id="${escapeHtml(service.id)}"
                aria-expanded="${open}">
                ${open ? "Hide ↑" : "Inspect ↓"}
              </button>
            </td>
          </tr>
          ${
            open
              ? `<tr><td colspan="6" class="detail-cell">${detailsHtml(service)}</td></tr>`
              : ""
          }`;
      }).join("")
    : '<tr><td colspan="6" class="empty">No matching services.</td></tr>';
}

function render() {
  if (!state) return;

  const data = state.data;
  const services = data.services;

  $("mode").textContent = `${data.mode.toUpperCase()} DATA`;
  $("context").textContent = data.context || "Not yet observed";
  $("namespace").textContent = data.namespace || "Not yet observed";
  $("discovered").textContent = data.discoveredDeployments;
  $("count").textContent = `${services.length} services`;

  const metricData = [
    ["Configured services", services.length, "Explicit service-to-repository mappings", "var(--blue)"],
    ["Aligned", services.filter(x => x.state === "Aligned").length, "Across configured comparisons", "var(--green)"],
    ["Changes not live", services.filter(x =>
      x.comparisons.some(c => c.ahead !== null && c.ahead > 0)
    ).length, "Services with branch-only commits", "var(--amber)"],
    ["Mixed versions", services.filter(x => x.mixed).length, "Multiple image identities or source SHAs", "var(--purple)"],
    ["Needs attention", services.filter(x =>
      ["Unknown", "Diverged", "Production ahead", "No live version", "Missing workload"]
        .includes(x.state)
    ).length, "History conflict, missing runtime, or uncertainty", "var(--red)"]
  ];

  $("metrics").innerHTML = metricData.map(([label, value, hint, color]) => `
    <article class="metric" style="--accent:${color}">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
      <div class="metric-hint">${hint}</div>
    </article>
  `).join("");

  let notices = "";

  if (data.error)
    notices += `<div class="notice error">${escapeHtml(data.error)}</div>`;

  if (state.stale)
    notices += '<div class="notice error">Inventory is stale or no successful observation exists. Do not treat displayed results as current release approval.</div>';

  if (data.coverageWarnings.length) {
    notices += `
      <details class="notice">
        <summary>${data.coverageWarnings.length} discovery / coverage notice(s)</summary>
        <ul>${data.coverageWarnings.slice(0, 100).map(w =>
          `<li>${escapeHtml(w)}</li>`
        ).join("")}</ul>
        ${
          data.coverageWarnings.length > 100
            ? "<p>Showing the first 100 notices.</p>"
            : ""
        }
      </details>`;
  }

  $("notices").innerHTML = notices;
  $("refresh").disabled = state.isScanning;

  $("refresh").textContent =
    state.isScanning ? "↻ Scanning…" : "↻ Refresh inventory";

  $("scan-info").textContent = state.isScanning
    ? "Reading Kubernetes and GitHub"
    : `Scheduled every ${state.intervalSeconds}s · UI refreshes every 10s`;

  $("updated").textContent = data.observedAtUtc
    ? `Inventory observed ${new Date(data.observedAtUtc).toLocaleString()}`
    : "Waiting for first observation…";

  renderRows();
}

async function load() {
  if (loading) return;
  loading = true;

  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");

    state = await response.json();
    render();
  } catch {
    $("notices").innerHTML = `
      <div class="notice error">
        Backend unavailable. Any displayed results may be outdated.
        Check that dotnet is still running.
      </div>`;

    $("refresh").disabled = false;
  } finally {
    loading = false;
  }
}

$("search").addEventListener("input", renderRows);
$("filter").addEventListener("change", renderRows);

$("rows").addEventListener("click", event => {
  const button = event.target.closest("button[data-id]");
  if (!button) return;

  const id = button.dataset.id;
  expanded.has(id) ? expanded.delete(id) : expanded.add(id);
  renderRows();
});

$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;

  try {
    const response = await fetch("/api/scan", { method: "POST" });
    const result = await response.json();
    $("action-message").textContent = result.message || "Refresh requested.";
  } catch {
    $("action-message").textContent = "Could not contact the refresh API.";
  } finally {
    await load();
    setTimeout(() => { $("action-message").textContent = ""; }, 6000);
  }
});

load();
setInterval(load, 10000);
'''

FILES["Start-Live.ps1"] = r'''
param(
    [string]$Context = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET SDK not found. Install the .NET 8 SDK."
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found in PATH."
}

$previousMode = $env:Mode
$previousToken = $env:GitHub__Token
$previousContext = $env:Kubernetes__Context

if ([string]::IsNullOrWhiteSpace($Context)) {
    $output = & kubectl config current-context

    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the current Kubernetes context."
    }

    $Context = ($output -join "").Trim()
}

Write-Host ""
Write-Host "Release Radar - Live mode"
Write-Host "Kubernetes context: $Context"
Write-Host "Namespace and services: backend/appsettings.json"
Write-Host "Read-only: no deployment or repository changes."
Write-Host ""

$confirm = Read-Host "Use this context? Type YES to continue"

if ($confirm -cne "YES") {
    Write-Host "Cancelled."
    exit
}

$secureToken = $null
$plainToken = $null

try {
    if ([string]::IsNullOrWhiteSpace($env:GitHub__Token)) {
        $secureToken = Read-Host "Read-only GitHub token" -AsSecureString
        $plainToken = [System.Net.NetworkCredential]::new("", $secureToken).Password
        $env:GitHub__Token = $plainToken
    }

    $env:Mode = "Live"
    $env:Kubernetes__Context = $Context

    Push-Location (Join-Path $PSScriptRoot "backend")

    try {
        Write-Host ""
        Write-Host "Open http://localhost:5080 after startup."
        Write-Host "Press Ctrl+C to stop."
        Write-Host ""

        & dotnet run
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:Mode = $previousMode
    $env:GitHub__Token = $previousToken
    $env:Kubernetes__Context = $previousContext

    $plainToken = $null
    $secureToken = $null
}
'''

FILES[".vscode/extensions.json"] = r'''
{
  "recommendations": [
    "ms-dotnettools.csharp",
    "ms-dotnettools.csdevkit"
  ]
}
'''

FILES[".vscode/tasks.json"] = r'''
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
    }
  ]
}
'''

FILES[".vscode/launch.json"] = r'''
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Release Radar: Debug Demo",
      "type": "coreclr",
      "request": "launch",
      "preLaunchTask": "build",
      "program": "${workspaceFolder}/backend/bin/Debug/net8.0/ReleaseRadar.Api.dll",
      "cwd": "${workspaceFolder}/backend",
      "console": "internalConsole",
      "env": {
        "Mode": "Demo",
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
'''

FILES[".gitignore"] = r'''
**/bin/
**/obj/
.env
*.user
*.suo
.DS_Store
'''

FILES["README.md"] = r'''
# Release Radar - Kubernetes image-tag edition

## What this project does

1. Uses local kubectl access to read one configured namespace.
2. Lists Deployments, ReplicaSets, and pods.
3. Resolves Deployment -> ReplicaSet -> Pod ownership by UID.
4. Selects Ready pods and the configured ready application container.
5. Reads the runtime-reported image and imageID.
6. Extracts the built Git SHA from the image tag.
7. Resolves it to a full SHA through GitHub Enterprise.
8. Resolves master and the configured release branch to exact HEAD SHAs.
9. Compares production against those branch heads.
10. Publishes the results to a local dashboard.

The application performs read-only operations.

## Requirements

- .NET 8 SDK
- kubectl in PATH for Live mode
- Working approved kubeconfig/authentication for Live mode
- Network access to Kubernetes and GitHub Enterprise
- Read-only GitHub token
- VS Code recommended

No Node.js, RabbitMQ, PostgreSQL, or Docker is required.

## Start Demo mode

Open the project root in VS Code.

```powershell
cd backend
dotnet run
'''