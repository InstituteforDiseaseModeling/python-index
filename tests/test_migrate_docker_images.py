"""Test migrate_docker_images.py against a fake Artifactory and a fake target registry."""
import contextlib, csv, io, os, sys, tempfile

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, ROOT)
import migrate_docker_images as m


def tag(path, digest, layers):
    files = [{"path": path, "name": "manifest.json", "sha256": digest, "size": 1}]
    files += [{"path": path, "name": f"sha256__{h}", "sha256": h, "size": size} for h, size in layers]
    return files


REPOS = {
    "idm-docker-production": (
        tag("app/a/1.0", "d1", [("l1", 100), ("l2", 50)])
        + tag("app/a/2.0", "d2", [("l1", 100), ("l3", 70)])            # shares l1 with 1.0
        + tag("shared/b/1.0", "d3", [("l4", 10)])
        + tag("conflict/c/1.0", "d4", [("l5", 10)])
        + tag("jenkins-x/1", "d9", [("l9", 10)])                        # excluded
        + tag("app/a/sha256:abc", "d0", [("l0", 5)])                    # digest folder: skipped
        + [{"path": "_uploads/x", "name": "blob", "sha256": "u", "size": 1}]  # upload area: skipped
    ),
    "idm-docker-public": (
        tag("app/a/1.0", "d1", [("l1", 100), ("l2", 50)])              # identical to production
        + tag("shared/b/1.0", "d3", [("l4", 10)])                      # identical
        + tag("conflict/c/1.0", "d5", [("l6", 10)])                    # same tag, different image
        + tag("pub/only/1", "d6", [("l7", 20)])
        + tag("Upper/Name/1", "d7", [("l8", 5)])                        # uppercase -> lowercase target
    ),
    "docker-production-public": tag("pub/only2/1", "d8", [("l1", 100)]),  # shares l1 again
}
m.fetch_files = lambda url, repo: iter(REPOS[repo])

TARGET = {}      # dst -> digest
COPIES = []
FAIL = set()
def fake_crane(*args):
    if args[0] == "digest":
        return (args[1] in TARGET, TARGET.get(args[1], "MANIFEST_UNKNOWN"))
    if args[0] == "copy":
        src, dst = args[1], args[2]
        COPIES.append((src, dst))
        if dst in FAIL:
            return False, "upload failed: connection reset"
        host, ref = src.split("/", 1)
        if host.startswith("packagesidmod-"):          # subdomain style: <server>-<repo>.jfrog.io
            repo = host.split(".", 1)[0][len("packagesidmod-"):]
        else:                                          # path style: <host>/<repo>/<image>
            repo, ref = ref.split("/", 1)
        image, t = ref.rsplit(":", 1)
        TARGET[dst] = m.list_tags("x", repo)[(image, t)]["digest"]
        return True, ""
    raise AssertionError(args)
m.crane = fake_crane
m.crane_available = lambda: True

WORK = tempfile.mkdtemp(prefix="test-docker-")
LOG = os.path.join(WORK, "log.csv")
BASE = ["x", "--url", "https://packagesidmod.jfrog.io/artifactory", "--target", "ghcr.io/idm",
        "--exclude", "jenkins-*", "--log", LOG]


def run(*extra):
    sys.argv = BASE + list(extra)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            m.main()
        except SystemExit as e:
            code = e.code
    rows = {(r["image"], r["tag"]): r for r in csv.DictReader(open(LOG, encoding="utf-8"))}
    return code, rows, out.getvalue()


# 1. Dry run: nothing copied, duplicates merged, conflict reported
code, rows, out = run()
assert COPIES == []
st = {k: r["status"] for k, r in rows.items()}
assert st == {
    ("app/a", "1.0"): "would-copy", ("app/a", "2.0"): "would-copy", ("shared/b", "1.0"): "would-copy",
    ("conflict/c", "1.0"): "conflict", ("jenkins-x", "1"): "excluded", ("pub/only", "1"): "would-copy",
    ("Upper/Name", "1"): "would-copy", ("pub/only2", "1"): "would-copy",
}, st
assert ("app/a", "sha256:abc") not in rows
assert rows[("app/a", "1.0")]["source_repo"] == "idm-docker-production"
assert "identical in idm-docker-public" in rows[("app/a", "1.0")]["detail"]
assert rows[("Upper/Name", "1")]["target"] == "ghcr.io/idm/upper/name:1"
assert "d4" in rows[("conflict/c", "1.0")]["detail"] and "d5" in rows[("conflict/c", "1.0")]["detail"]
assert code == 1  # conflict
# Unique content: l1 100 + l2 50 + l3 70 + l4 10 + l7 20 + l8 5 = 255 bytes; l1 counted once
assert "0.0 GB (each layer counted once)" in out

# 2. Execute: one copy per unique image:tag; skips what the target has; reports failures
TARGET["ghcr.io/idm/app/a:1.0"] = "sha256:d1"        # already there, same image
TARGET["ghcr.io/idm/pub/only:1"] = "sha256:other"    # already there, different image
FAIL.add("ghcr.io/idm/shared/b:1.0")
code, rows, out = run("--execute")
st = {k: r["status"] for k, r in rows.items()}
assert st[("app/a", "1.0")] == "already-in-target"
assert st[("pub/only", "1")] == "conflict-in-target"
assert st[("shared/b", "1.0")] == "failed" and "connection reset" in rows[("shared/b", "1.0")]["detail"]
assert st[("app/a", "2.0")] == st[("Upper/Name", "1")] == st[("pub/only2", "1")] == "copied"
assert st[("conflict/c", "1.0")] == "conflict"
dsts = [d for _, d in COPIES]
assert len(dsts) == len(set(dsts)) == 4, COPIES         # each target written at most once
assert ("packagesidmod-idm-docker-production.jfrog.io/app/a:2.0", "ghcr.io/idm/app/a:2.0") in COPIES
assert code == 1

# 3. Re-run after fixing the failure: only the failed one is copied; conflict resolved by preference
FAIL.clear()
before = len(COPIES)
code, rows, out = run("--execute", "--prefer-first-repo")
new = COPIES[before:]
assert sorted(d for _, d in new) == ["ghcr.io/idm/conflict/c:1.0", "ghcr.io/idm/shared/b:1.0"], new
assert ("packagesidmod-idm-docker-production.jfrog.io/conflict/c:1.0", "ghcr.io/idm/conflict/c:1.0") in new
assert rows[("app/a", "2.0")]["status"] == "already-in-target"
assert code == 1  # pub/only is still a conflict in the target

# 4. --limit picks only the first N images to copy
TARGET.clear(); COPIES.clear()
code, rows, out = run("--limit", "2")
assert sum(1 for r in rows.values() if r["status"] == "would-copy") == 2

# 5. --images: only the named images; with one repo in --repos, that repo's version of a conflict
code, rows, out = run("--images", "app/a:1.0", "pub/*", "--target", "ghcr.io/IDM")
assert rows[("app/a", "1.0")]["target"] == "ghcr.io/idm/app/a:1.0"  # target lowercased
assert set(rows) == {("app/a", "1.0"), ("pub/only", "1"), ("pub/only2", "1")}, set(rows)
assert code == 0
code, rows, out = run("--images", "conflict/c:1.0", "--repos", "idm-docker-public", "--execute")
assert rows[("conflict/c", "1.0")]["status"] == "copied"
assert rows[("conflict/c", "1.0")]["source_repo"] == "idm-docker-public"
assert TARGET["ghcr.io/idm/conflict/c:1.0"] == "sha256:d5"
sys.argv = BASE + ["--images", "no-such-image"]
err = io.StringIO()
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    m.main()
assert "--images no-such-image matched no image" in err.getvalue()
# 6. path-style addressing on request
TARGET.clear(); COPIES.clear()
code, rows, out = run("--images", "app/a:2.0", "--source-style", "path", "--execute")
assert COPIES == [("packagesidmod.jfrog.io/idm-docker-production/app/a:2.0", "ghcr.io/idm/app/a:2.0")], COPIES
assert m.source_ref("packagesidmod.jfrog.io", "subdomain", "idm-docker-public", "x/y", "1") == \
    "packagesidmod-idm-docker-public.jfrog.io/x/y:1"
print("ALL OK")
