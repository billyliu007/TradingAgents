"""Ticker file cache parsing (see ``service.tickers``)."""

import json
from pathlib import Path

import service.tickers as tickers


def test_read_tickers_file_dict_format(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tickers, "_MIN_FILE_PAIRS", 1)
    p = tmp_path / "t.json"
    p.write_text(
        json.dumps({"version": 1, "pairs": [["AAA", "Alpha Inc"], ["BBB", "Beta LLC"]]}),
        encoding="utf-8",
    )
    got = tickers.read_tickers_file(p)
    assert got == [("AAA", "Alpha Inc"), ("BBB", "Beta LLC")]


def test_read_tickers_file_bare_array(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tickers, "_MIN_FILE_PAIRS", 1)
    p = tmp_path / "t.json"
    p.write_text(json.dumps([["ZZ", "Zed Co"]]), encoding="utf-8")
    assert tickers.read_tickers_file(p) == [("ZZ", "Zed Co")]


def test_read_tickers_file_missing(tmp_path: Path) -> None:
    assert tickers.read_tickers_file(tmp_path / "nope.json") is None


def test_read_tickers_file_too_few_pairs(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"pairs": [["Z", "z"]]}), encoding="utf-8")
    assert tickers.read_tickers_file(p) is None


def test_tickers_file_path_default_endswith_json(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_TICKERS_FILE", raising=False)
    path = tickers.tickers_file_path()
    assert path.name == "us_tickers.json"
    assert path.parent.name == "data"
