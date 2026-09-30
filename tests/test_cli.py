import json

from hermes.__main__ import main
from hermes.db import Database
from hermes.models import JobPosting, Status


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    db = Database(tmp_path / "hermes.db")
    ids = []
    for i, path in enumerate([(Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APROVADA),
                              (Status.AVALIADA, Status.AGUARDANDO_VOCE)]):
        j = db.add_job(JobPosting(source="gupy", external_id=str(i), title=f"Vaga {i}",
                                  company=f"Empresa {i}", url=f"https://x/{i}", description="desc"))
        for s in path:
            db.transition(j, s)
        ids.append(j)
    return db, ids


def test_fila_json_lists_only_approved_by_default(tmp_path, monkeypatch, capsys):
    db, (approved, waiting) = setup(tmp_path, monkeypatch)
    assert main(["fila", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [j["id"] for j in out] == [approved]
    assert out[0]["url"] == "https://x/0" and out[0]["descricao"] == "desc"
    main(["fila", "--json", "--todas"])
    assert {j["id"] for j in json.loads(capsys.readouterr().out)} == {approved, waiting}


def test_marcar_applies_and_records_decision(tmp_path, monkeypatch, capsys):
    db, (approved, _) = setup(tmp_path, monkeypatch)
    assert main(["marcar", str(approved), "aplicada", "--nota", "pelo Claude"]) == 0
    row = Database(tmp_path / "hermes.db").get(approved)
    assert row["status"] == Status.APLICADA and row["decision"] == "aprovou"
    assert "pelo Claude" in Database(tmp_path / "hermes.db").events(approved)[-1]["detail"]


def test_marcar_refuses_invalid_transition(tmp_path, monkeypatch, capsys):
    db, (_, waiting) = setup(tmp_path, monkeypatch)
    assert main(["marcar", str(waiting), "recusada"]) == 1
    assert "não mudei" in capsys.readouterr().err
