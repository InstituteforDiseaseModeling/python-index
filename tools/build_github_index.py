#!/usr/bin/env python3
"""
Build the pip package index (PEP 503 "simple" pages) from GitHub Releases
alone, with no Artifactory. Use it after the migration, whenever a package
version is added to or removed from the releases.

Usage:
  gh auth login
  python build_github_index.py --github-repo <owner>/<repo>
  python build_github_index.py --github-repo <owner>/<repo> --site-dir C:\\python-index-site

Every release tagged pypi/<package> becomes one package page listing its
.whl, .tar.gz, .zip and .tar.bz2 files, each linked with #sha256=<hash> so pip
checks what it downloads. Draft releases are skipped.

SHA-256 checksums are kept in <site-dir>/hashes.json, keyed by release tag and
file name. A file already in it (same size) isn't downloaded again; a new file
is downloaded once with `gh release download` to compute its checksum.
migrate_python_to_github.py writes hashes.json for migrated files, from
checksums it verified against Artifactory. Files no longer in any release are
dropped from the index and from hashes.json.

Then publish <site-dir> to GitHub Pages (see github-migration-guide.md).
Exits 1 if any file's checksum couldn't be computed (it's left out of the index).
"""
import argparse
import hashlib
import html
import json
import os
import sys
import tempfile

import github_releases
from github_releases import Releases, gh_available

TAG_PREFIX = "pypi/"
PACKAGE_EXTS = (".whl", ".tar.gz", ".zip", ".tar.bz2")


def write_pages(site_dir, packages, title="Python packages"):
    """Write PEP 503 pages. packages: {name: [(file name, url, sha256)]}. Returns (packages, files) written."""
    simple = os.path.join(site_dir, "simple")
    os.makedirs(simple, exist_ok=True)
    open(os.path.join(site_dir, ".nojekyll"), "w").close()  # serve files as-is
    listed, nfiles = [], 0
    for pkg in sorted(packages):
        links = [f'    <a href="{html.escape(url + (f"#sha256={sha}" if sha else ""))}">{html.escape(name)}</a><br/>'
                 for name, url, sha in sorted(packages[pkg])]
        if not links:
            continue
        os.makedirs(os.path.join(simple, pkg), exist_ok=True)
        with open(os.path.join(simple, pkg, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>{html.escape(pkg)}</title></head>\n"
                     f"<body>\n<h1>{html.escape(pkg)}</h1>\n" + "\n".join(links) + "\n</body></html>\n")
        listed.append(pkg)
        nfiles += len(links)
    with open(os.path.join(simple, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title></head>\n<body>\n"
                 + "\n".join(f'    <a href="{p}/">{p}</a><br/>' for p in listed) + "\n</body></html>\n")
    return listed, nfiles


def load_hashes(site_dir):
    path = os.path.join(site_dir, "hashes.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_hashes(site_dir, hashes):
    os.makedirs(site_dir, exist_ok=True)
    with open(os.path.join(site_dir, "hashes.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(sorted(hashes.items())), f, indent=1)
        f.write("\n")


def list_package_tags(repo):
    ok, out = github_releases.gh("release", "list", "-R", repo, "--limit", "10000", "--json", "tagName,isDraft")
    if not ok:
        raise RuntimeError(f"gh release list: {out}")
    return sorted(r["tagName"] for r in json.loads(out) if r["tagName"].startswith(TAG_PREFIX) and not r["isDraft"])


def download_sha256(repo, tag, name):
    """Download one release file with gh and return its SHA-256. Replaced in tests."""
    with tempfile.TemporaryDirectory() as d:
        ok, out = github_releases.gh("release", "download", tag, "-R", repo, "-p", name, "-D", d, "--clobber")
        if not ok:
            raise RuntimeError(out)
        h = hashlib.sha256()
        with open(os.path.join(d, name), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--github-repo", required=True, help="owner/name of the repo holding the pypi/<package> releases")
    ap.add_argument("--site-dir", default="github-index", help="Where to write the pages and hashes.json")
    args = ap.parse_args()
    if not gh_available():
        sys.exit("The GitHub CLI (gh) is not installed: https://cli.github.com/")

    releases = Releases(args.github_repo)
    hashes = load_hashes(args.site_dir)
    seen, packages, failed, downloaded = set(), {}, 0, 0
    tags = list_package_tags(args.github_repo)
    print(f"{len(tags)} package releases in {args.github_repo}", file=sys.stderr)
    for tag in tags:
        pkg = tag[len(TAG_PREFIX):]
        files = []
        for name, asset in sorted((releases.assets(tag) or {}).items()):
            if not name.endswith(PACKAGE_EXTS):
                continue
            key = f"{tag}/{name}"
            seen.add(key)
            entry = hashes.get(key)
            if not entry or entry.get("size") != asset.get("size"):
                try:
                    print(f"  hashing  {key}", file=sys.stderr)
                    entry = {"sha256": download_sha256(args.github_repo, tag, name), "size": asset.get("size")}
                    hashes[key] = entry
                    downloaded += 1
                except Exception as e:
                    print(f"  FAILED   {key}: {str(e)[-200:]}", file=sys.stderr)
                    failed += 1
                    continue
            files.append((name, asset.get("url") or releases.download_url(tag, name), entry["sha256"]))
        packages[pkg] = files

    removed = [k for k in hashes if k not in seen]
    for k in removed:
        del hashes[k]
    save_hashes(args.site_dir, hashes)
    pkgs, nfiles = write_pages(args.site_dir, packages)
    print(f"\nIndex: {len(pkgs)} packages, {nfiles} files written to {os.path.join(args.site_dir, 'simple')}")
    print(f"  {downloaded} new files hashed, {len(removed)} removed files dropped, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
