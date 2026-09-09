BEGIN;

CREATE TABLE IF NOT EXISTS finance.lineage_review_group_members (
    lineage_group_id text NOT NULL
        REFERENCES finance.lineage_review_groups(lineage_group_id),
    member_identity_hash text NOT NULL
        CHECK (member_identity_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (lineage_group_id, member_identity_hash)
);

DROP TRIGGER IF EXISTS lineage_review_group_members_append_only
ON finance.lineage_review_group_members;
CREATE TRIGGER lineage_review_group_members_append_only
BEFORE UPDATE OR DELETE ON finance.lineage_review_group_members
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE OR REPLACE FUNCTION finance.validate_lineage_review_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM finance.lineage_review_groups group_record
        WHERE group_record.lineage_group_id = NEW.lineage_group_id
          AND group_record.candidate_hash = NEW.candidate_hash
          AND group_record.audit_graph_hash = NEW.audit_graph_hash
          AND group_record.evidence_set_hash = NEW.evidence_set_hash
    ) THEN
        RAISE EXCEPTION
            'lineage decision is not bound to the exact reviewed evidence'
            USING ERRCODE = '23514';
    END IF;
    IF (
        NEW.outcome = 'duplicate-economic-event'
        AND NOT EXISTS (
            SELECT 1
            FROM finance.lineage_review_group_members member
            WHERE member.lineage_group_id = NEW.lineage_group_id
              AND member.member_identity_hash = NEW.survivor_identity_hash
        )
    ) THEN
        RAISE EXCEPTION
            'duplicate survivor is not a member of the reviewed lineage group'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE VIEW finance_read.shadow_lineage_status AS
SELECT
    group_record.lineage_group_id,
    group_record.candidate_hash,
    group_record.audit_graph_hash,
    group_record.evidence_set_hash,
    group_record.member_count,
    issue.status AS issue_status,
    latest.outcome,
    latest.decision_version,
    latest.decided_at,
    CASE
        WHEN latest.lineage_decision_id IS NULL THEN 'review-required'
        WHEN latest.outcome = 'insufficient-evidence' THEN 'review-required'
        ELSE 'evidence-bound-decision'
    END AS review_status,
    (
        SELECT count(*)
        FROM finance.lineage_review_group_members member
        WHERE member.lineage_group_id = group_record.lineage_group_id
    ) AS persisted_member_count
FROM finance.lineage_review_groups group_record
JOIN finance.quality_issues issue
  ON issue.quality_issue_id = group_record.quality_issue_id
LEFT JOIN LATERAL (
    SELECT decision.*
    FROM finance.lineage_review_decisions decision
    WHERE decision.lineage_group_id = group_record.lineage_group_id
    ORDER BY decision.decision_version DESC
    LIMIT 1
) latest ON true;

GRANT SELECT ON finance_read.shadow_lineage_status TO finance_readonly;
GRANT SELECT, INSERT, UPDATE ON finance.lineage_review_group_members
TO finance_shadow_ingest;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER
ON finance.lineage_review_group_members
FROM finance_shadow_ingest;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
