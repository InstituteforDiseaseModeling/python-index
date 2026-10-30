#!/usr/bin/env python3
"""
Move npm packages from Artifactory to GitHub Packages (npm.pkg.github.com).

Usage:
  $env:ARTIFACTORY_URL   = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN = "<access token>"
  gh auth refresh -s write:packages,read:packages      # or set $env:NPM_TOKEN to a classic token
  python migrate_npm_to_github.py --scope institutefordiseasemodeling             # dry run
  python migrate_npm_to_github.py --scope institutefordiseasemodeling --execute

GitHub Packages only accepts package names under the owner's scope, so every
package is renamed: name or @old-scope/name becomes @<scope>/name. The name in
each tarball's package.json is rewritten before publishing; nothing else
changes. Users must install the new names. Dependencies between our own
packages inside package.json are NOT rewritten; check those by hand.

Versions are published oldest first under a temporary dist-tag, then "latest"
is set to the highest stable version, so upload order can't make an old
version "latest".

Safe to re-run: versions already on GitHub are skipped. Every version's
outcome goes to --log. Exits 1 if anything failed.
"""
import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict

from migrate_to_azure import download, semver_key
from triage_artifactory import fetch_files, parse_npm

REGISTRY = "https://npm.pkg.github.com"
TEMP_TAG = "artifactory-migration"


def npm(*args, userconfig):
    """Run npm; return (ok, output). Replaced in tests."""
    exe = shutil.which("npm") or "npm"  # on Windows this finds npm.cmd
    p = subprocess.run([exe, *args, "--registry", REGISTRY, "--userconfig", userconfig],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()


def github_token():
    if os.environ.get("NPM_TOKEN"):
        return os.environ["NPM_TOKEN"]
    p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        sys.exit("No GitHub token: run `gh auth login`, or set NPM_TOKEN to a classic token with write:packages.")
    return p.stdout.strip()


def rename_tarball(src, dest, new_name):
    """Copy an npm tarball, changing "name" in its top-level package.json."""
    with tarfile.open(src, "r:gz") as tin, tarfile.open(dest, "w:gz") as tout:
        done = False
        for m in tin.getmembers():
            data = tin.extractfile(m).read() if m.isfile() else None
            if not done and m.isfile() and m.name.count("/") == 1 and m.name.endswith("/package.json"):
                meta = json.loads(data.decode("utf-8"))
                meta["name"] = new_name
                meta.pop("publishConfig", None)  # would point at the old registry
                data = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
                m.size = len(data)
                done = True
            tout.addfile(m, io.BytesIO(data) if data is not None else None)
    if not done:
        raise ValueError("no package.json found in tarball")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--repos", nargs="+", default=["idm-npm-production"], help="Artifactory npm repos")
    ap.add_argument("--scope", required=True, help="GitHub owner (org) name, lowercase, e.g. institutefordiseasemodeling")
    ap.add_argument("--download-dir", default="downloads")
    ap.add_argument("--log", default="npm_github_log.csv")
    ap.add_argument("--execute", action="store_true", help="Actually publish (default: dry run)")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")
    scope = args.scope.lower().lstrip("@")

    versions = defaultdict(dict)  # new name -> version -> (repo, path, filename)
    for repo in args.repos:
        for item in fetch_files(args.url, repo):
            pkg, version = parse_npm(item["path"], item["name"])
            if pkg is None:
                continue
            new_name = f"@{scope}/{pkg.split('/')[-1]}"
            versions[new_name].setdefault(version, (repo, f"{item['path']}/{item['name']}", item["name"], pkg))

    workdir = tempfile.mkdtemp()
    userconfig = os.path.join(workdir, ".npmrc")
    # A dry run needs the token too: it asks GitHub which versions already exist
    with open(userconfig, "w") as f:
        f.write(f"//npm.pkg.github.com/:_authToken={github_token()}\n@{scope}:registry={REGISTRY}\n")

    counts = defaultdict(int)
    os.makedirs(args.download_dir, exist_ok=True)
    with open(args.log, "w", newline="", encoding="utf-8") as logf:
        log = csv.writer(logf)
        log.writerow(["old_name", "new_name", "version", "status", "detail"])
        print(f"{'EXECUTE' if args.execute else 'DRY RUN'}: -> {REGISTRY} (@{scope})", file=sys.stderr)
        for new_name in sorted(versions):
            published = False
            for version in sorted(versions[new_name], key=semver_key):
                repo, path, filename, old_name = versions[new_name][version]
                ok, _ = npm("view", f"{new_name}@{version}", "version", userconfig=userconfig)
                if ok:
                    status, detail = "already-on-github", ""
                elif not args.execute:
                    status, detail = "would-publish", f"renamed from {old_name}" if old_name != new_name else ""
                else:
                    src = os.path.join(args.download_dir, repo, filename)
                    dest = os.path.join(workdir, f"{new_name.replace('/', '__')}-{version}.tgz")
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    try:
                        download(args.url, repo, path, src)
                        rename_tarball(src, dest, new_name)
                        ok, out = npm("publish", dest, "--tag", TEMP_TAG, userconfig=userconfig)
                        status, detail = ("published", "") if ok else ("failed", out[-200:])
                        published = published or ok
                    except Exception as e:
                        status, detail = "failed", str(e)[-200:]
                counts[status] += 1
                log.writerow([old_name, new_name, version, status, detail])
                logf.flush()
                print(f"  {status:18} {new_name}@{version}  {detail}", file=sys.stderr)

            if args.execute and published:
                stable = [v for v in versions[new_name] if "-" not in v]
                latest = max(stable or versions[new_name], key=semver_key)
                ok, out = npm("dist-tag", "add", f"{new_name}@{latest}", "latest", userconfig=userconfig)
                npm("dist-tag", "rm", new_name, TEMP_TAG, userconfig=userconfig)
                print(f"  {'tagged' if ok else 'tag FAILED':18} {new_name} latest -> {latest}  {'' if ok else out[-200:]}",
                      file=sys.stderr)
                if not ok:
                    counts["failed"] += 1

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:18} {counts[status]:5}")
    print(f"Wrote {args.log}")
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
