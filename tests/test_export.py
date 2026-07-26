"""WP6 export tests — the archive, its record, and the guards around both.

Built on a synthetic root rather than the real 198 MB `index/`, so the suite stays
fast and can assert exact file counts. The real round trip (build → verify →
extract → query) is recorded in the WP6 handover.
"""
from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kng.pipeline import export


def fake_root(tmp: Path, *, with_extracted: bool = True, with_graph: bool = True) -> Path:
    root = tmp / "root"
    (root / "index/chunks/data").mkdir(parents=True)
    (root / "index/lancedb/chunks.lance").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "index/manifest.json").write_text('{"files": {}}', encoding="utf-8")
    (root / "index/stats.json").write_text('{"chunks": 3}', encoding="utf-8")
    (root / "index/chunks/data/a.json").write_text('[{"chunk_id": "a"}]', encoding="utf-8")
    (root / "index/lancedb/chunks.lance/data.bin").write_bytes(b"vectors")
    (root / "config/ontology.yaml").write_text("nodes: []\n", encoding="utf-8")
    if with_graph:
        (root / "index/graph").mkdir(parents=True)
        (root / "index/graph/graph.json").write_text(
            '{"nodes": [1, 2, 3], "edges": [1, 2]}', encoding="utf-8")
    if with_extracted:
        (root / "extracted/data").mkdir(parents=True)
        (root / "extracted/data/a.json").write_text('{"text": "hi"}', encoding="utf-8")
    # Things that must never travel.
    (root / ".env").write_text("SARVAM_API_KEY=secret-key-value\n", encoding="utf-8")
    (root / "var").mkdir()
    (root / "var/users.json").write_text('{"users": [{"hash": "scrypt"}]}', encoding="utf-8")
    (root / "data").mkdir()
    (root / "data/huge.pdf").write_bytes(b"x" * 1024)
    return root


class TestInventory(unittest.TestCase):
    def test_counts_only_the_allow_listed_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            parts = export.inventory(root)
            rels = [p.rel for p in parts]
            self.assertIn("index/chunks", rels)
            self.assertIn("extracted", rels)
            self.assertNotIn("var", rels)
            self.assertNotIn("data", rels)
            self.assertNotIn(".env", rels)

    def test_no_extracted_drops_that_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            rels = [p.rel for p in export.inventory(root, with_extracted=False)]
            self.assertNotIn("extracted", rels)

    def test_missing_required_part_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            (root / "index/manifest.json").unlink()
            self.assertEqual(export.missing_required(export.inventory(root)),
                             ["index/manifest.json"])

    def test_absent_optional_part_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp), with_graph=False)
            self.assertEqual(export.missing_required(export.inventory(root)), [])


class TestBuild(unittest.TestCase):
    def test_archive_contains_the_index_and_never_the_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            out = Path(tmp) / "bundle.tar.gz"
            record = export.build(out, root=root)

            with tarfile.open(out) as tar:
                names = tar.getnames()
            self.assertIn("index/manifest.json", names)
            self.assertIn("index/lancedb/chunks.lance/data.bin", names)
            self.assertIn("extracted/data/a.json", names)
            self.assertIn(export.RECORD_NAME, names)
            for forbidden in (".env", "var/users.json", "data/huge.pdf"):
                self.assertNotIn(forbidden, names, f"{forbidden} must never be exported")
            blob = out.read_bytes()
            self.assertNotIn(b"secret-key-value", blob)
            self.assertEqual(record["archive"]["sha256"], export.sha256(out))

    def test_a_sidecar_checksum_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            out = Path(tmp) / "bundle.tar.gz"
            export.build(out, root=root)
            sidecar = Path(f"{out}.sha256")
            self.assertTrue(sidecar.exists())
            self.assertEqual(sidecar.read_text(encoding="utf-8").split()[0],
                             export.sha256(out))

    def test_record_names_the_embedding_model_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            out = Path(tmp) / "bundle.tar.gz"
            export.build(out, root=root, note="hello")
            record = export.read_record(out)
            self.assertTrue(record["embed_model"], "vectors are useless without the model")
            self.assertEqual(record["counts"]["graph"], {"nodes": 3, "edges": 2})
            self.assertEqual(record["counts"]["chunk_files"], 1)
            self.assertEqual(record["note"], "hello")
            self.assertEqual(record["format"], 1)

    def test_export_refuses_without_a_required_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            (root / "index/chunks/data/a.json").unlink()
            (root / "index/chunks/data").rmdir()
            (root / "index/chunks").rmdir()
            with self.assertRaises(FileNotFoundError):
                export.build(Path(tmp) / "x.tar.gz", root=root)


class TestVerify(unittest.TestCase):
    def _bundle(self, tmp: Path) -> Path:
        root = fake_root(tmp)
        out = tmp / "bundle.tar.gz"
        export.build(out, root=root)
        return out

    def test_a_good_archive_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._bundle(Path(tmp))
            result = export.verify(out)
            self.assertTrue(result["ok"], result["problems"])
            self.assertEqual(result["files"], result["record"]["totals"]["files"])

    def test_a_tampered_checksum_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._bundle(Path(tmp))
            Path(f"{out}.sha256").write_text("0" * 64 + f"  {out.name}\n", encoding="utf-8")
            result = export.verify(out)
            self.assertFalse(result["ok"])
            self.assertTrue(any("checksum mismatch" in p for p in result["problems"]))

    def test_a_missing_sidecar_is_reported_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self._bundle(Path(tmp))
            Path(f"{out}.sha256").unlink()
            self.assertFalse(export.verify(out)["ok"])

    def test_an_embedding_model_mismatch_is_reported(self):
        """The failure this whole record exists to prevent."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self._bundle(Path(tmp))
            with tarfile.open(out) as tar:
                record = json.loads(tar.extractfile(export.RECORD_NAME).read())
            record["embed_model"] = "some/other-model"

            rebuilt = Path(tmp) / "rebuilt.tar.gz"
            with tarfile.open(out) as src, tarfile.open(rebuilt, "w:gz") as dst:
                for member in src.getmembers():
                    if member.name == export.RECORD_NAME:
                        continue
                    dst.addfile(member, src.extractfile(member) if member.isfile() else None)
                blob = json.dumps(record).encode("utf-8")
                info = tarfile.TarInfo(export.RECORD_NAME)
                info.size = len(blob)
                dst.addfile(info, __import__("io").BytesIO(blob))
            Path(f"{rebuilt}.sha256").write_text(
                f"{export.sha256(rebuilt)}  {rebuilt.name}\n", encoding="utf-8")

            result = export.verify(rebuilt)
            self.assertFalse(result["ok"])
            self.assertTrue(any("embedding model differs" in p for p in result["problems"]))

    def test_an_archive_without_a_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.tar.gz"
            (Path(tmp) / "f.txt").write_text("x", encoding="utf-8")
            with tarfile.open(plain, "w:gz") as tar:
                tar.add(Path(tmp) / "f.txt", arcname="f.txt")
            with self.assertRaises(ValueError):
                export.read_record(plain)


class TestExtract(unittest.TestCase):
    def test_round_trip_restores_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            out = Path(tmp) / "bundle.tar.gz"
            record = export.build(out, root=root)
            dest = Path(tmp) / "dest"
            n = export.extract(out, dest)
            self.assertEqual(n, record["totals"]["files"] + 1)      # + EXPORT.json
            self.assertEqual((dest / "index/chunks/data/a.json").read_text(encoding="utf-8"),
                             '[{"chunk_id": "a"}]')
            self.assertTrue((dest / "extracted/data/a.json").exists())

    def test_extract_refuses_a_member_that_escapes_the_destination(self):
        """A tarball is untrusted input; `../../etc/x` must not be written."""
        with tempfile.TemporaryDirectory() as tmp:
            evil = Path(tmp) / "evil.tar.gz"
            (Path(tmp) / "payload").write_text("bad", encoding="utf-8")
            with tarfile.open(evil, "w:gz") as tar:
                tar.add(Path(tmp) / "payload", arcname="../escaped.txt")
            with self.assertRaises(ValueError):
                export.extract(evil, Path(tmp) / "dest")
            self.assertFalse((Path(tmp) / "escaped.txt").exists())

    def test_verify_flags_an_unsafe_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fake_root(Path(tmp))
            good = Path(tmp) / "good.tar.gz"
            export.build(good, root=root)
            evil = Path(tmp) / "evil.tar.gz"
            with tarfile.open(good) as src, tarfile.open(evil, "w:gz") as dst:
                for m in src.getmembers():
                    dst.addfile(m, src.extractfile(m) if m.isfile() else None)
                blob = b"payload"
                info = tarfile.TarInfo("../escaped.txt")
                info.size = len(blob)
                dst.addfile(info, __import__("io").BytesIO(blob))
            Path(f"{evil}.sha256").write_text(f"{export.sha256(evil)}  {evil.name}\n",
                                              encoding="utf-8")
            problems = export.verify(evil)["problems"]
            self.assertTrue(any("unsafe member path" in p for p in problems))


class TestEmbeddingGuard(unittest.TestCase):
    """A query embedded by the wrong model must fail loudly, not rank nonsense."""

    class FakeTable:
        def __init__(self, dim):
            import pyarrow as pa
            self.schema = pa.schema([pa.field("vector", pa.list_(pa.float32(), dim))])

    def test_matching_dimension_passes(self):
        from kng.store import vector
        vector.check_dim(self.FakeTable(4), [0.1, 0.2, 0.3, 0.4])

    def test_mismatch_names_the_cause_and_the_env_var(self):
        from kng.store import vector
        with self.assertRaises(ValueError) as caught:
            vector.check_dim(self.FakeTable(1024), [0.0] * 768)
        message = str(caught.exception)
        self.assertIn("768", message)
        self.assertIn("1024", message)
        self.assertIn("LOCAL_EMBED_MODEL", message)

    def test_the_default_model_matches_the_committed_index(self):
        """A clone that never wrote a `.env` must still search the shipped vectors.

        The default used to be the 768-dim model WP2 abandoned, so `python -m
        kng.query` on a fresh clone died inside LanceDB with "there is no vector
        column".
        """
        import dataclasses

        from kng.config import Settings
        fresh = dataclasses.replace(Settings())          # no env overrides applied
        self.assertEqual(fresh.local_embed_model, "BAAI/bge-m3")


if __name__ == "__main__":
    unittest.main()
