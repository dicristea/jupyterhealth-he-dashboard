"""Create a FHIR Patient in Medplum for every synthetic dashboard patient, so the SMART launch has someone to open.

Each Patient gets a made-up name and the synthetic MRN (data.synthetic_mrn) under SMART_MRN_SYSTEM, which is how
the dashboard matches a launch to DEMO-xx. Creates are conditional (ifNoneExist on the MRN), so re-running is safe.
Synthetic cohort only: the script refuses any patient ID that isn't DEMO-<n>.

    python scripts/seed_medplum.py --dry-run                      # print the transaction Bundle, no network
    MEDPLUM_CLIENT_ID=… MEDPLUM_CLIENT_SECRET=… python scripts/seed_medplum.py
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import data  # noqa: E402

SYNTHETIC_ID = re.compile(r"DEMO-(\d+)")
GIVEN = ["Ada", "Ben", "Cleo", "Dev", "Esme", "Finn", "Gita", "Hugo", "Iris", "Jude", "Kai", "Lena", "Milo"]
FAMILY = ["Testpatient", "Synthetica", "Demoson", "Fakeley", "Mockford", "Sampleton", "Placeholder"]
BIRTH_YEAR_REF = 2026  # fixed so ages → birth dates stay deterministic


def load_cohort(path: Path | None = None) -> pd.DataFrame:
    """One row per synthetic patient (name, sex, age_yrs), from the same source the app uses."""
    if path is None and data.DATA_SOURCE == "synthetic":
        path = data.SYNTHETIC_FILE
    if path is not None:
        df = pd.read_csv(path, usecols=["name", "sex", "age_yrs"], dtype={"name": str})
    else:
        df = data.synthetic_cohort()[["name", "sex", "age_yrs"]]
    df = df.drop_duplicates("name").sort_values("name").reset_index(drop=True)
    bad = df.loc[~df["name"].str.fullmatch(SYNTHETIC_ID.pattern, na=False), "name"]
    if len(bad):
        raise SystemExit(f"Refusing to seed: {len(bad)} patient IDs are not synthetic DEMO-<n> IDs.")
    return df


def patient_resource(pid: str, sex: str | None, age: float | None) -> dict:
    n = int(SYNTHETIC_ID.fullmatch(pid).group(1))
    mrn = data.synthetic_mrn(pid)
    out = {
        "resourceType": "Patient",
        "meta": {"tag": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": "HTEST",
                          "display": "test health data"}]},
        "identifier": [{"use": "usual", "system": data.MRN_SYSTEM, "value": mrn,
                        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]}}],
        "name": [{"use": "official", "family": FAMILY[n % len(FAMILY)], "given": [GIVEN[n % len(GIVEN)]]}],
    }
    if isinstance(sex, str) and sex.lower() in {"female", "male"}:
        out["gender"] = sex.lower()
    if pd.notna(age):
        out["birthDate"] = f"{BIRTH_YEAR_REF - int(age)}-{n % 12 + 1:02d}-{n % 28 + 1:02d}"
    return out


def build_bundle(cohort: pd.DataFrame) -> dict:
    entries = []
    for row in cohort.itertuples(index=False):
        mrn = data.synthetic_mrn(row.name)
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{data.MRN_SYSTEM}|{mrn}')}",
            "resource": patient_resource(row.name, row.sex, row.age_yrs),
            "request": {"method": "POST", "url": "Patient", "ifNoneExist": f"identifier={data.MRN_SYSTEM}|{mrn}"},
        })
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def _post(url: str, body: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read())


def medplum_token(base_url: str, client_id: str, client_secret: str) -> str:
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    tok = _post(urllib.parse.urljoin(base_url, "oauth2/token"),
                urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                {"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
    return tok["access_token"]


def post_bundle(base_url: str, token: str, bundle: dict) -> dict:
    return _post(urllib.parse.urljoin(base_url, "fhir/R4"), json.dumps(bundle).encode(),
                 {"Authorization": f"Bearer {token}", "Content-Type": "application/fhir+json"})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--synthetic-file", type=Path, default=None,
                        help="synthetic cohort CSV (default: HE_SYNTHETIC_FILE when HE_DATA_SOURCE=synthetic, "
                             "else the in-memory demo cohort)")
    parser.add_argument("--dry-run", action="store_true", help="write the Bundle instead of sending it")
    parser.add_argument("--output", type=Path, help="with --dry-run: write the Bundle here instead of stdout")
    args = parser.parse_args(argv)

    bundle = build_bundle(load_cohort(args.synthetic_file))
    if args.dry_run:
        text = json.dumps(bundle, indent=2)
        if args.output:
            args.output.write_text(text + "\n")
            print(f"Wrote {len(bundle['entry'])} Patients to {args.output}", file=sys.stderr)
        else:
            print(text)
        return

    base_url = os.environ.get("MEDPLUM_BASE_URL", "https://api.medplum.com/").rstrip("/") + "/"
    client_id, secret = os.environ.get("MEDPLUM_CLIENT_ID"), os.environ.get("MEDPLUM_CLIENT_SECRET")
    if not (client_id and secret):
        raise SystemExit("Set MEDPLUM_CLIENT_ID and MEDPLUM_CLIENT_SECRET (or use --dry-run).")
    result = post_bundle(base_url, medplum_token(base_url, client_id, secret), bundle)
    statuses = [e.get("response", {}).get("status", "?") for e in result.get("entry", [])]
    created = sum(s.startswith("201") for s in statuses)
    print(f"{len(statuses)} Patients sent to {base_url}: {created} created, {len(statuses) - created} already there or other "
          f"({', '.join(sorted(set(statuses)))})")


if __name__ == "__main__":
    main()
