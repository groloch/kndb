"""Download the frontend's third-party JS into static/vendor/.

The pages load these from `/static/vendor/` rather than a CDN, so KNDB renders
notes and PDFs with no network. They are build artifacts, not sources: this
script fetches them instead of the repository carrying two megabytes of
minified JS that git would diff on every upgrade.

    python dev_tools/fetch_vendor.py           # fetch anything missing
    python dev_tools/fetch_vendor.py --force   # re-download everything

Packages come from the npm registry as plain tarballs — no Node, no npm client,
and nothing outside the standard library, so this runs on a bare Python before
the virtualenv exists. Each package is pinned to an exact version and to the
SHA-256 of its tarball; a mismatch aborts before a single byte is written,
because the payload here is code the browser will run.

Upgrading: bump `version`, run with `--force`, and paste the hash the failure
reports. Check the new files in the browser before committing the bump.
"""

import argparse
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static", "vendor")

# name -> version, tarball SHA-256, and {path in the tarball: name on disk}
PACKAGES = {
    "pdfjs-dist": {
        "version": "6.2.108",
        "sha256": "b3e68d5cda70551a90b3f771419d379e20fc788ce056fa32de73608e01df47f4",
        "files": {
            "package/build/pdf.min.mjs": "pdf.min.mjs",
            "package/build/pdf.worker.min.mjs": "pdf.worker.min.mjs",
            "package/LICENSE": "pdf.js.LICENSE",
        },
    },
    "marked": {
        "version": "12.0.2",
        "sha256": "6cfd2d09c6bce2541558a1547e5f3b9895ed743f0d287536ff2280e318e8a074",
        "files": {
            "package/marked.min.js": "marked.min.js",
            "package/LICENSE.md": "marked.LICENSE.md",
        },
    },
    "dompurify": {
        "version": "3.4.13",
        "sha256": "2a0647141c748404c9958cf12079c3fdb79cf9a7faaa87c8fac034a3b1275ab2",
        "files": {
            "package/dist/purify.min.js": "purify.min.js",
            "package/LICENSE": "dompurify.LICENSE",
        },
    },
}


class VendorError(RuntimeError):
    pass


def _tarball_url(name: str, version: str) -> str:
    # scoped names (@scope/pkg) put only the last segment in the filename
    return (f"https://registry.npmjs.org/{name}/-/"
            f"{name.rsplit('/', 1)[-1]}-{version}.tgz")

def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 — pinned host
        return r.read()

def _extract(blob: bytes, files: dict) -> dict:
    """Read the wanted members out of the tarball, in memory.

    Nothing is written until every member is found, so a moved path in a new
    release leaves the previous vendor directory intact rather than half of it."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member, dest in files.items():
            f = tf.extractfile(member)
            if f is None:
                raise VendorError(f"{member} is missing from the tarball")
            out[dest] = f.read()
    return out

def fetch(name: str, spec: dict) -> list:
    url = _tarball_url(name, spec["version"])
    print(f"  {name}@{spec['version']} … ", end="", flush=True)
    blob = _download(url)
    got = hashlib.sha256(blob).hexdigest()
    if got != spec["sha256"]:
        raise VendorError(
            f"{name}@{spec['version']} does not match its pinned hash.\n"
            f"    expected {spec['sha256']}\n    got      {got}\n"
            f"    {url}\n"
            "  If you just bumped the version, paste the hash above into "
            "PACKAGES. Otherwise do not use this download.")
    written = _extract(blob, spec["files"])
    for dest, data in written.items():
        with open(os.path.join(VENDOR, dest), "wb") as f:
            f.write(data)
    print(f"{len(blob) / 1024:.0f} KB → {len(written)} files")
    return list(written)

def missing() -> list:
    return [name for name, spec in PACKAGES.items()
            if any(not os.path.exists(os.path.join(VENDOR, dest))
                   for dest in spec["files"].values())]

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="re-download packages that are already present")
    args = ap.parse_args()

    os.makedirs(VENDOR, exist_ok=True)
    todo = list(PACKAGES) if args.force else missing()
    if not todo:
        have = ", ".join(f"{n}@{s['version']}" for n, s in PACKAGES.items())
        print(f"static/vendor is up to date ({have})")
        return 0

    print(f"Fetching into {VENDOR}")
    names = []
    for name in todo:
        names += fetch(name, PACKAGES[name])
    with open(os.path.join(VENDOR, "VERSION"), "w", encoding="utf-8") as f:
        for name, spec in PACKAGES.items():
            f.write(f"{name} {spec['version']}\n")
    print(f"Done — {len(names)} files.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VendorError as e:
        print(f"\nvendor error: {e}", file=sys.stderr)
        sys.exit(1)
