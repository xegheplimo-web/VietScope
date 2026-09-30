"""CLI smoke tests (mock mode, writes reports into a tmp dir)."""

import json

from eval.cli import main


def _run(argv):
    return main(argv)


def test_cli_list_datasets(capsys):
    assert _run(["list-datasets"]) == 0
    out = capsys.readouterr().out
    assert "vi_general" in out
    assert "current_events" in out


def test_cli_run_mock_writes_report(tmp_path, capsys):
    out_dir = tmp_path / "reports"
    rc = _run(
        [
            "run",
            "--dataset",
            "vi_general",
            "--top-k",
            "5",
            "--mock",
            "--out-dir",
            str(out_dir),
            "--note",
            "cli smoke",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Report written:" in out

    files = list(out_dir.glob("vi_general_*.json"))
    assert len(files) == 1
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["meta"]["force_mock"] is True
    assert report["meta"]["note"] == "cli smoke"
    assert report["summary"]["queries_evaluated"] == 15
    assert "ndcg@10" in report["summary"]


def test_cli_run_unknown_dataset(tmp_path, capsys):
    rc = _run(["run", "--dataset", "does_not_exist", "--mock"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Dataset 'does_not_exist' not found" in err


def test_cli_run_baseline_diff(tmp_path, capsys):
    out_dir = tmp_path / "reports"
    rc1 = _run(
        [
            "run",
            "--dataset",
            "vi_general",
            "--top-k",
            "5",
            "--mock",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc1 == 0
    baseline_file = list(out_dir.glob("vi_general_*.json"))[0]

    rc2 = _run(
        [
            "run",
            "--dataset",
            "vi_general",
            "--top-k",
            "5",
            "--mock",
            "--out-dir",
            str(out_dir),
            "--baseline",
            str(baseline_file),
        ]
    )
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "Baseline diff" in out
    assert "ndcg@10" in out
    assert len(list(out_dir.glob("vi_general_*.json"))) == 2
