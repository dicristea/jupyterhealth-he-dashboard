# Open the dashboard from Medplum (synthetic data only)

This is a proof of concept. You open a **synthetic** patient in [Medplum](https://www.medplum.com) (an open-source FHIR server and EHR toolkit), click the dashboard under **Apps**, and the dashboard opens on that patient's wearable profile through a SMART on FHIR EHR launch.

> ⚠️ **Synthetic data only.** The app has no login and runs on `127.0.0.1`. Never seed Medplum from study data, and never map real MRNs to study IDs.

How the pieces fit:

```
Medplum Patient ──(identifier: SMART_MRN_SYSTEM | JH-DEMO07)──┐
   │ Apps tab → SmartAppLaunch                                  │
   ▼                                                            │
/ehr/launch?iss=…/fhir/R4/&launch=…  →  Medplum /oauth2/authorize (PKCE)
   ▼                                                            │
/ehr/callback?code=…  →  /oauth2/token (returns `patient`)  →  GET Patient
   ▼                                                            │
MRN JH-DEMO07 ──(data.synthetic_mrn)──▶ dashboard patient DEMO-07 ◀┘
```

Synthetic patients don't need a mapping file. Each `DEMO-07` has the fixed MRN `JH-DEMO07` (`data.synthetic_mrn`). `HE_MRN_MAP` is only read for study data, and only after IRB approval and authentication are in place.

Legend: ✅ checked against Medplum's public metadata or source code · ⚠️ not tested against a live Medplum project (no credentials were available when this was written).

## 1. Make the synthetic cohort

```bash
python scripts/generate_synthetic_data.py --participants 24 --seed 7
export HE_DATA_SOURCE=synthetic   # the app and the seeding script both read synthetic_data/synthetic_he.csv.gz
```

Use the same `HE_DATA_SOURCE` and `HE_SYNTHETIC_FILE` for the app and the seeding script, so both see the same `DEMO-xx` patients. If you skip this step, both fall back to the built-in 24-patient demo cohort. That only works when no study file is present.

## 2. Create a free Medplum project ⚠️

1. Sign up at <https://app.medplum.com/register> and create a project, e.g. `HE dashboard demo`.
2. Or self-host Medplum (`docker compose` from the Medplum repo). Then use your own base URL below instead of `https://api.medplum.com/`.

## 3. Register the dashboard as a ClientApplication ⚠️

In the Medplum app, go to **Admin → Project → Clients → New** and fill in:

| Field | Value |
|---|---|
| Name | `HE Clinician Dashboard` |
| Launch URI | `http://localhost:5050/ehr/launch` |
| Redirect URIs | `http://localhost:5050/ehr/callback` |
| Launch identifier systems | leave empty (or Patient → `https://jupyterhealth.org/fhir/identifier/mrn`; both work, see notes) |

Save it, then copy the client **ID** and **secret**.

**Use the same host everywhere.** Pick either `localhost` or `127.0.0.1`. The launch state lives in a browser cookie, so if Medplum opens `localhost` but redirects to `127.0.0.1`, the callback fails with "Launch state didn't match".

You can use this one client for both seeding (client credentials) and launching. If you'd rather keep the seeding secret separate, create a second client for seeding only.

## 4. Seed synthetic Patients

Preview the FHIR transaction Bundle first. This step makes no network calls:

```bash
python scripts/seed_medplum.py --dry-run --output /tmp/he_patients.json
```

Each entry is a `Patient` with:

- a made-up name
- `gender` and `birthDate` taken from the synthetic cohort
- the `HTEST` (test data) meta tag
- the identifier `{system: SMART_MRN_SYSTEM, value: JH-DEMOxx}`
- a conditional create (`ifNoneExist: identifier=<system>|<mrn>`)

Because of the conditional create, re-running the script doesn't create duplicates. The script refuses any cohort whose IDs aren't `DEMO-<n>`.

Send it:

```bash
export MEDPLUM_BASE_URL=https://api.medplum.com/   # default; change it for self-hosted
export MEDPLUM_CLIENT_ID=<client id>
export MEDPLUM_CLIENT_SECRET=<client secret>
python scripts/seed_medplum.py
# → 24 Patients sent to https://api.medplum.com/: 24 created, 0 already there or other (201 Created)
```

The script gets a token with `POST {base}oauth2/token` (`grant_type=client_credentials`, HTTP Basic), then sends `POST {base}fhir/R4` with the Bundle. ✅ Medplum lists `client_credentials` and `client_secret_basic` as supported. ⚠️ Not yet run against a live project: transaction plus `ifNoneExist` behaviour, and whether a new client's default access lets it create Patients.

## 5. Start the dashboard

```bash
export SMART_CLIENT_ID=<client id>
export SMART_CLIENT_SECRET=<client secret>                  # optional: PKCE is always used
export SMART_REDIRECT_URI=http://localhost:5050/ehr/callback  # must match the client's Redirect URI exactly
HE_DATA_SOURCE=synthetic python app.py
```

Open <http://localhost:5050/ehr> to check the settings. **Simulate launch** with `JH-DEMO01` tests the matching step without Medplum.

Default scopes are `launch openid fhirUser patient/Patient.rs`; override them with `SMART_SCOPES`. The `.rs` suffix (SMART v2 read + search) lets the app search by MRN when Medplum sends an identifier as `patient` (see notes). ✅ Medplum advertises `permission-v2` and `patient/*.rs`.

## 6. Launch ⚠️

1. In Medplum, open one of the seeded patients (search Patients by identifier, e.g. `JH-DEMO01`).
2. Open the **Apps** tab and click **HE Clinician Dashboard**.
3. Medplum may ask you to choose scopes. Accept them.
4. The dashboard opens on `DEMO-xx`. The green banner shows the MRN and a read-only summary of the FHIR Patient that came back (name, gender, birth date, `Patient/<id>`).

The **Recent launches** table on `/ehr` logs each attempt (in memory only).

## Notes on Medplum compatibility

Checked against `https://api.medplum.com/fhir/R4/.well-known/smart-configuration` and the Medplum source (Nov 2026):

- ✅ **Discovery.** The `/.well-known/smart-configuration` file under the FHIR base returns `authorization_endpoint` = `https://api.medplum.com/oauth2/authorize` and `token_endpoint` = `https://api.medplum.com/oauth2/token`, with PKCE `S256`, `launch-ehr` and `client-confidential-symmetric`.
- ✅ **`iss` has a trailing slash.** Medplum's `SmartAppLaunchLink` sends `iss=https://api.medplum.com/fhir/R4/`. The app strips the slash when it builds URLs.
- ✅ **`launch`** is the id of a `SmartAppLaunch` resource that Medplum creates on click. The app passes it back unchanged to `/authorize`.
- ✅ **The token response has `patient`.** By default it's the bare FHIR id. If the client sets **launch identifier systems**, Medplum sends that identifier's *value* instead (here, the MRN). The app tries `GET Patient/<patient>` first. On a 400 or 404 it falls back to `GET Patient?identifier=<SMART_MRN_SYSTEM>|<patient>`.
- ✅ **PKCE.** Medplum requires PKCE unless the client sets `pkceOptional`. When PKCE is used, the client secret isn't checked at the token endpoint, so `SMART_CLIENT_SECRET` is optional.
- ⚠️ **Not verified live:** the consent and scope screen, whether `aud` with a trailing slash is accepted, the `Apps` tab label in the current Medplum UI, and the end-to-end redirect.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "SMART_CLIENT_ID is not set" | Start the app with `SMART_CLIENT_ID`. |
| "Launch state didn't match" | `localhost` vs `127.0.0.1` mismatch, or the launch was started in another browser. |
| Medplum error about `redirect_uri` | `SMART_REDIRECT_URI` must equal one of the client's Redirect URIs exactly. |
| "No local patient matches MRN …" | The app and the seeding script used different cohorts, or the Patient has no identifier under `SMART_MRN_SYSTEM`. |
| Seeding returns 401 or 403 | Wrong client secret, or the client has no project access. Check **Admin → Project → Clients**. |
