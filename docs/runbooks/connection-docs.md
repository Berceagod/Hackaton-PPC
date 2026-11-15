# Runbook: connection documents (personal data protected and auditable)

## PersonalDataStoredUnencrypted
**Means:** at least one `connection_docs` file is stored without encryption: someone with direct access to the storage could read personal data. This must always be 0.

1. Check the policy: `GET /api/v1/admin/policies`; `connection_docs.encryption` must be `true`. If it is false, someone changed it: the audit trail of policy changes is in the backend log (`policies updated`, with the user).
2. Switch it back on (admin): the *Politici Rutare* page or `PUT /api/v1/admin/policies`. New uploads are encrypted again at once.
3. Find the files stored unencrypted: `docker compose exec postgres-meta psql -U filegrid -d filegrid -c "select id, filename, created_at, uploaded_by from files_metadata where category='connection_docs' and not encrypted"`.
4. Re-protect them: download each through the app and upload it again (it is stored encrypted), then remove the unencrypted original with the storage administrator. The gauge returns to 0 when none is left.
5. Decide whether this is a reportable incident with the data-protection officer: the audit trail shows who downloaded each of these files (`GET /api/v1/files/<id>/audit`), and `GET /api/v1/audit/verify` proves the trail was not altered.
