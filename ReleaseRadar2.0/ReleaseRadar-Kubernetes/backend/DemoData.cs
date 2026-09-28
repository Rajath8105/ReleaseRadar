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
