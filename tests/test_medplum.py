import importlib.util
import json
import os
import urllib.error
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

os.environ["HE_DATA_FILE"] = "/nonexistent/no-data-here.rds"  # force the synthetic cohort

import data  # noqa: E402
from app import app  # noqa: E402

_spec = importlib.util.spec_from_file_location("seed_medplum", Path(__file__).resolve().parents[1] / "scripts" / "seed_medplum.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("tests must not touch the network")
    monkeypatch.setattr("urllib.request.urlopen", refuse)


def _cohort(n: int = 3) -> pd.DataFrame:
    return data.synthetic_cohort(n=n, seed=1, min_days=1, max_days=1)[["name", "sex", "age_yrs"]]


def test_bundle_is_conditional_transaction_keyed_on_synthetic_mrn():
    bundle = seed.build_bundle(_cohort())
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "transaction" and len(bundle["entry"]) == 3
    for e, pid in zip(bundle["entry"], ["DEMO-01", "DEMO-02", "DEMO-03"]):
        mrn = data.synthetic_mrn(pid)
        ident = e["resource"]["identifier"][0]
        assert (ident["system"], ident["value"]) == (data.MRN_SYSTEM, mrn)
        assert e["request"] == {"method": "POST", "url": "Patient", "ifNoneExist": f"identifier={data.MRN_SYSTEM}|{mrn}"}
        assert e["fullUrl"].startswith("urn:uuid:") and e["resource"]["meta"]["tag"][0]["code"] == "HTEST"
        assert e["resource"]["gender"] in {"female", "male"} and e["resource"]["name"][0]["family"]
    assert seed.build_bundle(_cohort()) == bundle  # deterministic, so re-runs are idempotent


def test_seed_refuses_non_synthetic_ids(tmp_path):
    f = tmp_path / "cohort.csv"
    pd.DataFrame({"name": ["DEMO-01", "P123"], "sex": ["Male", "Female"], "age_yrs": [50, 60]}).to_csv(f, index=False)
    with pytest.raises(SystemExit, match="not synthetic"):
        seed.load_cohort(f)


def test_dry_run_writes_bundle_without_network(tmp_path, capsys):
    f, out = tmp_path / "cohort.csv", tmp_path / "bundle.json"
    _cohort(4).to_csv(f, index=False)
    seed.main(["--synthetic-file", str(f), "--dry-run", "--output", str(out)])
    assert len(json.loads(out.read_text())["entry"]) == 4
    seed.main(["--synthetic-file", str(f), "--dry-run"])
    assert json.loads(capsys.readouterr().out)["type"] == "transaction"


def test_seed_uses_client_credentials(tmp_path, monkeypatch, capsys):
    f = tmp_path / "cohort.csv"
    _cohort(2).to_csv(f, index=False)
    calls = []

    def fake_post(url, body, headers):
        calls.append((url, headers))
        if url.endswith("/oauth2/token"):
            assert urllib.parse.parse_qs(body.decode()) == {"grant_type": ["client_credentials"]}
            return {"access_token": "tok"}
        assert json.loads(body)["type"] == "transaction"
        return {"entry": [{"response": {"status": "201 Created"}}, {"response": {"status": "200 OK"}}]}

    monkeypatch.setattr(seed, "_post", fake_post)
    monkeypatch.setenv("MEDPLUM_BASE_URL", "https://medplum.example")
    monkeypatch.setenv("MEDPLUM_CLIENT_ID", "cid")
    monkeypatch.setenv("MEDPLUM_CLIENT_SECRET", "sec")
    seed.main(["--synthetic-file", str(f)])
    assert [u for u, _ in calls] == ["https://medplum.example/oauth2/token", "https://medplum.example/fhir/R4"]
    assert calls[0][1]["Authorization"].startswith("Basic ") and calls[1][1]["Authorization"] == "Bearer tok"
    assert "1 created" in capsys.readouterr().out


def test_synthetic_mrns_resolve_without_a_map_file(monkeypatch, tmp_path):
    f = tmp_path / "map.csv"
    pd.DataFrame({"mrn": ["JH-DEMO01"], "study_id": ["DEMO-02"]}).to_csv(f, index=False)
    monkeypatch.setenv("HE_MRN_MAP", str(f))  # ignored for synthetic data
    for pid in data.load()[0]["name"].unique():
        assert data.resolve_mrn(data.synthetic_mrn(pid)) == pid
    assert data.resolve_mrn("JH-NOPE") is None
    # every seeded Patient opens a dashboard patient
    for e in seed.build_bundle(seed.load_cohort())["entry"]:
        assert data.resolve_mrn(e["resource"]["identifier"][0]["value"])


def test_study_data_only_maps_through_mrn_file(monkeypatch, tmp_path):
    fake = pd.DataFrame({"name": ["S-001", "S-002"]})  # stand-in IDs, not study rows
    monkeypatch.setattr(data, "load", lambda: (fake, "study"))
    monkeypatch.delenv("HE_MRN_MAP", raising=False)
    assert data.resolve_mrn(data.synthetic_mrn("S-001")) is None  # no synthetic MRNs for study participants
    f = tmp_path / "map.csv"
    pd.DataFrame({"mrn": ["111", "222"], "study_id": ["S-002", "S-999"]}).to_csv(f, index=False)
    monkeypatch.setenv("HE_MRN_MAP", str(f))
    assert data.resolve_mrn("111") == "S-002"
    assert data.resolve_mrn("222") is None  # mapped ID not in the data
    assert data.resolve_mrn(data.synthetic_mrn("S-001")) is None


def _medplum_launch(monkeypatch, token_patient: str, fhir_patient: dict, by_id_ok: bool = True):
    import app as app_module
    iss = "https://api.medplum.com/fhir/R4/"  # Medplum sends iss with a trailing slash
    seen = []

    def fake_http(url, form=None, token=None):
        seen.append(url)
        if url == "https://api.medplum.com/fhir/R4/.well-known/smart-configuration":
            return {"authorization_endpoint": "https://api.medplum.com/oauth2/authorize",
                    "token_endpoint": "https://api.medplum.com/oauth2/token"}
        if url == "https://api.medplum.com/oauth2/token":
            return {"access_token": "tok", "token_type": "Bearer", "patient": token_patient, "project": {}}
        assert token == "tok"
        if "/Patient/" in url:
            if by_id_ok:
                return fhir_patient
            raise urllib.error.HTTPError(url, 404, "Not found", {}, None)
        return {"resourceType": "Bundle", "entry": [{"resource": fhir_patient}]}

    monkeypatch.setattr(app_module, "_http_json", fake_http)
    monkeypatch.setitem(app_module.SMART, "client_id", "medplum-client")
    client = app.test_client()
    res = client.get(f"/ehr/launch?iss={urllib.parse.quote(iss)}&launch=launch-1")
    assert res.headers["Location"].startswith("https://api.medplum.com/oauth2/authorize?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(res.headers["Location"]).query)
    return client, client.get(f"/ehr/callback?code=c&state={q['state'][0]}"), seen


def test_medplum_launch_with_seeded_patient(monkeypatch):
    pid = data.patient_list()[0]["id"]
    resource = {**seed.patient_resource(pid, "Female", 50), "id": "0b6f-uuid"}
    client, res, seen = _medplum_launch(monkeypatch, "0b6f-uuid", resource)
    assert res.headers["Location"].endswith(f"/patient/{pid}")
    assert "https://api.medplum.com/fhir/R4/Patient/0b6f-uuid" in seen
    page = client.get(f"/patient/{pid}").data.decode()
    assert "EHR record:" in page and resource["name"][0]["family"] in page and "Patient/0b6f-uuid" in page


def test_medplum_launch_identifier_in_token(monkeypatch):
    # With launchIdentifierSystems set, Medplum's token `patient` is the MRN, not the FHIR id.
    pid = data.patient_list()[1]["id"]
    mrn = data.synthetic_mrn(pid)
    client, res, seen = _medplum_launch(monkeypatch, mrn, {**seed.patient_resource(pid, "Male", 60), "id": "x"},
                                        by_id_ok=False)
    assert res.headers["Location"].endswith(f"/patient/{pid}")
    assert seen[-1].endswith("/Patient?" + urllib.parse.urlencode({"identifier": f"{data.MRN_SYSTEM}|{mrn}"}))
