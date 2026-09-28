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
                "Inspect rollout state.");
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
