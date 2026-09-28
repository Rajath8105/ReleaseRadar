using System.Text.Json;

public sealed class GitHubClient(HttpClient http)
{
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
