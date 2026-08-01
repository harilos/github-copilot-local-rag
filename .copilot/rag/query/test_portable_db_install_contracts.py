from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import portable_db_install as dbi

def package(root: Path, values: dict[str, bytes]) -> Path:
    files=[]; databases=[]
    for name,value in values.items():
        db=root/".copilot"/"rag"/"dbs"/name; db.mkdir(parents=True); (db/"data.bin").write_bytes(value)
        prefix=f".copilot/rag/dbs/{name}"; records=dbi.file_records(db,prefix); files.extend(records)
        databases.append({"name":name,"prefix":prefix,"file_count":len(records),"bytes":sum(int(item["size"]) for item in records),"fingerprint":dbi.records_fingerprint(records),"coverage":"closed-set"})
    (root/"PACKAGE-MANIFEST.json").write_text(json.dumps({"schema":dbi.SCHEMA,"files":files,"databases":databases}),encoding="utf-8")
    return root

class PortableDatabaseInstallContracts(unittest.TestCase):
    def test_installs_multiple_preserves_unrelated_and_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=package(root/"package",{"one-rag":b"one","two-rag":b"two"}); target=root/"target"
            (target/"private-rag").mkdir(parents=True); (target/"private-rag"/"keep").write_bytes(b"private")
            first=dbi.install_databases(source,target); self.assertEqual({"one-rag":"installed","two-rag":"installed"},first["databases"])
            stamp=(target/"one-rag"/"data.bin").stat().st_mtime_ns; second=dbi.install_databases(source,target)
            self.assertEqual("already_installed",second["databases"]["one-rag"]); self.assertEqual(stamp,(target/"one-rag"/"data.bin").stat().st_mtime_ns); self.assertEqual(b"private",(target/"private-rag"/"keep").read_bytes())
    def test_collision_is_fail_closed_then_explicitly_replaceable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=package(root/"package",{"one-rag":b"new"}); target=root/"target"; (target/"one-rag").mkdir(parents=True); existing=target/"one-rag"/"data.bin"; existing.write_bytes(b"old")
            with self.assertRaisesRegex(ValueError,"replacement was not approved"): dbi.install_databases(source,target)
            self.assertEqual(b"old",existing.read_bytes()); dbi.install_databases(source,target,replace_existing=True); self.assertEqual(b"new",existing.read_bytes())
    def test_second_publish_failure_rolls_back_all_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=package(root/"package",{"one-rag":b"new1","two-rag":b"new2"}); target=root/"target"
            for name,value in (("one-rag",b"old1"),("two-rag",b"old2")): (target/name).mkdir(parents=True); (target/name/"data.bin").write_bytes(value)
            original=dbi.os.replace
            def fault(src,dst):
                if Path(src).name.startswith(".two-rag.stage-"): raise OSError("injected")
                return original(src,dst)
            with mock.patch.object(dbi.os,"replace",side_effect=fault):
                with self.assertRaisesRegex(OSError,"injected"): dbi.install_databases(source,target,replace_existing=True)
            self.assertEqual(b"old1",(target/"one-rag"/"data.bin").read_bytes()); self.assertEqual(b"old2",(target/"two-rag"/"data.bin").read_bytes())
    def test_partial_copy_failure_removes_hidden_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=package(root/"package",{"one-rag":b"one"}); target=root/"target"
            def partial_copy(_source, stage):
                stage.mkdir(parents=True)
                (stage/"partial.bin").write_bytes(b"partial")
                raise OSError("injected partial copy")
            with mock.patch.object(dbi.shutil,"copytree",side_effect=partial_copy):
                with self.assertRaisesRegex(OSError,"injected partial copy"):
                    dbi.install_databases(source,target)
            self.assertEqual([],list(target.glob(".*.stage-*")))

    def test_rejects_stray_file_outside_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=package(root/"package",{"one-rag":b"one"}); (source/"stray.txt").write_text("stray",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"closed set"): dbi.preflight(source,root/"target")
if __name__=="__main__": unittest.main()
