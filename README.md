# git-remote-gcs

Use Google Cloud Storage as a Git remote. This is a GCP equivalent of [awslabs/git-remote-s3](https://github.com/awslabs/git-remote-s3).

It provides a [git remote helper](https://git-scm.com/docs/gitremote-helpers) that lets you use a GCS bucket as a serverless Git server — no VMs, no Secure Source Manager, just a bucket.

## Installation

### macOS / Linux

```bash
pip install git-remote-gcs
```

That's it. `pip` installs the `git-remote-gcs` script to a directory that's typically already on your PATH (e.g. `/usr/local/bin` or `~/.local/bin`).

Verify it works:

```bash
which git-remote-gcs
```

If `which` returns nothing, add your Python scripts directory to your PATH. Find it with:

```bash
python3 -m site --user-base
```

Then add `<that-path>/bin` to your shell profile:

```bash
# For zsh (default on macOS)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# For bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### Windows

```powershell
pip install git-remote-gcs
```

**Important:** Git on Windows uses its own shell and cannot find Python scripts on the normal PATH. You need to copy the executable into Git's directory. Run this from an **Administrator** PowerShell:

```powershell
copy C:\Users\%USERNAME%\AppData\Roaming\Python\Python313\Scripts\git-remote-gcs.exe "C:\Program Files\Git\mingw64\libexec\git-core\git-remote-gcs.exe"
```

> **Note:** Your Python version folder (e.g. `Python313`) may differ. Find the exact path with:
>
> ```powershell
> where.exe git-remote-gcs.exe
> ```
>
> If that returns nothing, check `python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"`

Verify it works:

```powershell
git-remote-gcs
# Should print: Usage: git-remote-gcs <remote-name> <url>
```

### Install from source

```bash
git clone <this-repo>
cd git-remote-gcs
pip install .
```

On Windows, also run the copy step above after installing.

## Prerequisites

1. **Google Cloud SDK** — install from https://cloud.google.com/sdk/docs/install-sdk

2. **Authentication** — run once per machine:

```bash
gcloud auth application-default login
```

Other auth methods also work: service account keys via `GOOGLE_APPLICATION_CREDENTIALS`, Workload Identity (GKE), or attached service accounts (Compute Engine, Cloud Shell).

3. **A GCS bucket** (or create one):

```bash
gcloud storage buckets create gs://my-git-bucket --location=us-west1
```

4. **IAM permissions** on the bucket. Minimum required:
   - `storage.objects.create`
   - `storage.objects.get`
   - `storage.objects.delete`
   - `storage.objects.list`

   The simplest way is the **Storage Object Admin** role on the bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://my-git-bucket \
  --member="user:you@example.com" \
  --role="roles/storage.objectAdmin"
```

## Quick Start

### Create a new repo

```bash
mkdir my-repo
cd my-repo
git init
git remote add origin gcs://my-git-bucket/my-repo

echo "Hello" > hello.txt
git add -A
git commit -m "initial commit"
git branch -M main
git push --set-upstream origin main
```

### Clone a repo

```bash
git clone gcs://my-git-bucket/my-repo my-repo-clone
```

### Branches

```bash
cd my-repo
git checkout -b feature-branch
touch new_file.txt
git add -A
git commit -m "new feature"
git push origin feature-branch
```

## Team Setup

To give a colleague access:

**1. Grant them IAM access to the bucket:**

```bash
gcloud storage buckets add-iam-policy-binding gs://my-git-bucket \
  --member="user:colleague@example.com" \
  --role="roles/storage.objectAdmin"
```

Or grant access to a Google Group:

```bash
gcloud storage buckets add-iam-policy-binding gs://my-git-bucket \
  --member="group:devs@example.com" \
  --role="roles/storage.objectAdmin"
```

For read-only access (clone and pull only), use `roles/storage.objectViewer` instead.

**2. They install on their machine:**

```bash
pip install git-remote-gcs
gcloud auth application-default login
git clone gcs://my-git-bucket/my-repo
```

On Windows, they also need to run the copy step from the [Windows installation section](#windows).

## Access Control

Access is controlled entirely through GCS IAM. You can scope permissions per-repo using bucket prefixes and IAM conditions:

```bash
gcloud storage buckets add-iam-policy-binding gs://my-git-bucket \
  --member="user:dev@example.com" \
  --role="roles/storage.objectAdmin" \
  --condition="expression=resource.name.startsWith('projects/_/buckets/my-git-bucket/objects/my-repo/'),title=my-repo-access"
```

Multiple repos can share the same bucket with different prefixes:

```
gcs://my-git-bucket/repo-a
gcs://my-git-bucket/repo-b
gcs://my-git-bucket/team/project-c
```

## Data Encryption

GCS encrypts all data at rest by default with Google-managed keys. For additional control, use [Customer-Managed Encryption Keys (CMEK)](https://cloud.google.com/storage/docs/encryption/customer-managed-keys):

```bash
gcloud storage buckets update gs://my-git-bucket \
  --default-encryption-key=projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY
```

## Concurrent Push Protection

`git-remote-gcs` uses GCS [generation-match preconditions](https://cloud.google.com/storage/docs/request-preconditions) to implement per-reference locking, preventing concurrent pushes to the same branch.

If a lock acquisition fails:

```
error refs/heads/main "failed to acquire ref lock at my-repo/refs/heads/main/LOCK.lock.
Another client may be pushing. If this persists beyond 60s,
run git-gcs doctor gcs://my-git-bucket/my-repo --lock-ttl 60 to inspect and clear stale locks."
```

Configure the lock TTL via environment variable:

```bash
export GIT_REMOTE_GCS_LOCK_TTL=120  # seconds, default is 60
```

## Managing the Remote

### Doctor — diagnose and fix issues

```bash
git-gcs doctor gcs://my-git-bucket/my-repo
git-gcs doctor gcs://my-git-bucket/my-repo --delete-bundle  # remove conflicting bundles
git-gcs doctor gcs://my-git-bucket/my-repo --lock-ttl 30    # clear locks older than 30s
```

### Protect/unprotect branches

```bash
git-gcs protect gcs://my-git-bucket/my-repo main
git-gcs unprotect gcs://my-git-bucket/my-repo main
```

Protected branches cannot be force-pushed to or deleted.

### Delete a remote branch

```bash
git-gcs delete-branch gcs://my-git-bucket/my-repo -b old-feature
```

## Under the Hood

### How it works

Bundles are stored in GCS as `<prefix>/<ref>/<sha>.bundle`.

**Push:**

1. Acquire a per-ref lock using GCS generation-match preconditions
2. Create a git bundle: `git bundle create <sha>.bundle <ref>`
3. Upload the bundle to `<prefix>/<ref>/<sha>.bundle`
4. Clean up the previous bundle for that ref
5. Release the lock

**Fetch:**

1. List all objects under `<prefix>/refs/` to discover refs and SHAs
2. Download the bundle for each requested ref
3. Unbundle locally with `git bundle unbundle`

**List:**

1. Scan `<prefix>/refs/` for `.bundle` objects
2. Extract ref names and SHAs from the object keys
3. Read `<prefix>/HEAD` for the default branch

### Storage layout

```
gs://my-git-bucket/my-repo/
├── HEAD                                    # default branch ref
├── refs/
│   ├── heads/
│   │   ├── main/
│   │   │   └── abc123...def.bundle         # branch bundle
│   │   └── feature/
│   │       └── 789abc...012.bundle
│   └── tags/
│       └── v1.0/
│           └── 345def...678.bundle
```

### Debugging

```bash
# Verbose output
GIT_REMOTE_GCS_VERBOSE=1 git push origin main

# Or use git's verbosity flag
git -c transfer.verbosity=2 push origin main
```

## Platform Compatibility

|               | macOS                                   | Linux          | Windows                  |
| ------------- | --------------------------------------- | -------------- | ------------------------ |
| `pip install` | Works directly                          | Works directly | Requires extra copy step |
| Auth          | `gcloud auth application-default login` | Same           | Same                     |
| GUI clients   | Terminal-based Git only                 | Same           | Same                     |

> **Note:** GUI clients like GitHub Desktop do not support custom remote helpers. Use the command line for push/pull/clone, then open the repo in your GUI for commits, diffs, and branch management. VS Code's built-in Git panel may work directly.

## Comparison with Alternatives

| Feature         | git-remote-gcs    | Secure Source Manager | GitHub + Cloud Build | Gitea on VM  |
| --------------- | ----------------- | --------------------- | -------------------- | ------------ |
| Cost            | ~$0 (GCS storage) | $1,000/mo             | Free (public)        | Free tier VM |
| Managed         | Yes (GCS)         | Yes                   | Yes                  | No           |
| IAM integration | Native GCP IAM    | Native GCP IAM        | Separate             | None         |
| Setup time      | Minutes           | Minutes               | Minutes              | 30+ min      |
| UI / PRs        | No                | Yes                   | Yes                  | Yes          |
| Serverless      | Yes               | Yes                   | N/A                  | No           |

## Inspired By

- [awslabs/git-remote-s3](https://github.com/awslabs/git-remote-s3) — the S3 equivalent this project is modeled after
- [bgahagan/git-remote-s3](https://github.com/bgahagan/git-remote-s3) — the original S3 remote helper

## License

Apache-2.0
