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
