from kbw.cli import _exclude_codegraph
from kbw.config import downstream_env, load_config
from kbw import examples
from kbw.util import git


def test_local_index_exclude_is_idempotent(tmp_path):
    examples.create(tmp_path, tmp_path / "kbw.yaml")
    cfg = load_config(str(tmp_path / "kbw.yaml"))
    repo = cfg.repos[0].path
    _exclude_codegraph(repo)
    _exclude_codegraph(repo)
    generated = repo / ".codegraph"
    generated.mkdir()
    (generated / ".gitignore").write_text("*\n!.gitignore\n")
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "check-ignore", ".codegraph/.gitignore") == ".codegraph/.gitignore"
    assert not (repo / ".gitignore").exists()


def test_upstream_runtime_never_downloads():
    env = downstream_env()
    assert env["CODEGRAPH_NO_DOWNLOAD"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
