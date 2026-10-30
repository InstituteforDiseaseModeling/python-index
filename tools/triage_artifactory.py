#!/usr/bin/env python3
"""
Triage files in Artifactory local repos (PyPI, npm, generic) into
MIGRATE / ARCHIVE / DROP lists based on download statistics.

Usage:
  $env:ARTIFACTORY_URL   = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN = "<access token>"     # or ARTIFACTORY_USER + ARTIFACTORY_PASSWORD
  python triage_artifactory.py --repos pypi-local npm-local files-local `
      --active-days 365 --drop-after-days 1095 --out triage

Each repo's type (pypi, npm, generic) is read from Artifactory. If your token
cannot read repo configuration, give it as repo=type, e.g. files-local=generic.

Rules (per file):
  - downloaded or uploaded within --active-days            -> MIGRATE
  - otherwise, last activity older than --drop-after-days  -> DROP
  - otherwise                                              -> ARCHIVE
"Last activity" = last download, or upload date if never downloaded.

With --keep-latest N, pypi and npm files are triaged by version instead,
ignoring download stats (use this when bulk jobs such as scanners, mirrors
or backups have touched every file and made the stats meaningless):
  - one of the N newest stable versions of its major       -> MIGRATE
    version (so with N=3, a package with 1.x and 2.x lines keeps
    three of each; a package with no stable release keeps its N
    newest versions)
  - downloaded within --used-days (default 90), and the    -> MIGRATE
    download was not part of a burst (--burst-files or more
    files downloaded within 5 minutes; a crawler, mirror or
    test script, not someone installing). Bursts are listed
    when the script runs, and the burst_download column marks
    the files they touched.
  - uploaded within --active-days                          -> MIGRATE
  Both version rules cover every file of a version (wheels and sdist).
  - otherwise                                              -> ARCHIVE
  Nothing is marked DROP this way. Generic repos have no versions, so they
  still use the download-based rules.
The reason column says which rule decided each file.

--archive PACKAGE ... forces every file of those packages to ARCHIVE,
overriding all rules (reason "--archive"). Use it for packages that must not
enter the new feed, e.g. an unused internal package whose name belongs to
someone else on PyPI. Names match the way pip matches them (dtk_api = dtk-api).

What "package" means per repo type:
  pypi     project name from the wheel/sdist filename (normalized)
  npm      package name from the path, including any @scope
  generic  top-level folder, so owners can review a folder as a unit

With --check-public, pypi packages are looked up on pypi.org and npm packages
on registry.npmjs.org (generic files are not checked):
  - a MIGRATE file published there with the same hash     -> USE_PUBLIC
  - public_status column: "match" (same file is public), "name-taken" (a
    public package has this name but not this file; dependency-confusion
    risk, review it), "no" (not public), "error" (lookup failed).
  This sends every package name in the scanned repos to those registries, so
  skip it if internal package names are themselves sensitive.

With --check-owners (implies --check-public), names that exist publicly also
get the accounts that can publish them there: PyPI project owners and
maintainers, or npm maintainers (public_owners column). Add --our-accounts to
classify each one (owner_check column):
  ours         every account that can publish it is one of ours
  partly-ours  some are ours, some aren't: an outside account can publish it
  not-ours     none are ours: someone else controls the name. If you migrate
               it internally, a pip/npm client that also checks the public
               registry could install their package instead of yours.
  unknown / error  no accounts listed / lookup failed
The run prints every account it found, so you can build the --our-accounts
list from a first run. Put the accounts in a file, one per line, and pass
--our-accounts @accounts.txt.

Outputs:
  <out>_files.csv    one row per file with the recommended action
  <out>_packages.csv one row per package with a rollup (package is MIGRATE
                     if any of its files is MIGRATE, so owners review it whole)

Notes:
  - Download stats may be missing for files that were migrated between
    Artifactory instances or had stats reset. Treat DROP as a recommendation
    and have owners review it before deleting anything.
  - Remote (proxy) and virtual repos are skipped; only local repos hold
    files that need moving.
"""
import argparse
import base64
import bisect
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO_TYPES = ("pypi", "npm", "generic")


def auth_header():
    token = os.environ.get("ARTIFACTORY_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = os.environ.get("ARTIFACTORY_USER")
    pw = os.environ.get("ARTIFACTORY_PASSWORD")
    if user and pw:
        raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {raw}"}
    sys.exit("Set ARTIFACTORY_TOKEN, or ARTIFACTORY_USER and ARTIFACTORY_PASSWORD.")


def aql(base_url, query):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/search/aql",
        data=query.encode(),
        headers={**auth_header(), "Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["results"]


def repo_info(base_url, repo):
    """Return (package_type, rclass) for a repo, e.g. ("npm", "local")."""
    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/repositories/{repo}", headers=auth_header())
    with urllib.request.urlopen(req, timeout=60) as resp:
        info = json.load(resp)
    return info.get("packageType", "").lower(), info.get("rclass", "").lower()


def parse_dt(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pypi(path, filename):
    """Return (normalized_name, version) from a wheel or sdist filename."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        if len(parts) >= 2:
            return normalize(parts[0]), parts[1]
    for ext in (".tar.gz", ".zip", ".tar.bz2", ".tgz"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            # Old "bdist_dumb" builds put the platform after the version:
            # name-1.0.win-amd64.zip, name-1.0.linux-x86_64.tar.gz
            stem = re.sub(r"\.(win|linux|macosx|cygwin)[-_.].*$", "", stem)
            if "-" not in stem:
                return None, None
            name, version = stem.rsplit("-", 1)
            if not version[:1].isdigit():
                # Custom version with dashes (name-0.5.4-local-fix): split at the first -<digit>
                m = re.match(r"^(.+?)-(\d.*)$", stem)
                if m:
                    name, version = m[1], m[2]
            return normalize(name), version
    return None, None


def parse_npm(path, filename):
    """Return (package, version) for an npm tarball. Artifactory stores them as
    <name>/-/<name>-<version>.tgz, or @scope/<name>/-/@scope/<name>-<version>.tgz."""
    if not filename.endswith(".tgz"):
        return None, None
    m = re.match(r"^(.*?)/-(?:/|$)", path + "/")
    if not m or m[1].startswith("."):
        return None, None  # .npm metadata and other non-tarball files
    package = m[1]
    base = package.rsplit("/", 1)[-1]
    if not filename.startswith(base + "-"):
        return None, None
    return package, filename[len(base) + 1:-4]


def parse_generic(path, filename):
    """Every file counts; group by top-level folder."""
    return (path.split("/")[0] if path not in ("", ".") else "(root)"), ""


PARSERS = {"pypi": parse_pypi, "npm": parse_npm, "generic": parse_generic}

try:
    from packaging.version import InvalidVersion, Version
except ImportError:
    Version = None


def version_key(version):
    """Return (key, is_stable) for a version string. The key sorts versions and
    is equal for equivalent spellings (1.0 and 1.0.0). Versions that fail to
    parse sort below every parsed one and count as unstable."""
    if Version:
        try:
            v = Version(version)
        except InvalidVersion:
            return (0, version), False
        return (1, v), not v.is_prerelease
    # No packaging library: compare leading numbers, treat any suffix as pre-release
    m = re.match(r"^v?(\d+(?:\.\d+)*)(.*)$", version)
    if not m:
        return (0, version), False
    nums = tuple(int(n) for n in m[1].split("."))
    nums = nums + (0,) * (10 - len(nums))
    stable = not m[2] or m[2].startswith("+")
    # A release sorts above its own pre-releases; pre-releases by their suffix (dev2 > dev1)
    suffix = tuple((1, int(p)) if p.isdigit() else (0, p) for p in re.findall(r"\d+|[A-Za-z]+", m[2]))
    return (1, (nums, stable, suffix)), stable


def major(key):
    """Major version number from a version_key key, or None if unparsed."""
    if key[0] == 0:
        return None
    return key[1].release[0] if Version else key[1][0][0]


def latest_versions(versions, n):
    """Keys (see version_key) of the n newest stable versions of each major
    version, so maintained older lines (1.7.x next to 2.x) are kept too. If the
    package has no stable release, the n newest versions of any kind."""
    keys = dict(version_key(v) for v in versions)
    stable = [k for k, is_stable in keys.items() if is_stable]
    if not stable:
        return set(sorted(keys, reverse=True)[:n])
    by_major = defaultdict(list)
    for k in stable:
        by_major[major(k)].append(k)
    return {k for ks in by_major.values() for k in sorted(ks, reverse=True)[:n]}


def fetch_files(base_url, repo, page_size=5000):
    offset = 0
    while True:
        query = (
            f'items.find({{"repo":"{repo}","type":"file"}})'
            f'.include("repo","path","name","size","created","sha256","actual_sha1","stat")'
            f'.sort({{"$asc":["path","name"]}})'
            f".offset({offset}).limit({page_size})"
        )
        batch = aql(base_url, query)
        if not batch:
            return
        yield from batch
        # Advance by what was actually returned: the server may cap results
        # below page_size (e.g. for non-admin users), and skipping ahead by
        # page_size would silently miss files.
        offset += len(batch)


def public_hashes(repo_type, name, cache={}):
    """Return the set of file hashes the public registry has for a package
    (sha256 for PyPI, sha1 for npm), or None if it does not exist there.
    Raises on other errors."""
    key = (repo_type, name)
    if key not in cache:
        if repo_type == "pypi":
            url = f"https://pypi.org/pypi/{name}/json"
        else:
            url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "triage-artifactory"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            cache[key] = None
        else:
            if repo_type == "pypi":
                cache[key] = {
                    f["digests"]["sha256"]
                    for files in data.get("releases", {}).values()
                    for f in files
                    if f.get("digests", {}).get("sha256")
                }
            else:
                cache[key] = {
                    v["dist"]["shasum"]
                    for v in data.get("versions", {}).values()
                    if v.get("dist", {}).get("shasum")
                }
    return cache[key]


def public_status(repo_type, name, sha256, sha1):
    if repo_type not in ("pypi", "npm"):
        return ""
    try:
        hashes = public_hashes(repo_type, name)
    except Exception as e:
        print(f"  Public registry lookup failed for {name}: {e}", file=sys.stderr)
        return "error"
    if hashes is None:
        return "no"
    digest = sha256 if repo_type == "pypi" else sha1
    return "match" if digest and digest in hashes else "name-taken"


def public_owners(repo_type, name, cache={}):
    """Return [(role, account)] for the accounts that can publish a package on
    the public registry. PyPI: the project's Owner/Maintainer roles (from its
    XML-RPC API; the JSON API only has self-declared author names). npm: the
    package's maintainers. Raises on lookup errors."""
    key = (repo_type, name)
    if key not in cache:
        if repo_type == "pypi":
            import xmlrpc.client
            roles = xmlrpc.client.ServerProxy("https://pypi.org/pypi").package_roles(name)
            cache[key] = [(role, user) for role, user in roles]
        else:
            req = urllib.request.Request(
                f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}",
                headers={"Accept": "application/json", "User-Agent": "triage-artifactory"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            cache[key] = [("Maintainer", m["name"]) for m in data.get("maintainers", []) if m.get("name")]
    return cache[key]


def owner_check(owners, ours):
    """Classify a package's public accounts against our own account names."""
    if not ours:
        return ""
    accounts = {user.lower() for _, user in owners}
    mine = accounts & ours
    if not accounts:
        return "unknown"
    if mine == accounts:
        return "ours"
    return "partly-ours" if mine else "not-ours"


BURST_WINDOW = timedelta(minutes=5)


def burst_detector(times, min_files):
    """Return in_burst(t): True if at least min_files of the given download
    times (t's own included) fall within BURST_WINDOW of t. A pip install pulls
    a handful of files at once; a crawler, mirror or test script pulls hundreds."""
    stamps = sorted(t.timestamp() for t in times if t)
    w = BURST_WINDOW.total_seconds()

    def in_burst(t):
        s = t.timestamp()
        return bisect.bisect_right(stamps, s + w) - bisect.bisect_left(stamps, s - w) >= min_files
    return in_burst


def report_bursts(times):
    """Print each burst (downloads less than BURST_WINDOW apart) with its size."""
    groups = []
    for t in sorted(times):
        if groups and t - groups[-1][-1] <= BURST_WINDOW:
            groups[-1].append(t)
        else:
            groups.append([t])
    if groups:
        print("\nIgnoring last-download dates from these bursts:", file=sys.stderr)
    for g in groups:
        print(f"  {g[0]:%Y-%m-%d %H:%M} - {g[-1]:%H:%M} UTC  {len(g):5} files", file=sys.stderr)


def resolve_repos(base_url, specs):
    """Turn "repo" / "repo=type" args into [(repo, type)], skipping non-local repos."""
    repos = []
    for spec in specs:
        repo, _, repo_type = spec.partition("=")
        if not repo_type:
            try:
                repo_type, rclass = repo_info(base_url, repo)
            except Exception as e:
                sys.exit(f"Could not read the type of repo {repo} ({e}). Give it as {repo}=<type>, "
                         f"type one of {', '.join(REPO_TYPES)}.")
            if rclass and rclass != "local":
                print(f"Skipping {repo}: it is a {rclass} repo, not local", file=sys.stderr)
                continue
        repo_type = repo_type.lower()
        if repo_type not in REPO_TYPES:
            sys.exit(f"Repo {repo} has type {repo_type}; supported: {', '.join(REPO_TYPES)}.")
        repos.append((repo, repo_type))
    return repos


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 fromfile_prefix_chars="@")
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--repos", nargs="+", required=True, help="Local repo keys, optionally as repo=type")
    ap.add_argument("--active-days", type=int, default=365)
    ap.add_argument("--drop-after-days", type=int, default=1095)
    ap.add_argument("--out", default="triage")
    ap.add_argument("--check-public", "--check-pypi", dest="check_public", action="store_true",
                    help="Look up pypi/npm packages on the public registries and mark files already published there")
    ap.add_argument("--keep-latest", type=int, metavar="N",
                    help="Triage pypi/npm files by version instead of download stats: migrate the N "
                         "newest stable versions of each major version plus recent uploads, archive the rest")
    ap.add_argument("--used-days", type=int, default=90,
                    help="With --keep-latest, also migrate files downloaded within this many days, "
                         "unless the download was part of a burst (default 90)")
    ap.add_argument("--burst-files", type=int, default=30,
                    help=f"A download is part of a burst when at least this many files were downloaded "
                         f"within {BURST_WINDOW.seconds // 60} minutes of it (default 30)")
    ap.add_argument("--check-owners", action="store_true",
                    help="For package names that exist publicly, list the accounts that can publish them "
                         "(implies --check-public)")
    ap.add_argument("--our-accounts", nargs="+", default=[], metavar="ACCOUNT",
                    help="Your org's PyPI/npm account names, to mark each public package as ours, "
                         "partly-ours or not-ours. Use @file to read them from a file, one per line")
    ap.add_argument("--archive", nargs="+", default=[], metavar="PACKAGE",
                    help="Archive every file of these packages, whatever the rules say (e.g. a name that "
                         "belongs to someone else on the public registry). Use @file for a list")
    args = ap.parse_args()
    if args.check_owners or args.our_accounts:
        args.check_owners = args.check_public = True
    if args.keep_latest is not None and args.keep_latest < 1:
        sys.exit("--keep-latest must be 1 or more.")
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")
    # Fail now rather than after the scan if an output is locked (e.g. open in Excel).
    # Opening for append leaves existing content alone.
    for path in (f"{args.out}_files.csv", f"{args.out}_packages.csv"):
        try:
            open(path, "a").close()
        except OSError as e:
            sys.exit(f"Cannot write {path} ({e.strerror}). Close it if it is open in Excel, then run again.")

    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(days=args.active_days)
    drop_cutoff = now - timedelta(days=args.drop_after_days)

    rows = []
    for repo, repo_type in resolve_repos(args.url, args.repos):
        print(f"Scanning {repo} ({repo_type}) ...", file=sys.stderr)
        parse = PARSERS[repo_type]
        by_version = args.keep_latest is not None and repo_type in ("pypi", "npm")
        repo_rows = []
        for item in fetch_files(args.url, repo):
            path = item["path"]
            name, version = parse(path, item["name"])
            if name is None:
                continue  # not a package file (index files, metadata, etc.)
            stats = item.get("stats") or []
            downloads = sum(s.get("downloads", 0) for s in stats)
            downloaded_dates = [parse_dt(s.get("downloaded")) for s in stats if s.get("downloaded")]
            last_download = max(downloaded_dates) if downloaded_dates else None
            created = parse_dt(item.get("created"))
            last_activity = last_download or created

            if by_version:
                action, reason = None, None  # decided below, once all versions are known
            elif last_download and last_download >= active_cutoff:
                action, reason = "MIGRATE", "recent-download"
            elif last_activity and last_activity >= active_cutoff:
                # Uploaded recently and not pulled yet
                action, reason = "MIGRATE", "recent-upload"
            elif last_activity and last_activity < drop_cutoff:
                action, reason = "DROP", "inactive"
            else:
                action, reason = "ARCHIVE", "inactive"

            repo_rows.append({
                "repo": repo,
                "repo_type": repo_type,
                "package": name,
                "version": version,
                "filename": item["name"],
                "path": item["name"] if path in ("", ".") else f'{path}/{item["name"]}',
                "size_bytes": item.get("size", 0),
                "sha256": item.get("sha256", ""),
                "sha1": item.get("actual_sha1", ""),
                "created": created.isoformat() if created else "",
                "last_download": last_download.isoformat() if last_download else "",
                "download_count": downloads,
                "public_status": "",
                "action": action,
                "reason": reason,
                "_created": created,
                "_last_download": last_download,
                "_by_version": by_version,
            })
        rows.extend(repo_rows)

    # Bursts are found across all repos: one job often crawls several
    in_burst = burst_detector([r["_last_download"] for r in rows], args.burst_files)
    for r in rows:
        r["burst_download"] = "yes" if r["_last_download"] and in_burst(r["_last_download"]) else ""
    if args.keep_latest is not None:
        report_bursts([r["_last_download"] for r in rows if r["burst_download"] and r["_by_version"]])

    used_cutoff = now - timedelta(days=args.used_days)
    versions = defaultdict(set)
    for r in rows:
        if r["_by_version"]:
            versions[(r["repo"], r["package"])].add(r["version"])
    keep = {pkg: latest_versions(vs, args.keep_latest) for pkg, vs in versions.items()}
    # A real download of any file keeps the whole version (wheels and sdist together)
    used = {(r["repo"], r["package"], version_key(r["version"])[0]) for r in rows
            if r["_by_version"] and r["_last_download"] and r["_last_download"] >= used_cutoff
            and not r["burst_download"]}
    forced_archive, forced_found = {normalize(p) for p in args.archive}, set()
    ours = {a.strip().lower() for a in args.our_accounts if a.strip()}
    accounts_seen, owner_errors = defaultdict(set), set()
    for r in rows:
        if r["_by_version"]:
            vkey = version_key(r["version"])[0]
            if vkey in keep[(r["repo"], r["package"])]:
                r["action"], r["reason"] = "MIGRATE", f"latest-{args.keep_latest}"
            elif (r["repo"], r["package"], vkey) in used:
                r["action"], r["reason"] = "MIGRATE", "recent-download"
            elif r["_created"] and r["_created"] >= active_cutoff:
                r["action"], r["reason"] = "MIGRATE", "recent-upload"
            else:
                r["action"], r["reason"] = "ARCHIVE", "older-version"
        for key in ("_created", "_last_download", "_by_version"):
            del r[key]
        if args.check_public:
            r["public_status"] = public_status(r["repo_type"], r["package"], r["sha256"], r["sha1"])
            if r["public_status"] == "match" and r["action"] == "MIGRATE":
                r["action"], r["reason"] = "USE_PUBLIC", "same file on public registry"
        if normalize(r["package"]) in forced_archive:
            r["action"], r["reason"] = "ARCHIVE", "--archive"
            forced_found.add(normalize(r["package"]))
        r["public_owners"], r["owner_check"] = "", ""
        if args.check_owners and r["public_status"] in ("match", "name-taken"):
            try:
                owners = public_owners(r["repo_type"], r["package"])
            except Exception as e:
                if (r["repo_type"], r["package"]) not in owner_errors:
                    print(f"  Owner lookup failed for {r['package']}: {e}", file=sys.stderr)
                owner_errors.add((r["repo_type"], r["package"]))
                r["owner_check"] = "error"
            else:
                r["public_owners"] = "; ".join(f"{role}:{user}" for role, user in owners)
                r["owner_check"] = owner_check(owners, ours)
                for _, user in owners:
                    accounts_seen[user.lower()].add(r["package"])  # account names ignore case

    for name in sorted(forced_archive - forced_found):
        print(f"  Warning: --archive {name} matched no package in the scanned repos", file=sys.stderr)

    # Per-file report
    with open(f"{args.out}_files.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)

    # Per-package rollup
    pkgs = defaultdict(list)
    for r in rows:
        pkgs[(r["repo"], r["repo_type"], r["package"])].append(r)
    priority = {"MIGRATE": 0, "USE_PUBLIC": 1, "ARCHIVE": 2, "DROP": 3}
    # Worst-case public status wins so "name-taken" is never hidden by a match
    public_priority = {"name-taken": 0, "error": 1, "match": 2, "no": 3, "": 4}
    with open(f"{args.out}_packages.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "repo_type", "package", "versions", "files", "total_mb", "total_downloads",
                    "last_download", "package_action", "public_status", "public_owners", "owner_check",
                    "files_migrate", "files_use_public", "files_archive", "files_drop"])
        for (repo, repo_type, pkg), items in sorted(pkgs.items()):
            counts = defaultdict(int)
            for i in items:
                counts[i["action"]] += 1
            overall = min((i["action"] for i in items), key=lambda a: priority[a])
            public = min((i["public_status"] for i in items), key=lambda s: public_priority[s])
            last_dl = max((i["last_download"] for i in items if i["last_download"]), default="")
            # Owners are per package, so any file that has them will do
            owned = next((i for i in items if i["owner_check"]), items[0])
            w.writerow([
                repo, repo_type, pkg,
                len({i["version"] for i in items if i["version"]}), len(items),
                round(sum(i["size_bytes"] for i in items) / 1e6, 1),
                sum(i["download_count"] for i in items),
                last_dl, overall, public, owned["public_owners"], owned["owner_check"],
                counts["MIGRATE"], counts["USE_PUBLIC"], counts["ARCHIVE"], counts["DROP"],
            ])

    # Summary
    total = defaultdict(lambda: [0, 0])
    for r in rows:
        total[r["action"]][0] += 1
        total[r["action"]][1] += r["size_bytes"]
    print(f"\n{len(rows)} files across {len(pkgs)} packages")
    for action in ("MIGRATE", "USE_PUBLIC", "ARCHIVE", "DROP"):
        n, size = total[action]
        print(f"  {action:10} {n:7} files  {size / 1e9:8.2f} GB")
    if args.check_owners:
        checks = defaultdict(int)
        for (repo, repo_type, pkg), items in pkgs.items():
            check = next((i["owner_check"] for i in items if i["owner_check"]), "")
            if check:
                checks[check] += 1
        if ours:
            print("\nPackages whose name exists publicly, by who can publish them there:")
            for check in ("ours", "partly-ours", "not-ours", "unknown", "error"):
                if checks[check]:
                    print(f"  {check:12} {checks[check]:5} packages")
        if accounts_seen:
            print("\nAccounts that can publish these names on the public registries"
                  + (" (* = in --our-accounts):" if ours else ":"))
            for user, names in sorted(accounts_seen.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())):
                mark = "*" if user in ours else " "
                print(f"  {mark} {user:30} {len(names):4} packages  e.g. {', '.join(sorted(names)[:3])}")
    print(f"\nWrote {args.out}_files.csv and {args.out}_packages.csv")


if __name__ == "__main__":
    main()
