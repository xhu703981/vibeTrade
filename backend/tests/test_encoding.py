"""Windows regression: text files must be UTF-8 whatever the OS code page.

On Xinrui's PC (cp1252) a token name with an emoji made research/cache.py write_text() raise UnicodeEncodeError after
truncating the cache file; the next round read the empty file and died with JSONDecodeError."""
from __future__ import annotations

import ast
import io
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _calls_without_encoding(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        kw = {k.arg for k in n.keywords}
        if name in ("read_text", "write_text") and "encoding" not in kw:
            bad.append(f"{path.relative_to(ROOT)}:{n.lineno} {name}()")
        elif name == "open" and "encoding" not in kw:
            idx = 1 if isinstance(f, ast.Name) else 0
            mode = n.args[idx].value if len(n.args) > idx and isinstance(n.args[idx], ast.Constant) else None
            mode = next((k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), mode)
            if not (isinstance(mode, str) and "b" in mode):
                bad.append(f"{path.relative_to(ROOT)}:{n.lineno} open()")
        elif name in ("run", "Popen", "check_output") and ({"text", "universal_newlines"} & kw) and "encoding" not in kw:
            bad.append(f"{path.relative_to(ROOT)}:{n.lineno} {name}(text=True)")
    return bad


def test_every_text_read_and_write_names_utf8():
    files = [p for d in ("backend", "scripts") for p in (ROOT / d).rglob("*.py") if p.name != "test_encoding.py"]   # this file breaks it on purpose
    bad = [b for p in files for b in _calls_without_encoding(p)]
    assert not bad, "text I/O without encoding= (breaks on Windows code pages):\n" + "\n".join(bad)


def test_dev_launchers_run_python_in_utf8_mode():
    ps1 = (ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
    assert "$env:PYTHONUTF8 = '1'" in ps1 and "$env:PYTHONIOENCODING = 'utf-8'" in ps1
    assert ps1.index("PYTHONUTF8") < ps1.index("'uvicorn'")
    assert "PYTHONUTF8=1" in sh and "PYTHONIOENCODING=utf-8" in sh
    assert ps1.isascii()   # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page


@pytest.fixture
def cp1252_default(monkeypatch):
    """Make pathlib's default text encoding cp1252, as on a Western-European Windows (encoding=None -> cp1252)."""
    real = io.text_encoding
    monkeypatch.setattr(io, "text_encoding", lambda enc, stacklevel=2: real(enc) if enc is not None else "cp1252")
    yield


def test_cp1252_simulation_really_breaks_without_encoding(tmp_path, cp1252_default):
    with pytest.raises(UnicodeEncodeError):
        (tmp_path / "x.json").write_text("\U0001f331")


def test_research_cache_round_trips_emoji_under_cp1252(tmp_path, monkeypatch, cp1252_default):
    from backend.research import cache
    monkeypatch.setattr(cache, "ROOT", tmp_path)
    data = [{"token_symbol": "\U0001f331SEED", "name": "\u79cd\u5b50"}]
    cache.put("nansen_smart_money", "solana-netflow-24h", data, "0xabc", 0.05)
    got = cache.get("nansen_smart_money", "solana-netflow-24h")
    assert got and got["data"] == data
    assert not list(tmp_path.glob("*.tmp"))


def test_research_cache_treats_a_broken_file_as_a_miss(tmp_path, monkeypatch):
    from backend.research import cache
    monkeypatch.setattr(cache, "ROOT", tmp_path)
    (tmp_path / "nansen_smart_money_solana-netflow-24h.json").write_text("", encoding="utf-8")     # what the crash left
    assert cache.get("nansen_smart_money", "solana-netflow-24h") is None
    (tmp_path / "s_m.json").write_bytes(b"\xff\xfe{")
    assert cache.get("s", "m") is None
    (tmp_path / "s_n.json").write_text(json.dumps([1]), encoding="utf-8")
    assert cache.get("s", "n") is None


def test_background_failure_logs_traceback_and_speaks_plainly(capsys):
    from backend.orchestrator import log_traceback
    try:
        "\U0001f331".encode("cp1252")
    except UnicodeEncodeError as e:
        log_traceback("research round", e)
    err = capsys.readouterr().err
    assert "background research round failed" in err and "Traceback" in err and "UnicodeEncodeError" in err
    src = (ROOT / "backend" / "orchestrator.py").read_text(encoding="utf-8")
    assert "Research hit an error on my side and stopped. Nothing was bought." in src
    assert "encoding" not in "Research hit an error on my side and stopped. Nothing was bought."
