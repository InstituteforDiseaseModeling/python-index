"""Tests for migrate_python_to_github.py, migrate_files_to_github.py and migrate_npm_to_github.py,
with a fake Artifactory, a fake `gh` (releases kept in memory) and a fake npm registry."""
import contextlib, csv, hashlib, io, json, os, re, sys, tarfile, tempfile

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, ROOT)
import github_releases
import migrate_files_to_github as mf
import migrate_npm_to_github as mn
import migrate_python_to_github as mp
import build_github_index as bi

WORK = tempfile.mkdtemp(prefix="test-github-")
os.chdir(WORK)
sha = lambda b: hashlib.sha256(b).hexdigest()

# ---------------------------------------------------------------- fake Artifactory
DATA = {}      # (repo, path) -> bytes
LISTING = {}   # repo -> [items]


def add(repo, path, data, sha_override=None, size=None):
    d, n = path.rsplit("/", 1) if "/" in path else (".", path)
    DATA[(repo, path)] = data
    LISTING.setdefault(repo, []).append({"path": d, "name": n, "size": size if size is not None else len(data),
                                         "sha256": sha_override or sha(data)})


def fake_download(base, repo, path, dest):
    with open(dest, "wb") as f:
        f.write(DATA[(repo, path)])


for mod in (mp, mf, mn):
    mod.fetch_files = lambda url, repo: iter(LISTING.get(repo, []))
    mod.download = fake_download

# ---------------------------------------------------------------- fake gh
RELEASES = {}  # tag -> {name: asset}
DRAFTS = set()
GH_CALLS = []


def fake_gh(*args):
    GH_CALLS.append(args)
    if args[:2] == ("release", "view"):
        tag = args[2]
        if tag not in RELEASES:
            return False, "release not found"
        return True, json.dumps({"assets": list(RELEASES[tag].values())})
    if args[:2] == ("release", "list"):
        return True, json.dumps([{"tagName": t, "isDraft": t in DRAFTS} for t in RELEASES])
    if args[:2] == ("release", "create"):
        RELEASES[args[2]] = {}
        return True, ""
    if args[:2] == ("release", "upload"):
        tag, path = args[2], args[3]
        name = os.path.basename(path)
        if name in RELEASES[tag] and "--clobber" not in args:
            return False, "asset already exists"
        RELEASES[tag][name] = {"name": name, "size": os.path.getsize(path),
                               "url": f"https://github.com/o/r/releases/download/{tag}/{name}",
                               "data": open(path, "rb").read().decode("latin-1")}
        return True, ""
    raise AssertionError(args)


github_releases.gh = fake_gh
for mod in (mp, mf):
    mod.gh_available = lambda: True


def run(mod, *argv):
    sys.argv = ["x", "--url", "http://fake/artifactory", *argv]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            mod.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue() + err.getvalue()


def log(path, key):
    return {tuple(r[k] for k in key): r for r in csv.DictReader(open(path, encoding="utf-8"))}


# ================================================================ Python
add("idm-pypi-production", "acme-core/1.0/acme_core-1.0-py3-none-any.whl", b"wheel 1.0")
add("idm-pypi-production", "acme-core/1.0/acme_core-1.0.tar.gz", b"sdist 1.0")
add("idm-pypi-production", "acme-core/2.0/acme_core-2.0-py3-none-any.whl", b"wheel 2.0")
add("idm-pypi-production", "bad/1.0/bad-1.0.tar.gz", b"corrupt", sha_override="0" * 64)
add("idm-pypi-production", "skipme/1.0/skipme-1.0.tar.gz", b"x")
add("idm-pypi-production", ".pypi/simple.html", b"<html>")             # not a package file
RELEASES["pypi/acme-core"] = {"acme_core-1.0.tar.gz": {"name": "acme_core-1.0.tar.gz", "size": 9,
                              "url": "https://github.com/o/r/releases/download/pypi/acme-core/acme_core-1.0.tar.gz"}}

PY = ["--github-repo", "o/r", "--exclude", "SkipMe", "--log", "py.csv", "--site-dir", "site"]
code, out = run(mp, *PY)                                            # dry run
st = {k[0]: r["status"] for k, r in log("py.csv", ["filename"]).items()}
assert st == {"acme_core-1.0-py3-none-any.whl": "would-upload", "acme_core-1.0.tar.gz": "already-on-github",
              "acme_core-2.0-py3-none-any.whl": "would-upload", "bad-1.0.tar.gz": "would-upload"}, st
assert not os.path.exists("site") and code == 0
assert not any(c[:2] in (("release", "create"), ("release", "upload")) for c in GH_CALLS)

code, out = run(mp, *PY, "--limit", "1")                            # trial: only one upload planned
assert sum(1 for r in log("py.csv", ["filename"]).values() if r["status"] == "would-upload") == 1

code, out = run(mp, *PY, "--execute")
st = {k[0]: r["status"] for k, r in log("py.csv", ["filename"]).items()}
assert st["acme_core-1.0-py3-none-any.whl"] == st["acme_core-2.0-py3-none-any.whl"] == "uploaded", st
assert st["bad-1.0.tar.gz"] == "hash-mismatch" and st["acme_core-1.0.tar.gz"] == "already-on-github"
assert "pypi/bad" not in RELEASES                                    # nothing created for a bad file
assert set(RELEASES["pypi/acme-core"]) == {"acme_core-1.0.tar.gz", "acme_core-1.0-py3-none-any.whl",
                                           "acme_core-2.0-py3-none-any.whl"}
assert code == 1                                                    # hash mismatch
root = open("site/simple/index.html", encoding="utf-8").read()
assert 'href="acme-core/"' in root and "bad" not in root and "skipme" not in root
page = open("site/simple/acme-core/index.html", encoding="utf-8").read()
assert page.count("<a href=") == 3
assert ("https://github.com/o/r/releases/download/pypi/acme-core/acme_core-2.0-py3-none-any.whl#sha256="
        + sha(b"wheel 2.0")) in page
assert os.path.exists("site/.nojekyll")
assert not os.listdir(os.path.join("downloads", "idm-pypi-production")) or \
    os.listdir(os.path.join("downloads", "idm-pypi-production")) == ["bad-1.0.tar.gz"]  # uploaded files removed

uploads_before = sum(1 for c in GH_CALLS if c[:2] == ("release", "upload"))
code, out = run(mp, *PY, "--execute")                               # re-run: nothing new
assert sum(1 for c in GH_CALLS if c[:2] == ("release", "upload")) == uploads_before
code, out = run(mp, *PY, "--index-only")
assert "Index: 1 packages, 3 files" in out, out

# ================================================================ plain files
add("idm-data", "models/v1/a.bin", b"model a")
add("idm-data", "models/b.bin", b"model b")
add("idm-data", "README.txt", b"readme")
add("idm-data", "big/huge.iso", b"tiny-in-test", size=3 * 1024 ** 3)  # reported as 3 GB
FILES = ["--github-repo", "o/r", "--log", "files.csv"]
code, out = run(mf, *FILES)
st = {k[0]: r["status"] for k, r in log("files.csv", ["path"]).items()}
assert st == {"models/v1/a.bin": "would-upload", "models/b.bin": "would-upload", "README.txt": "would-upload",
              "big/huge.iso": "too-large"}, st
assert "files/models" not in RELEASES

code, out = run(mf, *FILES, "--execute")
assert set(RELEASES["files/models"]) == {"v1__a.bin", "b.bin", "MANIFEST.csv"}
assert set(RELEASES["files/root"]) == {"README.txt", "MANIFEST.csv"}
manifest = RELEASES["files/models"]["MANIFEST.csv"]["data"]
assert "models/v1/a.bin,v1__a.bin,7," + sha(b"model a") in manifest, manifest
assert "files/big" not in RELEASES
uploads_before = sum(1 for c in GH_CALLS if c[:2] == ("release", "upload"))
code, out = run(mf, *FILES, "--execute")                            # re-run: nothing re-uploaded
assert sum(1 for c in GH_CALLS if c[:2] == ("release", "upload")) == uploads_before
assert all(r["status"] in ("already-on-github", "too-large") for r in log("files.csv", ["path"]).values())

# ================================================================ npm
def npm_tgz(name, version):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fn, text in (("package/package.json", json.dumps({"name": name, "version": version,
                                                              "publishConfig": {"registry": "https://old"}})),
                         ("package/index.js", "module.exports = 1;")):
            b = text.encode()
            info = tarfile.TarInfo(fn)
            info.size = len(b)
            tar.addfile(info, io.BytesIO(b))
    return buf.getvalue()


add("idm-npm-production", "widget/-/widget-1.1.0.tgz", npm_tgz("widget", "1.1.0"))
add("idm-npm-production", "widget/-/widget-1.0.0.tgz", npm_tgz("widget", "1.0.0"))
add("idm-npm-production", "widget/-/widget-2.0.0-beta.1.tgz", npm_tgz("widget", "2.0.0-beta.1"))
add("idm-npm-production", "@acme/ui/-/@acme/ui-1.0.0.tgz", npm_tgz("@acme/ui", "1.0.0"))
add("idm-npm-production", ".npm/widget/package.json", b"{}")

REGISTRY = {"@idm/widget": {"1.0.0": {}}}   # already has 1.0.0
TAGS = {}
NPM_CALLS = []


def fake_npm(*args, userconfig):
    NPM_CALLS.append(args)
    assert "_authToken=tok" in open(userconfig).read()
    if args[0] == "view":
        name, ver = args[1].rsplit("@", 1)
        return (ver in REGISTRY.get(name, {})), ver
    if args[0] == "publish":
        with tarfile.open(args[1], "r:gz") as t:
            meta = json.loads(t.extractfile("package/package.json").read())
            assert t.extractfile("package/index.js").read() == b"module.exports = 1;"
        assert "publishConfig" not in meta
        REGISTRY.setdefault(meta["name"], {})[meta["version"]] = meta
        TAGS.setdefault(meta["name"], {})[args[args.index("--tag") + 1]] = meta["version"]
        return True, ""
    if args[0] == "dist-tag":
        if args[1] == "add":
            name, ver = args[2].rsplit("@", 1)
            TAGS.setdefault(name, {})[args[3]] = ver
        else:
            TAGS.get(args[2], {}).pop(args[3], None)
        return True, ""
    raise AssertionError(args)


mn.npm = fake_npm
mn.github_token = lambda: "tok"
NPM = ["--scope", "@IDM", "--log", "npm.csv"]
code, out = run(mn, *NPM)
st = {(k[0], k[1]): r["status"] for k, r in log("npm.csv", ["new_name", "version"]).items()}
assert st == {("@idm/widget", "1.0.0"): "already-on-github", ("@idm/widget", "1.1.0"): "would-publish",
              ("@idm/widget", "2.0.0-beta.1"): "would-publish", ("@idm/ui", "1.0.0"): "would-publish"}, st
assert not any(c[0] == "publish" for c in NPM_CALLS)

code, out = run(mn, *NPM, "--execute")
assert code == 0, out
published = [c[1] for c in NPM_CALLS if c[0] == "publish"]
assert [re.search(r"-(\d[^/\\]*)\.tgz$", p)[1] for p in published if "widget" in p] == ["1.1.0", "2.0.0-beta.1"]
assert REGISTRY["@idm/ui"]["1.0.0"]["name"] == "@idm/ui"            # renamed from @acme/ui
assert TAGS["@idm/widget"] == {"latest": "1.1.0"}, TAGS               # stable, not the beta; temp tag removed
assert TAGS["@idm/ui"] == {"latest": "1.0.0"}
# ================================================================ build_github_index (no Artifactory)
bi.gh_available = lambda: True
hashes = json.load(open("site/hashes.json"))
assert set(hashes) == {"pypi/acme-core/acme_core-1.0-py3-none-any.whl", "pypi/acme-core/acme_core-1.0.tar.gz",
                       "pypi/acme-core/acme_core-2.0-py3-none-any.whl"}, hashes      # seeded by the migration
assert hashes["pypi/acme-core/acme_core-2.0-py3-none-any.whl"]["sha256"] == sha(b"wheel 2.0")

def release_file(tag, name, data):
    RELEASES.setdefault(tag, {})[name] = {"name": name, "size": len(data),
                                          "url": f"https://github.com/o/r/releases/download/{tag}/{name}", "data": data.decode("latin-1")}

release_file("pypi/acme-core", "acme_core-3.0-py3-none-any.whl", b"wheel 3.0")    # published straight to GitHub
release_file("pypi/newpkg", "newpkg-0.1.tar.gz", b"new package")                    # brand-new package
release_file("pypi/newpkg", "notes.txt", b"not a package file")                     # ignored
release_file("pypi/draftpkg", "draftpkg-1.0.tar.gz", b"draft")
DRAFTS.add("pypi/draftpkg")                                                         # drafts are skipped
del RELEASES["pypi/acme-core"]["acme_core-1.0.tar.gz"]                              # removed from GitHub

def run_bi(*argv):
    """build_github_index.py takes no Artifactory --url."""
    sys.argv = ["x", *argv]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            bi.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue() + err.getvalue()

HASHED = []
def fake_download_sha256(repo, tag, name):
    HASHED.append(f"{tag}/{name}")
    if name == "broken-1.0.tar.gz":
        raise RuntimeError("download failed")
    return sha(RELEASES[tag][name]["data"].encode("latin-1"))
bi.download_sha256 = fake_download_sha256

code, out = run_bi("--github-repo", "o/r", "--site-dir", "site")
assert code == 0, out
assert sorted(HASHED) == ["pypi/acme-core/acme_core-3.0-py3-none-any.whl", "pypi/newpkg/newpkg-0.1.tar.gz"], HASHED
page = open("site/simple/acme-core/index.html", encoding="utf-8").read()
assert "acme_core-3.0-py3-none-any.whl#sha256=" + sha(b"wheel 3.0") in page
assert "acme_core-1.0.tar.gz" not in page and page.count("<a href=") == 3
root = open("site/simple/index.html", encoding="utf-8").read()
assert 'href="newpkg/"' in root and "draftpkg" not in root
assert "notes.txt" not in open("site/simple/newpkg/index.html", encoding="utf-8").read()
hashes = json.load(open("site/hashes.json"))
assert "pypi/acme-core/acme_core-1.0.tar.gz" not in hashes and "pypi/newpkg/newpkg-0.1.tar.gz" in hashes

HASHED.clear()
code, out = run_bi("--github-repo", "o/r", "--site-dir", "site")                  # nothing new: no downloads
assert HASHED == [] and code == 0

release_file("pypi/newpkg", "broken-1.0.tar.gz", b"x")
code, out = run_bi("--github-repo", "o/r", "--site-dir", "site")
assert code == 1 and "broken-1.0.tar.gz" not in open("site/simple/newpkg/index.html", encoding="utf-8").read()
print("ALL OK")
