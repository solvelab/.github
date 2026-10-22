"""Testes do `scripts/ghcr-retention.py` com a API e o registro falsos (solvelab/.github#1).

Rodar: `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import pathlib
import unittest
import urllib.error
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "ghcr-retention.py"
spec = importlib.util.spec_from_file_location("ghcr_retention", SCRIPT)
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)

NOW = datetime.datetime.now(datetime.timezone.utc)
PLATFORM = {"os": "linux", "architecture": "amd64"}
ATTESTATION = {"platform": {"os": "unknown", "architecture": "unknown"},
               "annotations": {"vnd.docker.reference.type": "attestation-manifest"}}


def ago(hours: float) -> str:
    return (NOW - datetime.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class Fake:
    """Pacote falso: `versions` como a API lista, `manifests` como o registro responde."""

    def __init__(self):
        self.versions: list[dict] = []
        self.manifests: dict[str, dict] = {}
        self.deleted: list[int] = []
        self._id = 0

    def add(self, digest: str, tags=(), hours=48.0, children=(), listed=True):
        self._id += 1
        if listed:
            self.versions.append({"id": self._id, "name": digest, "created_at": ago(hours),
                                  "metadata": {"container": {"tags": list(tags)}}})
        self.manifests[digest] = {"manifests": [{"digest": d, **extra} for d, extra in children]}
        return self._id

    def image(self, n: int, tags=(), hours=48.0, attestation=True):
        """Índice do buildx: plataforma + atestação, os dois sem tag e listados."""
        plat, att = f"sha256:p{n}", f"sha256:a{n}"
        self.add(plat, hours=hours)
        children = [(plat, {"platform": PLATFORM})]
        if attestation:
            self.add(att, hours=hours)
            children.append((att, ATTESTATION))
        return self.add(f"sha256:i{n}", tags=tags, hours=hours, children=children)

    def id_of(self, digest: str) -> int:
        return next(v["id"] for v in self.versions if v["name"] == digest)

    def request(self, url, headers, method="GET"):
        if "/versions?" in url:
            return list(self.versions), ""
        if url.startswith("https://ghcr.io/token"):
            return {"token": "t"}, ""
        if method == "DELETE":
            self.deleted.append(int(url.rsplit("/", 1)[1]))
            return None, ""
        digest = url.rsplit("/", 1)[1]
        if digest not in self.manifests:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return self.manifests[digest], ""


def run(fake: Fake, **env) -> int:
    base = {"GH_TOKEN": "x", "ORG": "solvelab", "PACKAGE": "app", "KEEP": "2"}
    with mock.patch.dict(os.environ, {**base, **env}, clear=True), \
            mock.patch.object(retention, "request", fake.request):
        return retention.main()


class RetentionTest(unittest.TestCase):
    def test_keeps_newest_tagged_with_children_and_deletes_the_rest(self):
        f = Fake()
        old = f.image(1, tags=["v1"], hours=30)
        mid = f.image(2, tags=["v2"], hours=20)
        new = f.image(3, tags=["v3", "latest"], hours=10)
        self.assertEqual(run(f), 0)
        kept = {mid, new, f.id_of("sha256:p2"), f.id_of("sha256:a2"), f.id_of("sha256:p3"),
                f.id_of("sha256:a3")}
        self.assertEqual(set(f.deleted), {old, f.id_of("sha256:p1"), f.id_of("sha256:a1")})
        self.assertFalse(kept & set(f.deleted))

    def test_dry_run_deletes_nothing(self):
        f = Fake()
        for n in range(4):
            f.image(n, tags=[f"v{n}"], hours=10 + n)
        self.assertEqual(run(f, DRY_RUN="true"), 0)
        self.assertEqual(f.deleted, [])

    def test_unlisted_child_that_exists_aborts_without_deleting(self):
        f = Fake()
        f.image(1, tags=["v1"], hours=30)
        f.add("sha256:ghost", listed=False)  # existe no registro, a API não lista
        f.add("sha256:i9", tags=["v9"], hours=5, children=[("sha256:ghost", {"platform": PLATFORM})])
        self.assertEqual(run(f), 1)
        self.assertEqual(f.deleted, [])

    def test_missing_platform_marks_broken_and_prunes_it(self):
        f = Fake()
        good = f.image(1, tags=["v1"], hours=30)
        f.add("sha256:i2", tags=["v2"], hours=5, children=[("sha256:gone", {"platform": PLATFORM})])
        self.assertEqual(run(f, KEEP="1"), 0)
        self.assertIn(f.id_of("sha256:i2"), f.deleted)
        self.assertNotIn(good, f.deleted)

    def test_missing_attestation_only_still_counts_as_intact(self):
        f = Fake()
        f.add("sha256:p1", hours=5)
        v1 = f.add("sha256:i1", tags=["v1"], hours=5, children=[
            ("sha256:p1", {"platform": PLATFORM}), ("sha256:gone-att", ATTESTATION)])
        old = f.image(2, tags=["v0"], hours=30)
        self.assertEqual(run(f, KEEP="1"), 0)
        self.assertNotIn(v1, f.deleted)
        self.assertNotIn(f.id_of("sha256:p1"), f.deleted)
        self.assertIn(old, f.deleted)

    def test_nested_index_grandchildren_are_kept(self):
        f = Fake()
        f.add("sha256:leaf", hours=40)
        f.add("sha256:inner", hours=40, children=[("sha256:leaf", {"platform": PLATFORM})])
        f.add("sha256:outer", tags=["v1"], hours=40, children=[("sha256:inner", {})])
        self.assertEqual(run(f, KEEP="1"), 0)
        self.assertEqual(f.deleted, [])

    def test_other_tag_family_is_untouched(self):
        f = Fake()
        probe = f.image(1, tags=["probe-abc"], hours=200)
        for n in range(2, 6):
            f.image(n, tags=[f"v{n}"], hours=100 - n)
        self.assertEqual(run(f, TAG_PATTERN=r"^(v\d|latest$)"), 0)
        for kept in (probe, f.id_of("sha256:p1"), f.id_of("sha256:a1")):
            self.assertNotIn(kept, f.deleted)
        self.assertIn(f.id_of("sha256:i2"), f.deleted)

    def test_latest_moved_to_old_version_is_never_deleted(self):
        f = Fake()
        rolled_back = f.image(1, tags=["v1", "latest"], hours=100)
        for n in range(2, 5):
            f.image(n, tags=[f"v{n}"], hours=50 - n)
        self.assertEqual(run(f), 0)
        self.assertNotIn(rolled_back, f.deleted)
        self.assertNotIn(f.id_of("sha256:p1"), f.deleted)

    def test_recent_untagged_version_is_in_grace(self):
        f = Fake()
        f.image(1, tags=["v1"], hours=30)
        pushing = f.add("sha256:half-pushed", hours=0.1)  # filho de um push sem tag ainda
        stale = f.add("sha256:orphan", hours=5)
        self.assertEqual(run(f, KEEP="1"), 0)
        self.assertNotIn(pushing, f.deleted)
        self.assertIn(stale, f.deleted)

    def test_invalid_keep_or_grace_aborts(self):
        for env in ({"KEEP": "0"}, {"GRACE_MIN": "0"}, {"GRACE_MIN": "-60"}):
            f = Fake()
            f.image(1, tags=["v1"], hours=30)
            f.add("sha256:orphan", hours=5)
            self.assertEqual(run(f, **env), 1, env)
            self.assertEqual(f.deleted, [], env)

    def test_network_error_on_delete_is_counted(self):
        f = Fake()
        f.image(1, tags=["v1"], hours=30)
        f.add("sha256:orphan", hours=5)
        real = f.request

        def flaky(url, headers, method="GET"):
            if method == "DELETE":
                raise urllib.error.URLError("reset")
            return real(url, headers, method)

        with mock.patch.dict(os.environ, {"GH_TOKEN": "x", "ORG": "o", "PACKAGE": "app",
                                          "KEEP": "1"}, clear=True), \
                mock.patch.object(retention, "request", flaky):
            self.assertEqual(retention.main(), 1)


if __name__ == "__main__":
    unittest.main()
