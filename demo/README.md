# IntegrateHub demo data

A fictional GTM workspace for demos and screen recordings: European B2B contacts arriving from
four sources with different field names, overlapping contacts, a failing feed, and a team
reviewing duplicates. All people, companies and `.example` domains are made up.

## Run it

From the repository root (the folder with `app/` and `demo/`):

```powershell
pip install -r requirements.txt
python demo/seed_demo.py            # fills data/integratehub.db through the real API
uvicorn app.main:app --reload       # then open http://127.0.0.1:8000
```

Sign in as `veer@integratehub.example` with password `integratehub-demo`.
Teammates `lena.fischer@integratehub.example` (admin) and `aarav.mehta@integratehub.example`
(member) use the same demo password.

For a clean start, move `data/integratehub.db` aside first. The seeder never deletes anything and
refuses to run twice for the same owner unless you pass `--force`. Use `--no-spread` to keep real
timestamps instead of spreading records over the last 14 days.

## What gets created

| Source | File | Field style | Rows | Result |
| --- | --- | --- | --- | --- |
| CSV upload | `data/crm_export_contacts.csv` | canonical (`first_name`, `email`, …) | 24 | 23 new, 1 duplicate inside the file |
| CSV upload | `data/event_leads_berlin.csv` | canonical + `event` column | 14 | 10 new, 4 duplicates (one with different email casing) |
| REST connector | `data/partner_api_contacts.json` via `mock_partner_api.py` | `firstName`, `emailAddress`, `organization` | 12 | 7 new, 5 duplicates, mapped to canonical fields |
| REST connector | `/flaky` on the mock API | n/a | n/a | HTTP 503, so the connector shows as degraded with an error log |
| Signed webhooks | `data/webhook_events.json` | `given_name`, `work_email`, `company_name` | 8 | 6 new, 2 duplicates; one tampered event rejected with 401 |

The totals are 58 records and 12 duplicates flagged, across 4 connectors and 3 library documents.
The seeder also creates:

- 4 comments with @mentions on duplicate records
- 2 saved views
- audit events for a member add, a role change, a secret rotation and a webhook replay record

## Live moments for a recording

- **Mapping assistant:** on Connectors, paste `data/mapping_sample.txt` into the AI mapping box and
  click Suggest mapping.
- **Re-sync and dedup:** run `python demo/mock_partner_api.py`, then click Sync on
  "Partner API · contacts". All 12 records come back flagged as duplicates.
- **Webhook security:** with the server running, run `python demo/send_webhook.py`. It sends a
  signed event (accepted), a tampered body (401) and a replay (flagged as a duplicate).

`demo/.demo_state.json` holds the local demo webhook secret and connector IDs for these helpers.
It is ignored by git.
