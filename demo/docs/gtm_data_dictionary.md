# GTM contact data dictionary (demo)

Canonical fields every source is normalized to before it reaches the CRM.

| Field      | Description                          | Example                               |
| ---------- | ------------------------------------ | ------------------------------------- |
| first_name | Contact given name                   | Anna                                  |
| last_name  | Contact family name                  | Becker                                |
| email      | Work email, primary dedup key        | anna.becker@northwind-logistics.example |
| company    | Account legal name                   | Northwind Logistics GmbH              |
| phone      | Direct or mobile number, E.164-ish   | +49 30 754 2824                       |
| role       | Job title                            | Head of IT                            |

## Source field mappings

| Canonical  | CRM export CSV | Partner REST API | Website webhook |
| ---------- | -------------- | ---------------- | --------------- |
| first_name | first_name     | firstName        | given_name      |
| last_name  | last_name      | lastName         | family_name     |
| email      | email          | emailAddress     | work_email      |
| company    | company        | organization     | company_name    |
| phone      | phone          | mobile           | (not sent)      |
| role       | role           | jobTitle         | title           |

Duplicates are matched on lower-cased email and flagged for review, never silently dropped.
All people and companies in this demo are fictional.
