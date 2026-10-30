#!/usr/bin/env python3
"""
Copy the MIGRATE files of pypi and npm repos from a triage report
(triage_artifactory.py) from Artifactory into an Azure Artifacts feed.
Generic files are skipped: put them in Blob Storage with archive_to_blob.py.

Usage:
  $env:ARTIFACTORY_URL     = "https://yourcompany.jfrog.io/artifactory"
  $env:ARTIFACTORY_TOKEN   = "<access token>"     # or ARTIFACTORY_USER + ARTIFACTORY_PASSWORD
  $env:AZURE_ARTIFACTS_PAT = "<PAT with Packaging (Read & write)>"  # or install artifacts-keyring
  python migrate_to_azure.py triage_files.csv `
      --feed-url https://pkgs.dev.azure.com/<org>/<project>/_packaging/<feed>

  Dry run by default: it shows what would be uploaded. Add --execute to upload.
  Needs twine for pypi files (pip install twine) and npm for npm files.

For each file with action MIGRATE (change with --actions):
  1. Look it up in the feed. Same file and same hash already there -> skip.
     Same file, different hash -> conflict, skip.
  2. Download it from Artifactory and check its SHA-256 against the report.
  3. Upload it: twine for pypi, "npm publish" for npm.

npm dist-tags: each version is published under a temporary tag so the
upload order cannot move "latest" around. Afterwards each package's
dist-tags are copied from Artifactory (or "latest" is set to the highest
stable version if they can't be read). A tag is never moved to an older
version than the feed already has, so re-runs don't undo newer publishes.

Safe to re-run: files already in the feed are skipped, and downloaded files
with the right hash are reused. Every file's outcome goes to --log.

Files with public_status "name-taken" (the name exists on PyPI/npmjs but
belongs to a different project) are skipped: uploading them to a feed with a
public upstream is the setup for dependency confusion. Exceptions:
  - owner_check "ours" (triage --our-accounts): your org controls the public
    name too, so there's no confusion risk. These are migrated.
  - --allow-name-taken PACKAGE ...: migrate these anyway, after deciding to
    (e.g. a heavily used internal package that is being renamed). Block
    external versions for them in the feed's upstream settings.
  - --include-name-taken: migrate all of them.

Edit the "action" column of the CSV to apply owner review decisions before
running. Azure Artifacts never lets a version be re-published, even after it
is deleted, so check the dry run before using --execute.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from collections import Counter, defaultdict

from triage_artifactory import auth_header as artifactory_auth

TEMP_TAG = "artifactory-migration"


def azure_credentials(feed_url):
    pat = os.environ.get("AZURE_ARTIFACTS_PAT")
    if pat:
        return "azure", pat
    try:
        import keyring
        cred = keyring.get_credential(feed_url, None)
    except Exception:
        cred = None
    if cred and cred.password:
        return cred.username, cred.password
    sys.exit("Set AZURE_ARTIFACTS_PAT, or pip install artifacts-keyring and sign in.")


def basic_auth(creds):
    return {"Authorization": "Basic " + b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()}


def file_hash(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of(path):
    return file_hash(path, "sha256")


def get_json(url, headers):
    """GET a JSON document, or None on 404."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def download(base_url, repo, path, dest):
    url = f"{base_url.rstrip('/')}/{repo}/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, headers=artifactory_auth())
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)
    os.replace(tmp, dest)


def semver_key(version):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", version)
    if not m:
        return (-1, 0, 0, 0, ())
    pre = m[4]
    pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(m[1]), int(m[2]), int(m[3]), 0 if pre else 1, pre_key)


# --- PyPI -------------------------------------------------------------------

class PyPI:
    def __init__(self, feed_url, creds):
        self.simple_url = f"{feed_url}/pypi/simple"
        self.upload_url = f"{feed_url}/pypi/upload/"
        self.creds = creds
        self.cache = {}

    def check_tools(self):
        if subprocess.run([sys.executable, "-m", "twine", "--version"], capture_output=True).returncode:
            sys.exit("twine is not installed: pip install twine")

    def existing(self, r):
        """Return the feed's hash for this file ("" if unknown), or None if absent."""
        package = r["package"]
        if package not in self.cache:
            req = urllib.request.Request(f"{self.simple_url}/{package}/", headers=basic_auth(self.creds))
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    html = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                html = ""
            files = {}
            for href, name in re.findall(r'<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>', html):
                m = re.search(r"#sha256=([0-9a-fA-F]{64})", href)
                files[name.strip()] = m.group(1).lower() if m else ""
            self.cache[package] = files
        return self.cache[package].get(r["filename"])

    expected_hash = staticmethod(lambda r: r.get("sha256", "").lower())

    def mark(self, r):
        pass

    def upload(self, r, path):
        env = {**os.environ, "TWINE_USERNAME": self.creds[0], "TWINE_PASSWORD": self.creds[1]}
        proc = subprocess.run(
            [sys.executable, "-m", "twine", "upload", "--non-interactive", "--disable-progress-bar",
             "--repository-url", self.upload_url, path],
            env=env, capture_output=True, text=True,
        )
        self.cache.pop(r["package"], None)
        return proc.returncode == 0, ((proc.stdout + proc.stderr).strip().splitlines() or [""])[-1]

    def finish(self, record, execute):
        pass


# --- npm --------------------------------------------------------------------

class Npm:
    def __init__(self, feed_url, creds, artifactory_url):
        self.registry = f"{feed_url}/npm/registry/"
        self.creds = creds
        self.artifactory_url = artifactory_url.rstrip("/")
        self.cache = {}
        self.done = defaultdict(set)  # (repo, package) -> versions now in the feed
        self.workdir = None

    def check_tools(self):
        self.npm = shutil.which("npm")
        if not self.npm:
            sys.exit("npm is not installed or not on PATH")
        # Run npm from an empty folder with its own config, so a project or
        # user .npmrc (e.g. "@acme:registry=<artifactory>") cannot redirect
        # publishes to a different registry.
        self.workdir = tempfile.mkdtemp(prefix="npm-migrate-")
        host = re.sub(r"^https?:", "", self.registry)
        password = b64encode(self.creds[1].encode()).decode()
        lines = [f"registry={self.registry}"]
        for prefix in (host, host[: -len("registry/")]):
            lines += [f"{prefix}:username={self.creds[0]}", f"{prefix}:_password={password}",
                      f"{prefix}:email=npm@example.com", f"{prefix}:always-auth=true"]
        self.userconfig = os.path.join(self.workdir, "user.npmrc")
        self.globalconfig = os.path.join(self.workdir, "global.npmrc")
        with open(self.userconfig, "w") as f:
            f.write("\n".join(lines) + "\n")
        open(self.globalconfig, "w").close()

    def npm_run(self, package, *args):
        cmd = [self.npm, *args, "--registry", self.registry, "--userconfig", self.userconfig,
               "--globalconfig", self.globalconfig]
        if package.startswith("@"):
            cmd.append(f"--{package.split('/')[0]}:registry={self.registry}")
        proc = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip().splitlines()
        return proc.returncode == 0, next((l for l in reversed(out) if "ERR" in l), out[-1] if out else "")

    def packument(self, package, refresh=False):
        if refresh or package not in self.cache:
            url = self.registry + urllib.parse.quote(package, safe="@")
            self.cache[package] = get_json(url, basic_auth(self.creds)) or {}
        return self.cache[package]

    def existing(self, r):
        version = self.packument(r["package"]).get("versions", {}).get(r["version"])
        return None if version is None else version.get("dist", {}).get("shasum", "")

    expected_hash = staticmethod(lambda r: r.get("sha1", "").lower())

    def mark(self, r):
        """Record a version that is (or in a dry run, would be) in the feed."""
        self.done[(r["repo"], r["package"])].add(r["version"])

    def upload(self, r, path):
        return self.npm_run(r["package"], "publish", os.path.abspath(path), "--tag", TEMP_TAG, "--ignore-scripts")

    def wanted_tags(self, repo, package, ours):
        """dist-tags from Artifactory, limited to versions migrated by us; or
        latest = our highest stable version."""
        try:
            url = f"{self.artifactory_url}/api/npm/{repo}/{urllib.parse.quote(package, safe='@')}"
            tags = (get_json(url, artifactory_auth()) or {}).get("dist-tags", {})
        except Exception:
            tags = {}
        tags = {t: v for t, v in tags.items() if v in ours and t != TEMP_TAG}
        if "latest" not in tags:
            stable = [v for v in ours if semver_key(v)[3] == 1] or list(ours)
            tags["latest"] = max(stable, key=semver_key)
        return tags

    def finish(self, record, execute):
        """Set dist-tags for every npm package touched in this run."""
        for (repo, package), versions in sorted(self.done.items()):
            row = {"repo": repo, "package": package, "filename": ""}
            try:
                current = self.packument(package, refresh=execute).get("dist-tags", {})
                wanted = self.wanted_tags(repo, package, versions)
            except Exception as e:
                record({**row, "version": ""}, "failed-dist-tag", str(e))
                continue
            for tag, version in sorted(wanted.items()):
                have = current.get(tag)
                if have == version or (have and semver_key(have) > semver_key(version)):
                    continue
                r = {**row, "version": version}
                if not execute:
                    record(r, "would-tag", f"{tag} -> {version}")
                    continue
                ok, msg = self.npm_run(package, "dist-tag", "add", f"{package}@{version}", tag)
                record(r, "tagged" if ok else "failed-dist-tag", f"{tag} -> {version}" + ("" if ok else f": {msg}"))
            if execute and TEMP_TAG in current:
                self.npm_run(package, "dist-tag", "rm", package, TEMP_TAG)
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


# ----------------------------------------------------------------------------

def feed_base(url):
    url = url.rstrip("/")
    for suffix in ("/pypi/simple", "/pypi/upload", "/pypi", "/npm/registry", "/npm"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="<out>_files.csv from triage_artifactory.py")
    ap.add_argument("--feed-url", required=True,
                    help="https://pkgs.dev.azure.com/<org>[/<project>]/_packaging/<feed>")
    ap.add_argument("--url", default=os.environ.get("ARTIFACTORY_URL"), help="Artifactory base URL ending in /artifactory")
    ap.add_argument("--actions", nargs="+", default=["MIGRATE"], help="Report actions to migrate")
    ap.add_argument("--packages", nargs="+", help="Only these packages")
    ap.add_argument("--allow-name-taken", nargs="+", default=[], metavar="PACKAGE",
                    help="Migrate these name-taken packages even though the public name isn't ours")
    ap.add_argument("--include-name-taken", action="store_true",
                    help="Also migrate files whose name exists on the public registry with different content")
    ap.add_argument("--download-dir", default="downloads")
    ap.add_argument("--log", default="migrate_log.csv")
    ap.add_argument("--execute", action="store_true", help="Actually download and upload (default: dry run)")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="Only the first N selected files, for a trial run. Re-running with the same "
                         "limit picks the same files; drop it for the full run")
    args = ap.parse_args()
    if not args.url:
        sys.exit("Provide --url or set ARTIFACTORY_URL.")

    feed_url = feed_base(args.feed_url)
    creds = azure_credentials(feed_url)
    artifactory_auth()  # fail early if Artifactory credentials are missing

    with open(args.report, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["action"] in args.actions]
    if args.packages:
        wanted = set(args.packages)
        rows = [r for r in rows if r["package"] in wanted]
    # Reports from before npm support have no repo_type column: all pypi
    for r in rows:
        r.setdefault("repo_type", "pypi")
        r["public_status"] = r.get("public_status", r.get("pypi_status", ""))
    # Oldest npm versions first, so the feed sees them in release order
    rows.sort(key=lambda r: (r["repo_type"] != "npm", r["package"], semver_key(r["version"])))
    if args.limit is not None:
        rows = rows[:args.limit]  # a trial run: the same files every time

    norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()  # pip-style name matching
    allowed = {norm(p) for p in args.allow_name_taken}
    handlers = {"pypi": PyPI(feed_url, creds), "npm": Npm(feed_url, creds, args.url)}
    used = {r["repo_type"] for r in rows} & set(handlers)
    if args.execute:
        for t in used:
            handlers[t].check_tools()
        os.makedirs(args.download_dir, exist_ok=True)
    print(f"{len(rows)} files to process ({'EXECUTE' if args.execute else 'DRY RUN'})", file=sys.stderr)

    counts = Counter()
    with open(args.log, "w", newline="", encoding="utf-8") as logf:
        log = csv.writer(logf)
        log.writerow(["repo", "package", "version", "filename", "status", "detail"])

        def record(r, status, detail=""):
            counts[status] += 1
            log.writerow([r["repo"], r["package"], r["version"], r["filename"], status, detail])
            logf.flush()
            print(f"  {status:18} {r['filename'] or r['package']}  {detail}", file=sys.stderr)

        for r in rows:
            handler = handlers.get(r["repo_type"])
            if handler is None:
                record(r, "skipped-type", f"{r['repo_type']} files go to Blob Storage: use archive_to_blob.py")
                continue
            if (r["public_status"] == "name-taken" and not args.include_name_taken
                    and r.get("owner_check") != "ours" and norm(r["package"]) not in allowed):
                who = f" (owner_check: {r['owner_check']})" if r.get("owner_check") else ""
                record(r, "skipped-name-taken", f"name exists on the public registry{who}; rename, "
                                                f"or use --allow-name-taken {r['package']}")
                continue
            expected = handler.expected_hash(r)

            try:
                have = handler.existing(r)
            except Exception as e:
                record(r, "failed-feed-check", str(e))
                continue
            if have is not None:
                if expected and have and have.lower() != expected:
                    record(r, "conflict", f"feed has a different file (hash {have})")
                else:
                    handler.mark(r)
                    record(r, "already-in-feed")
                continue

            if not args.execute:
                handler.mark(r)
                record(r, "would-upload", f"{int(r.get('size_bytes') or 0) / 1e6:.1f} MB")
                continue

            # One folder per repo keeps same-named files apart. The file name
            # itself must stay as-is: twine uploads under the local name.
            os.makedirs(os.path.join(args.download_dir, r["repo"]), exist_ok=True)
            dest = os.path.join(args.download_dir, r["repo"], r["filename"])
            sha256 = r.get("sha256", "").lower()
            try:
                if not (os.path.exists(dest) and sha256 and sha256_of(dest) == sha256):
                    download(args.url, r["repo"], r["path"], dest)
            except Exception as e:
                record(r, "failed-download", str(e))
                continue
            actual = sha256_of(dest)
            if sha256 and actual != sha256:
                record(r, "hash-mismatch", f"expected {sha256}, got {actual}")
                continue

            ok, msg = handler.upload(r, dest)
            if ok:
                handler.mark(r)
                record(r, "uploaded" if sha256 else "uploaded-unverified", "" if sha256 else "no sha256 in report")
            else:
                record(r, "failed-upload", msg)

        for t in used:
            handlers[t].finish(record, args.execute)

    print("\nSummary:", file=sys.stderr)
    for status, n in sorted(counts.items()):
        print(f"  {status:20} {n}", file=sys.stderr)
    print(f"\nWrote {args.log}", file=sys.stderr)
    failed = sum(n for s, n in counts.items() if s.startswith("failed") or s in ("conflict", "hash-mismatch"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
