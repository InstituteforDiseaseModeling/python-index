# Migrating from JFrog Artifactory to GitHub

*Step-by-step guide. Draft, 2026-10-29.*

This guide moves our content from JFrog Artifactory (`packagesidmod.jfrog.io`) to GitHub. Every step has a **dry run** that changes nothing, can be **re-run safely**, and writes a **log** of every file or image. Nothing in Artifactory is changed or deleted.

## 1. What goes where

| Content in Artifactory | Goes to | Script |
|---|---|---|
| Python packages: `idm-pypi-production`, all packages and versions | **GitHub Releases** (the files) + **GitHub Pages** (the pip index), in one repo | `migrate_python_to_github.py` |
| Docker images: `idm-docker-public`, `docker-production-public` | **GitHub Container Registry** (`ghcr.io`) | `migrate_docker_images.py` |
| npm packages: `idm-npm-production` | **GitHub Packages** (npm) | `migrate_npm_to_github.py` |
| Plain files: `idm-data` | **GitHub Releases** | `migrate_files_to_github.py` |

**Not migrated:** the Docker staging repositories and images found only in `idm-docker-production` (decided in `artifact-hosting-proposal.md`, 2.2), and `idm-pypi-staging`. Anything someone later needs can still be copied while Artifactory is running.

### How the Python index works

One GitHub repo, for example `InstituteforDiseaseModeling/python-index`, holds both parts:

- **The package files** (`.whl`, `.tar.gz`) are attached to **GitHub Releases** in that repo, one release per package, tagged `pypi/<package>` (e.g. `pypi/emodpy`). Release files don't live in git history, so the repo stays small. Each file can be up to 2 GiB.
- **The index pages** are small HTML files, one per package, listing its files with their SHA-256. They're published as a website with **GitHub Pages**, at `https://institutefordiseasemodeling.github.io/python-index/simple/`. pip reads these pages, then downloads the files from the releases.

## 2. Before you start

### Tools

| Tool | Needed for | Check |
|---|---|---|
| Python 3 | all scripts | `python --version` |
| GitHub CLI `gh` | Python, files, npm | `gh --version` |
| `crane` (go-containerregistry) | Docker | `crane version`; must be Google's crane, not another tool of the same name. Download from https://github.com/google/go-containerregistry/releases |
| `npm` | npm | `npm --version` |
| `git` | publishing the index | `git --version` |

### Artifactory access

In every PowerShell window you use:
```powershell
cd C:\jfrog_migration
$env:ARTIFACTORY_URL   = "https://packagesidmod.jfrog.io/artifactory"
$env:ARTIFACTORY_TOKEN = "<artifactory token>"
```

### GitHub sign-in

Sign in through the browser. This also handles the organization's single sign-on:
```powershell
gh auth login
gh auth refresh -s write:packages,read:packages
```

Let `crane` use the same login for the container registry:
```powershell
gh auth token | crane auth login ghcr.io -u <your-github-username> --password-stdin
```
If pushes are still refused ("DENIED"), check that your account may create packages in the organization (org **Settings → Packages**), or use a **classic** personal access token with `write:packages`, authorized for SSO (**Configure SSO → Authorize**).

### GitHub setup (once)

1. **Create the index repo** in the organization, e.g. `python-index`, **public**, with a README, so it has a first commit (needed for releases).
2. **Turn on GitHub Pages** for it after the first index is pushed (step 3.4): **Settings → Pages → Build and deployment → Deploy from a branch → `gh-pages` / root**.
3. **Package visibility:** new container and npm packages in an organization are usually **private by default**. Check the org's **Settings → Packages** for the default, and plan to set public packages to **Public** (step 4.3).

## 3. Python packages

### 3.1 Dry run

```powershell
python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --exclude dtk-api
```
Lists every package file as `would-upload` (or `already-on-github`) in `python_github_log.csv`. Nothing is uploaded.

- `--exclude dtk-api`: its PyPI name belongs to an unrelated project with higher version numbers, so it shouldn't be offered next to PyPI (proposal 7.2). Add other packages to leave out here.
- A file flagged *"name has characters GitHub may change"* may get a different name on GitHub; check it after upload.

### 3.2 Trial

```powershell
python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --exclude dtk-api --execute --limit 1
```
Uploads one file, then writes the index for what's on GitHub to `github-index\`. Check the release appears under the repo's **Releases**.

### 3.3 Everything

```powershell
python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --exclude dtk-api --execute
```
About 10.7 GB. Each file is downloaded, checked against Artifactory's SHA-256, uploaded, and deleted locally. If it stops partway, run the same command again; uploaded files are skipped. At the end it rebuilds `github-index\`.

### 3.4 Publish the index

First time:
```powershell
git clone https://github.com/InstituteforDiseaseModeling/python-index.git C:\python-index-site
cd C:\python-index-site
git checkout --orphan gh-pages
git rm -rf . --quiet
Copy-Item -Recurse -Force C:\jfrog_migration\github-index\* .
git add -A
git commit -m "Python package index"
git push origin gh-pages
```
Then turn on GitHub Pages (2, step 2). After each later run of the script:
```powershell
cd C:\python-index-site
Copy-Item -Recurse -Force C:\jfrog_migration\github-index\* .
git add -A; git commit -m "Update index"; git push origin gh-pages
```
To rebuild the pages during the migration without uploading anything: `python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --exclude dtk-api --index-only`. After the migration, use `build_github_index.py` (3.6).

### 3.5 Check it works

```powershell
pip download --no-deps --dest $env:TEMP\check --index-url https://institutefordiseasemodeling.github.io/python-index/simple/ "idmtools==1.7.11"
```

### 3.6 Publishing new versions after the migration

From now on, the index is built from GitHub alone with `build_github_index.py`, no Artifactory needed. Work in the folder you publish (`C:\python-index-site`), so it keeps its `hashes.json`:

```powershell
# 1. Upload the new version's files to the package's release (create the release first for a new package)
gh release upload pypi/idmtools dist\* -R InstituteforDiseaseModeling/python-index
#    new package:  gh release create pypi/<package> dist\* -R InstituteforDiseaseModeling/python-index --title pypi/<package> --notes "Python package <package>"

# 2. Rebuild the pages and publish them
cd C:\python-index-site
python C:\jfrog_migration\build_github_index.py --github-repo InstituteforDiseaseModeling/python-index --site-dir .
git add -A; git commit -m "Add idmtools 3.2.0"; git push origin gh-pages
```

- `hashes.json` holds each file's SHA-256. Migrated files already have theirs, from the checksums verified against Artifactory, so only new files are downloaded once to compute theirs.
- Files removed from a release disappear from the index the next time it's rebuilt.
- Draft releases are left out until they're published.

### 3.7 Automating publishing with GitHub Actions

Once set up, publishing a new version is just pushing a version tag, like publishing to PyPI from CI. The workflow files are in `C:\jfrog_migration\github-workflows\`.

**In the `python-index` repo (once):**
1. On the `main` branch, add the tools and the workflow:
   - `tools/build_github_index.py` and `tools/github_releases.py`, copied from `C:\jfrog_migration`
   - `.github/workflows/rebuild-index.yml`, copied from `github-workflows\python-index\`
2. Make sure the `gh-pages` branch exists (3.4) and GitHub Pages serves it.
3. Under **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions**, so the workflow can push to `gh-pages`.
4. Test it: **Actions → Rebuild package index → Run workflow**.

**Create a token for package repos (once):** a fine-grained personal access token or GitHub App with **Contents: Read and write** on `python-index` only. Store it as an **organization secret** named `PYTHON_INDEX_TOKEN`, available to the package repos.

**In each package repo:**
1. Copy `github-workflows\package-repo\publish-to-python-index.yml` to `.github/workflows/`.
2. If the repo builds several packages (like `idmtools`), list their folders in `PACKAGE_DIRS` at the top of the file, e.g. `"idmtools_core idmtools_models"`.
3. Publish by pushing a tag:
   ```powershell
   git tag v3.2.0
   git push origin v3.2.0
   ```

**What happens:** the package repo builds the package, uploads the files to `pypi/<package>` in `python-index`, creating the release for a new package, then triggers `rebuild-index.yml`. That workflow rebuilds the pages and pushes them to `gh-pages`, and GitHub Pages publishes them a minute or two later.

- **Versions can't be replaced:** uploading a file name that already exists fails, so the run stops instead of overwriting.
- **Concurrent publishes are safe:** rebuilds run one at a time, and the last one covers everything published meanwhile.
- **If the pages don't update** after the workflow pushes to `gh-pages`, GitHub Pages may not be rebuilding on pushes made by the workflow's own token. In that case, switch **Settings → Pages → Source** to **GitHub Actions** and add the standard `actions/upload-pages-artifact` and `actions/deploy-pages` steps after the push.

## 4. Docker images

### 4.1 Dry run

```powershell
python migrate_docker_images.py --target ghcr.io/institutefordiseasemodeling --repos idm-docker-public docker-production-public
```
Each `image:tag` is listed once, even if it's in both repositories. Tags with **different** images in the two repositories show as `conflict`; decide per tag (see 4.2).

### 4.2 Trial, then everything

```powershell
# one image
python migrate_docker_images.py --target ghcr.io/institutefordiseasemodeling --repos idm-docker-public docker-production-public --execute --images 'emodpy-hiv'
# everything, preferring idm-docker-public for conflicting tags
python migrate_docker_images.py --target ghcr.io/institutefordiseasemodeling --repos idm-docker-public docker-production-public --execute --prefer-first-repo
```
`crane` uploads only layers GitHub doesn't have yet, and the script checks each image's digest after copying. The public repositories allow anonymous pulls, so no Artifactory login is needed. Run from a fast connection: the largest layer is several GB, and GitHub limits how long one layer's upload may take.

### 4.3 Make the images public

In the organization's **Packages** tab, open each image → **Package settings → Change visibility → Public**. Public images can be pulled without a login, as today.

## 5. npm packages

GitHub Packages only accepts names under the organization's scope, so packages are **renamed**: `widget` or `@old/widget` becomes `@institutefordiseasemodeling/widget`. The script rewrites the name inside each package; nothing else changes.

```powershell
python migrate_npm_to_github.py --scope institutefordiseasemodeling             # dry run
python migrate_npm_to_github.py --scope institutefordiseasemodeling --execute
```
- Versions go up oldest first, and `latest` is then set to the highest stable version.
- Dependencies between our own packages, inside `package.json`, keep their old names. Check the 4 packages by hand.
- **Installing from GitHub's npm registry always needs a GitHub login,** even for public packages.

## 6. Plain files (`idm-data`)

```powershell
python migrate_files_to_github.py --github-repo InstituteforDiseaseModeling/python-index             # dry run
python migrate_files_to_github.py --github-repo InstituteforDiseaseModeling/python-index --execute
```
- One release per top-level folder, tagged `files/<folder>`. Releases can't contain folders, so `models/v1/a.bin` becomes asset `v1__a.bin` in release `files/models`.
- Each release gets a `MANIFEST.csv` with every file's original path, asset name, size and SHA-256.
- Files of 2 GiB or more are reported as `too-large` and skipped. Keep those in Azure Blob Storage.
- Use a separate repo, e.g. `idm-data`, by changing `--github-repo`, if you'd rather not mix files with the Python index.

## 7. After migration

### 7.1 Tell users what changes

| Before (Artifactory) | After (GitHub) |
|---|---|
| `pip install --index-url https://packagesidmod.jfrog.io/artifactory/api/pypi/pypi-production/simple idmtools` | `pip install --extra-index-url https://institutefordiseasemodeling.github.io/python-index/simple/ "idmtools==1.7.11"` (pin exact versions) |
| `docker pull packagesidmod-idm-docker-public.jfrog.io/<image>:<tag>` | `docker pull ghcr.io/institutefordiseasemodeling/<image>:<tag>` |
| npm: `widget` from Artifactory | `@institutefordiseasemodeling/widget`, with `.npmrc`: `@institutefordiseasemodeling:registry=https://npm.pkg.github.com` and a GitHub token |
| Files: `https://packagesidmod.jfrog.io/artifactory/idm-data/models/v1/a.bin` | `gh release download files/models -R <repo> -p v1__a.bin`, or the asset's download URL |

**For pip, use `--extra-index-url`** (the index holds only our packages, so dependencies like `numpy` still come from PyPI) and **pin exact versions**. pip picks the highest version across both indexes, so pinning matters for names someone else owns on PyPI, like `pycomps` (proposal 7.2).

### 7.2 Check, then switch off

1. **Compare the logs** (`python_github_log.csv`, `docker_migrate_log.csv`, `npm_github_log.csv`, `files_github_log.csv`): nothing left as `failed`, `hash-mismatch` or `conflict` without a decision.
2. **Update CI, docs and install instructions** to the new addresses.
3. **Find automated jobs** still downloading from Artifactory (bulk downloads, see the policy doc 5.1) and repoint them.
4. **Set Artifactory read-only** for a few weeks, and watch its request logs for remaining users.
5. **Shut Artifactory down.**

### 7.3 Undo

Nothing in Artifactory changes, so undoing means deleting what was created on GitHub: releases (`gh release delete <tag> -R <repo> --cleanup-tag`), container and npm packages (organization **Packages** tab), and the `gh-pages` branch.

## 8. Limits and risks

| Topic | Detail |
|---|---|
| File size | Release files must be under 2 GiB. Container layers up to about 10 GB, and each must upload within a time limit. |
| Files per release | GitHub limits the number of files per release (around 1,000). Our largest package, `emodpy`, has about 110. |
| File names | GitHub may change unusual characters in release file names. The index uses the names GitHub reports, so pip still finds them. |
| Changing versions | Release files can be replaced or deleted. Publish only through these scripts or CI. GitHub's *immutable releases* would lock this, but it also blocks adding files to an existing release, so turn it on only if each package release is complete. |
| Name conflicts | `dtk-api` is excluded. `pycomps` is published with the conditions in the policy (2.1): exact version pins, and a move to `idm-pycomps`. |
| Logins | Public Python index and public images: no login. npm from GitHub: always a login. |
| Costs | Public releases, Pages and public images: free. Private packages count against the organization's GitHub Packages storage. |

*Limits, prices and GitHub behaviour are from general knowledge; confirm against GitHub's current documentation.*
