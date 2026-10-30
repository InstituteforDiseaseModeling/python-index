#!/usr/bin/env python3
"""
Move plain files (an Artifactory generic repo, default idm-data) to GitHub
Releases: one release per top-level folder, tagged files/<folder>.

Usage:
  $env:ARTIFACTORY_URL   = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN = "<access token>"
  gh auth login
  python migrate_files_to_github.py --github-repo <owner>/<repo>             # dry run
  python migrate_files_to_github.py --github-repo <owner>/<repo> --execute

Release assets can't contain folders, so a file's path inside its top-level
folder becomes its asset name with "/" replaced by "__":
  idm-data/models/v1/a.bin  ->  release files/models, asset v1__a.bin
Each release also gets MANIFEST.csv (original path, asset name, size, SHA-256),
so files can be found by their old path and checked after download.

GitHub release assets must be under 2 GiB each; bigger files are reported as
too-large and skipped (keep those in Azure Blob Storage instead).

Safe to re-run: files already uploaded (same name and size) are skipped.
Every file's outcome goes to --log. Exits 1 if any file failed.
"""
import argparse
import csv
import os
import sys
import tempfile
from collections import defaultdict

from github_releases import Releases, gh_available
from migrate_to_azure import download, sha256_of
from triage_artifactory import fetch_files

MAX_ASSET = 2 * 1024 ** 3 - 1  # GitHub: release assets must be under 2 GiB


def asset_name(rel_path):
    return rel_path.replace("/", "__")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--repo", default="idm-data", help="Artifactory generic repo")
    ap.add_argument("--github-repo", required=True, help="owner/name of the GitHub repo for the releases")
    ap.add_argument("--folders", nargs="+", default=[], help="Only these top-level folders")
    ap.add_argument("--download-dir", default="downloads")
    ap.add_argument("--log", default="files_github_log.csv")
    ap.add_argument("--execute", action="store_true", help="Actually upload (default: dry run)")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")
    if not gh_available():
        sys.exit("The GitHub CLI (gh) is not installed: https://cli.github.com/")

    releases = Releases(args.github_repo)
    folders = defaultdict(list)
    for item in fetch_files(args.url, args.repo):
        full = item["name"] if item["path"] in ("", ".") else f"{item['path']}/{item['name']}"
        top, _, rest = full.partition("/")
        folder, rel = (top, rest) if rest else ("root", top)
        if args.folders and folder not in args.folders:
            continue
        folders[folder].append({"path": full, "rel": rel, "size": item.get("size", 0),
                                "sha256": (item.get("sha256") or "").lower()})

    counts = defaultdict(int)
    os.makedirs(args.download_dir, exist_ok=True)
    with open(args.log, "w", newline="", encoding="utf-8") as logf:
        log = csv.writer(logf)
        log.writerow(["path", "release", "asset", "status", "detail"])
        print(f"{'EXECUTE' if args.execute else 'DRY RUN'}: {args.repo} -> github.com/{args.github_repo} releases",
              file=sys.stderr)
        for folder in sorted(folders):
            tag = f"files/{folder}"
            manifest, changed = [], False
            try:
                existing = releases.assets(tag)
            except RuntimeError as e:
                existing, err = None, str(e)
            else:
                err = None
            for f in sorted(folders[folder], key=lambda f: f["rel"]):
                name = asset_name(f["rel"])
                status, detail = None, ""
                if err:
                    status, detail = "failed", err[-200:]
                elif f["size"] > MAX_ASSET:
                    status, detail = "too-large", f"{f['size'] / 1e9:.1f} GB; GitHub limit is 2 GiB per file"
                elif existing and name in existing and existing[name].get("size") == f["size"]:
                    status = "already-on-github"
                elif not args.execute:
                    status = "would-upload"
                else:
                    dest = os.path.join(args.download_dir, args.repo, name)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    try:
                        download(args.url, args.repo, f["path"], dest)
                        actual = sha256_of(dest)
                        if f["sha256"] and actual != f["sha256"]:
                            status, detail = "hash-mismatch", f"expected {f['sha256']}, got {actual}"
                        else:
                            if existing is None:
                                releases.create(tag, f"Files from Artifactory {args.repo}/{folder}. "
                                                     "See MANIFEST.csv for original paths and SHA-256 checksums.")
                                existing = {}
                            ok, out = releases.upload(tag, dest)
                            status, detail = ("uploaded", "") if ok else ("failed", out[-200:])
                            changed = changed or ok
                            existing = releases.assets(tag) or {}
                    except Exception as e:
                        status, detail = "failed", str(e)[-200:]
                    finally:
                        if os.path.exists(dest):
                            os.remove(dest)
                counts[status] += 1
                if status in ("uploaded", "already-on-github"):
                    manifest.append([f["path"], name, f["size"], f["sha256"]])
                log.writerow([f["path"], tag, name, status, detail])
                logf.flush()
                print(f"  {status:18} {f['path']}  {detail}", file=sys.stderr)

            # Refresh the release's manifest when anything was uploaded, or it's missing
            if args.execute and manifest and (changed or "MANIFEST.csv" not in (existing or {})):
                path = os.path.join(tempfile.mkdtemp(), "MANIFEST.csv")
                with open(path, "w", newline="", encoding="utf-8") as mf:
                    w = csv.writer(mf)
                    w.writerow(["original_path", "asset", "size_bytes", "sha256"])
                    w.writerows(manifest)
                ok, out = releases.upload(tag, path, clobber=True)
                print(f"  {'manifest' if ok else 'manifest FAILED'}   {tag}/MANIFEST.csv  {'' if ok else out[-200:]}",
                      file=sys.stderr)
                if not ok:
                    counts["failed"] += 1

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:18} {counts[status]:5}")
    print(f"Wrote {args.log}")
    if counts["failed"] or counts["hash-mismatch"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
