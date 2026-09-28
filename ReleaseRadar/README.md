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
