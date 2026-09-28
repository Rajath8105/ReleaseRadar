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
