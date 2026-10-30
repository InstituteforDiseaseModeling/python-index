#!/usr/bin/env python3
"""
Copy Docker images from Artifactory to another registry (e.g. GitHub
Container Registry), each image:tag only once, even when the same image is in
several Artifactory repositories.

Usage:
  $env:ARTIFACTORY_URL   = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN = "<access token>"
  python migrate_docker_images.py --target ghcr.io/<org>                 # dry run
  python migrate_docker_images.py --target ghcr.io/<org> --execute       # copy

Default repos: idm-docker-production idm-docker-public docker-production-public
(staging is left out). Change with --repos.

How it works:
  1. Lists every image:tag in the repos from Artifactory's file index (no image
     downloads). A tag is a folder holding manifest.json or list.manifest.json
     (multi-platform). Its digest is that file's SHA-256.
  2. Combines the repos into one list, keyed by image:tag:
       only in one repo                 -> copy once
       in several repos, same digest    -> identical: copy once
       in several repos, other digests  -> conflict: skipped and reported,
                                           unless --prefer-first-repo, which
                                           copies the one from the earliest
                                           repo in --repos
  3. With --execute, copies each with `crane copy`, which uploads only layers
     the target doesn't have yet. A tag already in the target with the same
     digest is skipped; with a different digest it's a conflict and left alone.
     After each copy the target's digest is checked against the source.

Folders named sha256:... are images pushed without a readable tag, or parts of
multi-platform images; they're skipped (a multi-platform tag copies its parts).

Needs crane (https://github.com/google/go-containerregistry) for --execute.
Sign in first:
  crane auth login ghcr.io -u <github-user> -p <token with write:packages>
  crane auth login packagesidmod-idm-docker-production.jfrog.io -u <user> -p <token>   # per repo, if anonymous pulls aren't allowed

Source addresses: on JFrog Cloud, each Docker repo has its own hostname, e.g.
packagesidmod-idm-docker-production.jfrog.io/emodpy-hiv:10112022 (--source-style subdomain, the default for
*.jfrog.io). Use --source-style path for <host>/<repo>/<image> addressing.

To copy specific images only, use --images (names or image:tag, wildcards allowed). With --repos set to one
repo, that also picks where a conflicting tag is copied from:
  python migrate_docker_images.py --target ghcr.io/<org> --execute `
      --images 'idm-rocky-py39:latest' --repos docker-production-public

Safe to re-run: copied images are skipped. Every image's outcome goes to --log.
Exits 1 if anything failed or conflicted.
"""
import argparse
import csv
import fnmatch
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections import defaultdict

from triage_artifactory import fetch_files

DEFAULT_REPOS = ["idm-docker-production", "idm-docker-public", "docker-production-public"]
MANIFESTS = ("manifest.json", "list.manifest.json")


def list_tags(base_url, repo):
    """Return {(image, tag): {"digest", "size", "layers"}} for one repo."""
    folders = defaultdict(lambda: {"digest": None, "size": 0, "layers": {}})
    for item in fetch_files(base_url, repo):
        path = item["path"]
        if "/" not in path or path.startswith(("_uploads", ".")):
            continue
        f = folders[path]
        if item["name"] in MANIFESTS:
            f["digest"] = "sha256:" + (item.get("sha256") or "")
        else:
            f["size"] += item.get("size", 0)
            f["layers"][item.get("sha256") or item["name"]] = item.get("size", 0)
    tags = {}
    for path, f in folders.items():
        image, tag = path.rsplit("/", 1)
        if f["digest"] is None or tag.startswith("sha256:"):
            continue
        tags[(image, tag)] = f
    return tags


def matches(image, tag, patterns):
    return any(fnmatch.fnmatch(image, pat) or fnmatch.fnmatch(f"{image}:{tag}", pat) for pat in patterns)


def plan(per_repo, repos, excludes, prefer_first, includes=()):
    """Combine repos into one list of (image, tag, source_repo, info, status, detail)."""
    keys = sorted({k for tags in per_repo.values() for k in tags})
    rows = []
    for image, tag in keys:
        if includes and not matches(image, tag, includes):
            continue  # not selected with --images
        if matches(image, tag, excludes):
            rows.append((image, tag, "", None, "excluded", ""))
            continue
        found = [(r, per_repo[r][(image, tag)]) for r in repos if (image, tag) in per_repo[r]]
        digests = {info["digest"] for _, info in found}
        repo, info = found[0]
        others = ", ".join(r for r, _ in found[1:])
        if len(digests) == 1:
            detail = f"identical in {others}" if others else ""
            rows.append((image, tag, repo, info, "copy", detail))
        else:
            versions = "; ".join(f"{r}={i['digest'][:19]}" for r, i in found)
            if prefer_first:
                rows.append((image, tag, repo, info, "copy", f"conflict resolved in favour of {repo} ({versions})"))
            else:
                rows.append((image, tag, repo, info, "conflict", f"different images with this tag: {versions}"))
    return rows


def source_ref(host, style, repo, image, tag):
    """Pull address of an image in Artifactory. JFrog Cloud usually addresses Docker repos by subdomain
    (<server>-<repo>.jfrog.io); self-hosted Artifactory often by path (<host>/<repo>)."""
    if style == "subdomain":
        server, _, domain = host.partition(".")
        return f"{server}-{repo}.{domain}/{image}:{tag}"
    return f"{host}/{repo}/{image}:{tag}"


def crane_available():
    """True if `crane` on PATH is go-containerregistry's crane (other tools share the name)."""
    if not shutil.which("crane"):
        return False
    p = subprocess.run(["crane", "copy", "--help"], capture_output=True, text=True)
    return p.returncode == 0 and "copy" in (p.stdout + p.stderr).lower()


def crane(*args):
    """Run crane; return (ok, output). Replaced in tests."""
    p = subprocess.run(["crane", *args], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout or p.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--repos", nargs="+", default=DEFAULT_REPOS,
                    help="Docker repos to combine, in order of preference (default: production and public, no staging)")
    ap.add_argument("--target", required=True, help="Target registry and namespace, e.g. ghcr.io/<org>")
    ap.add_argument("--source-registry", help="Registry host for docker pulls (default: the host of --url)")
    ap.add_argument("--source-style", choices=("subdomain", "path"),
                    help="How Artifactory addresses Docker repos: subdomain = <server>-<repo>.jfrog.io/<image>, "
                         "path = <host>/<repo>/<image>. Default: subdomain for *.jfrog.io, path otherwise")
    ap.add_argument("--images", nargs="+", default=[], metavar="PATTERN",
                    help="Only these images, e.g. 'idm-rocky-py39:latest' or 'idmtools/*' (default: all)")
    ap.add_argument("--exclude", nargs="+", default=[], metavar="PATTERN",
                    help="Leave out images matching these patterns, e.g. 'jenkins-*' 'amelia/*' or 'image:tag'")
    ap.add_argument("--prefer-first-repo", action="store_true",
                    help="For a tag with different images in different repos, copy the one from the earliest repo")
    ap.add_argument("--limit", type=int, metavar="N", help="Only the first N images to copy, for a trial run")
    ap.add_argument("--log", default="docker_migrate_log.csv")
    ap.add_argument("--execute", action="store_true", help="Actually copy (default: dry run)")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")
    source = args.source_registry or urllib.parse.urlparse(args.url).netloc
    style = args.source_style or ("subdomain" if source.endswith(".jfrog.io") else "path")
    target = args.target.rstrip("/")
    if target != target.lower():
        print(f"Note: registry names must be lowercase; using {target.lower()}", file=sys.stderr)
        target = target.lower()
    if args.execute and not crane_available():
        sys.exit("crane from go-containerregistry is not on PATH (another program called crane may be). "
                 "Download it from https://github.com/google/go-containerregistry/releases")

    per_repo = {}
    for repo in args.repos:
        print(f"Listing {repo} ...", file=sys.stderr)
        per_repo[repo] = list_tags(args.url, repo)
        print(f"  {len(per_repo[repo])} tags", file=sys.stderr)

    rows = plan(per_repo, args.repos, args.exclude, args.prefer_first_repo, args.images)
    for pat in args.images:
        if not any(matches(r[0], r[1], [pat]) for r in rows):
            print(f"  Warning: --images {pat} matched no image in {', '.join(args.repos)}", file=sys.stderr)
    to_copy = [r for r in rows if r[4] == "copy"]
    if args.limit is not None:
        keep = {(r[0], r[1]) for r in to_copy[:args.limit]}
        rows = [r for r in rows if r[4] != "copy" or (r[0], r[1]) in keep]

    counts, gb = defaultdict(int), defaultdict(float)
    unique_layers = {}
    with open(args.log, "w", newline="", encoding="utf-8") as logf:
        log = csv.writer(logf)
        log.writerow(["image", "tag", "status", "source_repo", "digest", "size_gb", "target", "detail"])
        print(f"\n{'EXECUTE' if args.execute else 'DRY RUN'}: {source} ({style} addressing) -> {target}", file=sys.stderr)
        for image, tag, repo, info, status, detail in rows:
            name = image.lower()
            dst = f"{target}/{name}:{tag}"
            src = source_ref(source, style, repo, image, tag) if repo else ""
            if status == "copy":
                if name != image:
                    detail = (detail + "; " if detail else "") + f"renamed to lowercase ({name})"
                if not args.execute:
                    status = "would-copy"
                else:
                    ok, have = crane("digest", dst)
                    if ok and have == info["digest"]:
                        status = "already-in-target"
                    elif ok:
                        status, detail = "conflict-in-target", f"target has {have}, source is {info['digest']}"
                    else:
                        ok, out = crane("copy", src, dst)
                        if not ok:
                            status, detail = "failed", out[-300:]
                        else:
                            ok, have = crane("digest", dst)
                            if ok and have == info["digest"]:
                                status = "copied"
                            else:
                                status, detail = "failed", f"after copy, target digest is {have or 'unknown'}"
                if status in ("would-copy", "copied", "already-in-target"):
                    unique_layers.update(info["layers"])
            counts[status] += 1
            if info:
                gb[status] += info["size"] / 1e9
            log.writerow([image, tag, status, repo, info["digest"] if info else "",
                          round(info["size"] / 1e9, 3) if info else "", dst if repo else "", detail])
            logf.flush()
            if status != "excluded":
                print(f"  {status:18} {image}:{tag}  {detail}", file=sys.stderr)

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:18} {counts[status]:5} tags  {gb[status]:8.1f} GB (layers counted per tag)")
    print(f"\nUnique content to be in the target: {sum(unique_layers.values()) / 1e9:.1f} GB "
          f"(each layer counted once)")
    print(f"Wrote {args.log}")
    if counts["failed"] or counts["conflict"] or counts["conflict-in-target"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
