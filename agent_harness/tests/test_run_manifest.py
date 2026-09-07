"""Round 6 release blocker: the write-once run manifest is the SOLE source of
resume authorization. It is created only at fresh bootstrap and is never healed,
regenerated, or inferred from mutable graph state.

Codex's exact reproduction (manifest deleted + mutable state substituted to
another feature ref) must fail closed with no Git ref moved.
"""

import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.git_tools import GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import (
    Persistence,
    RunManifestError,
    reconcile_for_resume,
    verify_run_authorization,
)
from agent_harness.state import CheckResult, HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]
AUTHOR = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
ORIG = "refs/heads/feature-original"
OTHER = "refs/heads/other-feature"


@contextmanager
def repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True,
                                        capture_output=True, text=True)
        run("init", "-q", "-b", "feature-original")
        run("config", "user.email", "t@e.c"); run("config", "user.name", "T")
        (root / ".gitignore").write_text(".agent/\n")
        (root / "app.py").write_text("v0\n")
        run("add", "-A"); run("commit", "-qm", "P")
        run("branch", "other-feature")
        run("branch", "main")
        run("branch", "master")
        yield root, run


def _authorized_reviewed_state(root, run):
    """A fresh run authorized for ORIG, with a real manifest, a valid candidate,
    and valid verify+review evidence - i.e. everything is legitimate."""
    g = GitTools(root)
    store = Persistence(root)
    parent = g.resolve_ref(ORIG)
    (root / "app.py").write_text("candidate\n")
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=["app.py"], protected=PROTECTED)
    commit = g.commit_tree(tree_oid=tree, parent=parent, message="harness: t [iter 1] [run r]",
                           author=AUTHOR, committer=AUTHOR)
    st = HarnessState(
        objective="o", run_id="r", current_task="t", owned_paths=["app.py"],
        run_target_ref=ORIG, checkpoint_paths=["app.py"], checkpoint_target_ref=ORIG,
        checkpoint_expected_parent=parent, checkpoint_message="harness: t [iter 1] [run r]",
        checkpoint_author=AUTHOR, checkpoint_committer=AUTHOR,
        candidate_tree_oid=tree, candidate_commit_oid=commit,
        verified_tree_oid=tree, verified_commit_oid=commit,
        reviewed_tree_oid=tree, reviewed_commit_oid=commit,
        checks_passed=True, review_status="pass",
        expected_head=parent, baseline_head=parent,
        checkpoint_phase="reviewed", next_node="checkpoint",
    )
    store.create_run_manifest(st, authorized_target_ref=ORIG, initial_target_oid=parent)
    return g, store, st, parent, tree, commit


def _resume_full(root, st):
    """cli-style resume: authorize -> reconcile -> run the graph tail."""
    g = GitTools(root)
    store = Persistence(root)
    reason = verify_run_authorization(st, store, g)
    if reason is not None:
        st.stop_reason = reason
        return st
    st.stop_reason = None
    st = reconcile_for_resume(st, g, PROTECTED)
    if st.stop_reason is not None:
        return st
    eng = Engine(make_config(root, checks={"commands": ["true"]}),
                 claude=FakeClaude(), codex=FakeCodex(), git=g,
                 store=store, printer=SpyPrinter(),
                 resume_target=st.next_node or "checkpoint")
    return eng.run(st)


class MissingManifestAttackTests(unittest.TestCase):
    def test_codex_exact_repro_manifest_deleted_state_substituted(self):
        with repo() as (root, run):
            g, store, st, parent, tree, commit = _authorized_reviewed_state(root, run)
            orig_before = run("rev-parse", ORIG).stdout.strip()
            other_before = run("rev-parse", OTHER).stdout.strip()
            main_before = run("rev-parse", "main").stdout.strip()
            master_before = run("rev-parse", "master").stdout.strip()

            # 1) delete ONLY the run manifest
            store._manifest_path("r").unlink()
            # 2) substitute mutable state to claim another feature ref
            st.run_target_ref = OTHER
            st.checkpoint_target_ref = OTHER

            final = _resume_full(root, st)

            self.assertIn(final.stop_reason,
                          {StopReason.RUN_AUTHORIZATION_MISSING,
                           StopReason.RUN_AUTHORIZATION_INVALID})
            self.assertNotEqual(final.stop_reason, StopReason.SUCCESS)
            # no manifest was recreated
            self.assertFalse(store._manifest_path("r").exists())
            # no ref moved
            self.assertEqual(run("rev-parse", ORIG).stdout.strip(), orig_before)
            self.assertEqual(run("rev-parse", OTHER).stdout.strip(), other_before)
            self.assertEqual(run("rev-parse", "main").stdout.strip(), main_before)
            self.assertEqual(run("rev-parse", "master").stdout.strip(), master_before)
            self.assertEqual(final.commits, [])

    def test_manifest_deleted_state_unchanged_also_fails_closed(self):
        with repo() as (root, run):
            g, store, st, parent, tree, commit = _authorized_reviewed_state(root, run)
            store._manifest_path("r").unlink()
            final = _resume_full(root, st)               # state still says ORIG
            self.assertEqual(final.stop_reason, StopReason.RUN_AUTHORIZATION_MISSING)
            self.assertFalse(store._manifest_path("r").exists())
            self.assertEqual(run("rev-parse", ORIG).stdout.strip(),
                             run("rev-parse", "HEAD").stdout.strip())
            self.assertEqual(final.commits, [])

    def test_missing_manifest_never_calls_paid_worker(self):
        with repo() as (root, run):
            g, store, st, parent, tree, commit = _authorized_reviewed_state(root, run)
            store._manifest_path("r").unlink()
            st.run_target_ref = OTHER
            st.checkpoint_target_ref = OTHER
            claude, codex = FakeClaude(), FakeCodex()
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=claude, codex=codex, git=g, store=store,
                         printer=SpyPrinter(), resume_target=st.next_node or "checkpoint")
            final = eng.run(st)   # bootstrap-resume authorization stops it first
            self.assertIn(final.stop_reason,
                          {StopReason.RUN_AUTHORIZATION_MISSING,
                           StopReason.RUN_AUTHORIZATION_INVALID})
            self.assertEqual(claude.calls, [])
            self.assertEqual(codex.calls, [])
            self.assertEqual(final.commits, [])


OID = "a" * 40
COMPLETE = {
    "version": 1, "run_id": "r", "authorized_target_ref": ORIG,
    "initial_target_oid": OID, "created_at": "2026-09-07T12:00:00+00:00",
}
_DROP = object()


def _with(**overrides):
    d = dict(COMPLETE)
    for k, v in overrides.items():
        if v is _DROP:
            d.pop(k, None)
        else:
            d[k] = v
    return d


class ReadRunManifestSchemaTests(unittest.TestCase):
    """Table-driven: every case exercises the ACTUAL runtime parser
    ``Persistence.read_run_manifest()``, not just ``RunManifest(...)``."""

    def _read(self, text):
        with repo() as (root, run):
            store = Persistence(root)
            p = store._manifest_path("r")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            return store.read_run_manifest("r")

    VALID = ("valid", json.dumps(COMPLETE), False)

    CASES = [
        VALID,
        # missing required fields
        ("missing version", json.dumps(_with(version=_DROP)), True),
        ("missing run_id", json.dumps(_with(run_id=_DROP)), True),
        ("missing authorized_target_ref", json.dumps(_with(authorized_target_ref=_DROP)), True),
        ("missing initial_target_oid", json.dumps(_with(initial_target_oid=_DROP)), True),
        ("missing created_at", json.dumps(_with(created_at=_DROP)), True),
        # version
        ("version 0", json.dumps(_with(version=0)), True),
        ("version 2", json.dumps(_with(version=2)), True),
        ("version -1", json.dumps(_with(version=-1)), True),
        ('version "1"', json.dumps(_with(version="1")), True),
        ("version 1.0", '{"version": 1.0, "run_id": "r", "authorized_target_ref": "%s",'
         ' "initial_target_oid": "%s", "created_at": "2026-09-07T12:00:00+00:00"}' % (ORIG, OID), True),
        ("version true", json.dumps(_with(version=True)), True),
        ("version null", json.dumps(_with(version=None)), True),
        # run_id wrong types
        ("run_id int", json.dumps(_with(run_id=123)), True),
        ("run_id bool", json.dumps(_with(run_id=True)), True),
        ("run_id list", json.dumps(_with(run_id=[])), True),
        ("run_id obj", json.dumps(_with(run_id={})), True),
        ("run_id empty", json.dumps(_with(run_id="")), True),
        ("run_id blank", json.dumps(_with(run_id="   ")), True),
        # authorized_target_ref wrong types
        ("auth_ref int", json.dumps(_with(authorized_target_ref=123)), True),
        ("auth_ref bool", json.dumps(_with(authorized_target_ref=True)), True),
        ("auth_ref list", json.dumps(_with(authorized_target_ref=[])), True),
        ("auth_ref obj", json.dumps(_with(authorized_target_ref={})), True),
        ("auth_ref empty", json.dumps(_with(authorized_target_ref="")), True),
        # initial_target_oid
        ("oid empty", json.dumps(_with(initial_target_oid="")), True),
        ("oid null", json.dumps(_with(initial_target_oid=None)), True),
        ("oid int", json.dumps(_with(initial_target_oid=123)), True),
        ("oid bool", json.dumps(_with(initial_target_oid=True)), True),
        ("oid short", json.dumps(_with(initial_target_oid="a" * 39)), True),
        ("oid long", json.dumps(_with(initial_target_oid="a" * 41)), True),
        ("oid nonhex", json.dumps(_with(initial_target_oid="g" * 40)), True),
        ("oid sha256 ok", json.dumps(_with(initial_target_oid="A" * 64)), False),
        # created_at
        ("created_at empty", json.dumps(_with(created_at="")), True),
        ("created_at null", json.dumps(_with(created_at=None)), True),
        ("created_at int", json.dumps(_with(created_at=123)), True),
        ("created_at list", json.dumps(_with(created_at=[])), True),
        ("created_at obj", json.dumps(_with(created_at={})), True),
        ("created_at garbage", json.dumps(_with(created_at="yesterday")), True),
        # extra / typo
        ("extra field", json.dumps({**COMPLETE, "unexpected": "value"}), True),
        ("typo field", json.dumps({**_with(initial_target_oid=_DROP), "initial_targt_oid": OID}), True),
        ("wrong field type run_id via bad json number", '{"version":1,"run_id":1,'
         '"authorized_target_ref":"%s","initial_target_oid":"%s",'
         '"created_at":"2026-09-07T12:00:00+00:00"}' % (ORIG, OID), True),
        # malformed root
        ("malformed json", "{ not json", True),
        ("root list", "[]", True),
        ("root string", '""', True),
        ("root number", "123", True),
        ("root null", "null", True),
    ]

    def test_matrix(self):
        for label, text, should_reject in self.CASES:
            with self.subTest(label=label):
                if should_reject:
                    with self.assertRaises(RunManifestError):
                        self._read(text)
                else:
                    m = self._read(text)
                    self.assertEqual(m.version, 1)
                    self.assertEqual(m.run_id, "r")

    def test_run_id_mismatch_rejected(self):
        m = json.dumps(_with(run_id="not-r"))
        with self.assertRaises(RunManifestError):
            self._read(m)

    def test_old_loose_parser_would_have_accepted_this_truncated_manifest(self):
        """Regression proof: the pre-fix loose parser only required a dict with a
        matching run_id and a truthy authorized_target_ref. That exact shape is
        now rejected by the strict schema (missing version / oid / created_at)."""
        truncated = json.dumps({"run_id": "r", "authorized_target_ref": ORIG})
        # old behaviour: isinstance(dict) ✓, run_id ✓, authorized_target_ref ✓ -> ACCEPT
        old_would_accept = (
            isinstance(json.loads(truncated), dict)
            and json.loads(truncated).get("run_id") == "r"
            and bool(json.loads(truncated).get("authorized_target_ref"))
        )
        self.assertTrue(old_would_accept)
        # new behaviour: strict schema rejects it
        with self.assertRaises(RunManifestError):
            self._read(truncated)


class CorruptManifestResumeTests(unittest.TestCase):
    """Representative invalid manifests driven through the real resume
    authorization path (not just the parser)."""

    def _resume_reason(self, root, st, manifest_text):
        store = Persistence(root)
        store._manifest_path("r").write_text(manifest_text)
        return verify_run_authorization(st, store, GitTools(root))

    def _base(self, root, run):
        _, _, st, *_ = _authorized_reviewed_state(root, run)
        return st

    def test_truncated_manifest(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(
                {"run_id": "r", "authorized_target_ref": ORIG}))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_version_2(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(_with(version=2)))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_malformed_initial_target_oid(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(_with(initial_target_oid="xyz")))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_malformed_created_at(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(_with(created_at="not-a-time")))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_extra_field(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps({**COMPLETE, "sneaky": 1}))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_integer_authorized_target_ref(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(_with(authorized_target_ref=123)))
            self.assertEqual(r, StopReason.RUN_AUTHORIZATION_INVALID)   # not an AttributeError

    def test_complete_manifest_naming_main_is_protected_branch(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(
                _with(authorized_target_ref="refs/heads/main")))
            self.assertEqual(r, StopReason.PROTECTED_BRANCH)

    def test_complete_manifest_naming_master_is_protected_branch(self):
        with repo() as (root, run):
            st = self._base(root, run)
            r = self._resume_reason(root, st, json.dumps(
                _with(authorized_target_ref="refs/heads/master")))
            self.assertEqual(r, StopReason.PROTECTED_BRANCH)

    def test_symbolic_target_with_intact_manifest(self):
        with repo() as (root, run):
            st = self._base(root, run)
            run("symbolic-ref", ORIG, "refs/heads/main")
            r = verify_run_authorization(st, Persistence(root), GitTools(root))
            self.assertEqual(r, StopReason.PROTECTED_BRANCH)

    def test_no_uncaught_exception_and_no_worker_for_each_bad_manifest(self):
        bad = [
            json.dumps({"run_id": "r", "authorized_target_ref": ORIG}),   # truncated
            json.dumps(_with(version=2)),
            json.dumps(_with(initial_target_oid="xyz")),
            json.dumps(_with(created_at="nope")),
            json.dumps({**COMPLETE, "extra": 1}),
            json.dumps(_with(authorized_target_ref=123)),
        ]
        for text in bad:
            with repo() as (root, run), self.subTest(text=text[:40]):
                g, store, st, parent, tree, commit = _authorized_reviewed_state(root, run)
                store._manifest_path("r").write_text(text)
                mtime = store._manifest_path("r").stat().st_mtime_ns
                orig_before = run("rev-parse", ORIG).stdout.strip()
                claude, codex = FakeClaude(), FakeCodex()
                eng = Engine(make_config(root, checks={"commands": ["true"]}),
                             claude=claude, codex=codex, git=g, store=store,
                             printer=SpyPrinter(), resume_target=st.next_node or "checkpoint")
                final = eng.run(st)   # must not raise
                self.assertEqual(final.stop_reason, StopReason.RUN_AUTHORIZATION_INVALID)
                self.assertEqual(claude.calls, [])
                self.assertEqual(codex.calls, [])
                self.assertEqual(final.commits, [])
                self.assertEqual(g.resolve_ref(ORIG), orig_before)          # no CAS
                self.assertEqual(store._manifest_path("r").read_text(), text)  # no rewrite
                self.assertEqual(store._manifest_path("r").stat().st_mtime_ns, mtime)


class ManifestWriteOnceTests(unittest.TestCase):
    def test_fresh_bootstrap_creates_manifest(self):
        from agent_harness.workers.claude import PlanningError
        with repo() as (root, run):
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=FakeClaude(plan_exc=PlanningError("stop after bootstrap")),
                         codex=FakeCodex(), git=GitTools(root),
                         store=Persistence(root), printer=SpyPrinter())
            final = eng.run(HarnessState(objective="o", run_id="fresh1"))
            self.assertEqual(final.stop_reason, StopReason.PLANNING_FAILED)
            man = Persistence(root).read_run_manifest("fresh1")
            self.assertIsNotNone(man)
            self.assertEqual(man.authorized_target_ref, ORIG)
            self.assertEqual(man.version, 1)
            # on-disk bytes are the canonical RunManifest serialisation
            raw = json.loads((Persistence(root)._manifest_path("fresh1")).read_text())
            self.assertEqual(set(raw), {"version", "run_id", "authorized_target_ref",
                                        "initial_target_oid", "created_at"})

    def test_create_run_manifest_refuses_second_call(self):
        with repo() as (root, run):
            store = Persistence(root)
            s = HarnessState(objective="o", run_id="r")
            store.create_run_manifest(s, authorized_target_ref=ORIG, initial_target_oid=OID)
            with self.assertRaises(RunManifestError):
                store.create_run_manifest(s, authorized_target_ref=ORIG, initial_target_oid=OID)
            with self.assertRaises(RunManifestError):
                store.create_run_manifest(s, authorized_target_ref=OTHER, initial_target_oid=OID)

    def test_create_run_manifest_rejects_bad_oid(self):
        with repo() as (root, run):
            store = Persistence(root)
            s = HarnessState(objective="o", run_id="r2")
            with self.assertRaises(RunManifestError):
                store.create_run_manifest(s, authorized_target_ref=ORIG, initial_target_oid=None)
            self.assertFalse(store._manifest_path("r2").exists())

    def test_resume_path_never_creates_a_manifest(self):
        with repo() as (root, run):
            g, store, st, *_ = _authorized_reviewed_state(root, run)
            store._manifest_path("r").unlink()
            verify_run_authorization(st, store, g)
            self.assertFalse(store._manifest_path("r").exists())
            # and a full resume attempt still doesn't create one
            st.run_target_ref = OTHER
            _resume_full(root, st)
            self.assertFalse(store._manifest_path("r").exists())


class IntactManifestControlTests(unittest.TestCase):
    def test_valid_manifest_and_state_checkpoints_successfully(self):
        with repo() as (root, run):
            g, store, st, parent, tree, commit = _authorized_reviewed_state(root, run)
            other_before = run("rev-parse", OTHER).stdout.strip()
            final = _resume_full(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ORIG), commit)                    # authorized ref moved
            self.assertEqual(run("rev-parse", OTHER).stdout.strip(), other_before)  # nothing else


if __name__ == "__main__":
    unittest.main()
