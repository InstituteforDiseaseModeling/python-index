"""GitHub Release helpers for the migration scripts, using the GitHub CLI (`gh`).

Sign in once with:  gh auth login
"""
import json
import shutil
import subprocess


def gh(*args):
    """Run gh; return (ok, output). Replaced in tests."""
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()


def gh_available():
    return shutil.which("gh") is not None


class Releases:
    """Releases of one GitHub repo (owner/name)."""

    def __init__(self, repo):
        self.repo = repo
        self._cache = {}

    def assets(self, tag):
        """{asset name: {"name", "size", "url"}} for a release, or None if the release doesn't exist."""
        if tag not in self._cache:
            ok, out = gh("release", "view", tag, "-R", self.repo, "--json", "assets")
            if not ok:
                if "not found" in out.lower():
                    self._cache[tag] = None
                    return None
                raise RuntimeError(f"gh release view {tag}: {out}")
            self._cache[tag] = {a["name"]: a for a in json.loads(out).get("assets", [])}
        return self._cache[tag]

    def create(self, tag, notes):
        ok, out = gh("release", "create", tag, "-R", self.repo, "--title", tag, "--notes", notes)
        if not ok:
            raise RuntimeError(f"gh release create {tag}: {out}")
        self._cache[tag] = {}

    def upload(self, tag, path, clobber=False):
        args = ["release", "upload", tag, path, "-R", self.repo] + (["--clobber"] if clobber else [])
        ok, out = gh(*args)
        self._cache.pop(tag, None)  # re-read assets (and GitHub's final names) next time
        return ok, out

    def download_url(self, tag, name):
        return f"https://github.com/{self.repo}/releases/download/{tag}/{name}"
