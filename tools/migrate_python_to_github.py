#!/usr/bin/env python3
"""
Move Python packages from Artifactory to GitHub: package files go to GitHub
Releases (one release per package, tagged pypi/<package>), and a pip-compatible
package index (PEP 503 "simple" pages) is written for GitHub Pages.

Usage:
  $env:ARTIFACTORY_URL   = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN = "<access token>"
  gh auth login
  python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index               # dry run
  python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --execute     # upload + build index
  python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --index-only  # rebuild index pages
  python migrate_python_to_github.py --github-repo InstituteforDiseaseModeling/python-index --exclude dtk-api --packages idmtools --execute  @ only update idmtools packages

Every package file in --repos (default idm-pypi-production in jfrog) is included, all
versions. Leave packages out with --exclude.

For each file:
  1. Already a release asset with the same name and size -> skip.
  2. Download from Artifactory, check SHA-256 against Artifactory's checksum.
  3. Upload to release pypi/<package> (created if missing) with `gh`.

Then the index is written to --site-dir (default github-index):
  simple/index.html             one link per package
  simple/<package>/index.html   one link per file, pointing at the release
                                asset, with #sha256=<hash> so pip verifies it
The index lists only files that are actually on GitHub, using the names GitHub
reports. Their SHA-256 checksums are also saved in <site-dir>/hashes.json, so
build_github_index.py can maintain the index after Artifactory is gone.
Publish the folder with GitHub Pages (see github-migration-guide.md).

Safe to re-run: uploaded files are skipped. Every file's outcome goes to --log.
Exits 1 if any file failed.
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict

from build_github_index import load_hashes, save_hashes, write_pages
from github_releases import Releases, gh_available
from migrate_to_azure import download, sha256_of
from triage_artifactory import fetch_files, normalize, parse_pypi

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def list_files(base_url, repos, packages, excludes):
    """[{repo, package, version, filename, path, size, sha256}] for every package file."""
    wanted = {normalize(p) for p in packages}
    skip = {normalize(p) for p in excludes}
    files = []
    for repo in repos:
        for item in fetch_files(base_url, repo):
            pkg, version = parse_pypi(item["path"], item["name"])
            if pkg is None or pkg in skip or (wanted and pkg not in wanted):
                continue
            path = item["name"] if item["path"] in ("", ".") else f"{item['path']}/{item['name']}"
            files.append({"repo": repo, "package": pkg, "version": version, "filename": item["name"],
                          "path": path, "size": item.get("size", 0), "sha256": (item.get("sha256") or "").lower()})
    return files


def write_index(site_dir, releases, by_package, title):
    """Write PEP 503 pages for files that exist as release assets, and record their
    Artifactory-verified SHA-256 in hashes.json for build_github_index.py.
    Returns (packages, files) listed."""
    hashes = load_hashes(site_dir)
    packages = {}
    for pkg in sorted(by_package):
        tag = f"pypi/{pkg}"
        assets = releases.assets(tag) or {}
        files = []
        for f in by_package[pkg]:
            asset = assets.get(f["filename"])
            if asset is None:
                continue
            files.append((asset["name"], asset.get("url") or releases.download_url(tag, asset["name"]), f["sha256"]))
            if f["sha256"]:
                hashes[f"{tag}/{asset['name']}"] = {"sha256": f["sha256"], "size": asset.get("size")}
        packages[pkg] = files
    save_hashes(site_dir, hashes)
    return write_pages(site_dir, packages, title)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--repos", nargs="+", default=["idm-pypi-production"], help="Artifactory PyPI repos")
    ap.add_argument("--github-repo", required=True, help="owner/name of the GitHub repo holding releases and the index")
    ap.add_argument("--packages", nargs="+", default=[], help="Only these packages")
    ap.add_argument("--exclude", nargs="+", default=[], metavar="PACKAGE", help="Leave these packages out")
    ap.add_argument("--limit", type=int, metavar="N", help="Only the first N files to upload, for a trial run")
    ap.add_argument("--download-dir", default="downloads")
    ap.add_argument("--site-dir", default="github-index", help="Where to write the index pages")
    ap.add_argument("--log", default="python_github_log.csv")
    ap.add_argument("--execute", action="store_true", help="Actually upload, then build the index (default: dry run)")
    ap.add_argument("--index-only", action="store_true", help="Only rebuild the index pages from what's on GitHub")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")
    if not gh_available():
        sys.exit("The GitHub CLI (gh) is not installed: https://cli.github.com/")

    releases = Releases(args.github_repo)
    print(f"Listing {', '.join(args.repos)} ...", file=sys.stderr)
    files = list_files(args.url, args.repos, args.packages, args.exclude)
    by_package = defaultdict(list)
    for f in files:
        by_package[f["package"]].append(f)
    print(f"  {len(files)} files in {len(by_package)} packages", file=sys.stderr)

    counts, failed = defaultdict(int), 0
    if not args.index_only:
        uploads = 0
        os.makedirs(args.download_dir, exist_ok=True)
        with open(args.log, "w", newline="", encoding="utf-8") as logf:
            log = csv.writer(logf)
            log.writerow(["package", "version", "filename", "status", "release", "detail"])

            def record(f, status, detail=""):
                counts[status] += 1
                log.writerow([f["package"], f["version"], f["filename"], status, f"pypi/{f['package']}", detail])
                logf.flush()
                print(f"  {status:18} {f['filename']}  {detail}", file=sys.stderr)

            print(f"\n{'EXECUTE' if args.execute else 'DRY RUN'}: -> github.com/{args.github_repo} releases",
                  file=sys.stderr)
            for pkg in sorted(by_package):
                tag = f"pypi/{pkg}"
                try:
                    existing = releases.assets(tag)
                except RuntimeError as e:
                    for f in by_package[pkg]:
                        record(f, "failed", str(e)[-200:])
                    continue
                for f in sorted(by_package[pkg], key=lambda f: f["filename"]):
                    if existing and f["filename"] in existing and existing[f["filename"]].get("size") == f["size"]:
                        record(f, "already-on-github")
                        continue
                    if args.limit is not None and uploads >= args.limit:
                        continue
                    uploads += 1
                    note = "" if SAFE_NAME.match(f["filename"]) else "name has characters GitHub may change"
                    if not args.execute:
                        record(f, "would-upload", note)
                        continue
                    dest = os.path.join(args.download_dir, f["repo"], f["filename"])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    try:
                        if not (os.path.exists(dest) and f["sha256"] and sha256_of(dest) == f["sha256"]):
                            download(args.url, f["repo"], f["path"], dest)
                    except Exception as e:
                        record(f, "failed-download", str(e)[-200:])
                        continue
                    actual = sha256_of(dest)
                    if f["sha256"] and actual != f["sha256"]:
                        record(f, "hash-mismatch", f"expected {f['sha256']}, got {actual}")
                        continue
                    try:
                        if existing is None:
                            releases.create(tag, f"Python package {pkg}, migrated from Artifactory.")
                            existing = {}
                        ok, out = releases.upload(tag, dest)
                    except RuntimeError as e:
                        ok, out = False, str(e)
                    if ok:
                        record(f, "uploaded", note)
                        os.remove(dest)
                        existing = releases.assets(tag) or {}
                    else:
                        record(f, "failed", out[-200:])

        print("\nSummary:")
        for status in sorted(counts):
            print(f"  {status:18} {counts[status]:5}")
        failed = sum(n for s, n in counts.items() if s.startswith("failed") or s == "hash-mismatch")
        print(f"Wrote {args.log}")

    if args.execute or args.index_only:
        pkgs, n = write_index(args.site_dir, releases, by_package, "Python packages")
        print(f"\nIndex: {len(pkgs)} packages, {n} files written to {args.site_dir}{os.sep}simple")
        missing = sum(len(v) for v in by_package.values()) - n
        if missing:
            print(f"  {missing} files aren't on GitHub yet, so they're not in the index")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
