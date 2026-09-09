BEGIN;

ALTER ROLE finance_readonly WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_ingest WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_backup WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_agent_readonly WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

DO $do$
DECLARE
    membership record;
BEGIN
    FOR membership IN
        SELECT granted.rolname AS granted_role,
               member.rolname AS member_role
        FROM pg_auth_members relation
        JOIN pg_roles granted ON granted.oid = relation.roleid
        JOIN pg_roles member ON member.oid = relation.member
        WHERE member.rolname IN (
            'finance_readonly',
            'finance_shadow_ingest',
            'finance_shadow_backup',
            'finance_shadow_agent_readonly'
        )
          AND NOT (
              (member.rolname = 'finance_shadow_ingest'
                  AND granted.rolname = 'finance_readonly')
              OR (member.rolname = 'finance_shadow_backup'
                  AND granted.rolname = 'pg_read_all_data')
              OR (member.rolname = 'finance_shadow_agent_readonly'
                  AND granted.rolname = 'finance_readonly')
          )
    LOOP
        EXECUTE format(
            'REVOKE %I FROM %I',
            membership.granted_role,
            membership.member_role
        );
    END LOOP;
END;
$do$;

GRANT finance_readonly TO finance_shadow_ingest;
GRANT finance_readonly TO finance_shadow_agent_readonly;
GRANT pg_read_all_data TO finance_shadow_backup;

CREATE OR REPLACE FUNCTION finance.freeze_decided_lineage_members()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM finance.lineage_review_decisions decision
        WHERE decision.lineage_group_id = NEW.lineage_group_id
    ) THEN
        RAISE EXCEPTION
            'decided lineage group membership is frozen'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS lineage_review_group_members_freeze_decided
ON finance.lineage_review_group_members;
CREATE TRIGGER lineage_review_group_members_freeze_decided
BEFORE INSERT ON finance.lineage_review_group_members
FOR EACH ROW EXECUTE FUNCTION finance.freeze_decided_lineage_members();

CREATE OR REPLACE FUNCTION finance.validate_lineage_review_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    expected_members integer;
    persisted_members integer;
BEGIN
    SELECT member_count
    INTO expected_members
    FROM finance.lineage_review_groups group_record
    WHERE group_record.lineage_group_id = NEW.lineage_group_id
      AND group_record.candidate_hash = NEW.candidate_hash
      AND group_record.audit_graph_hash = NEW.audit_graph_hash
      AND group_record.evidence_set_hash = NEW.evidence_set_hash;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'lineage decision is not bound to the exact reviewed evidence'
            USING ERRCODE = '23514';
    END IF;

    SELECT count(*)
    INTO persisted_members
    FROM finance.lineage_review_group_members member
    WHERE member.lineage_group_id = NEW.lineage_group_id;
    IF persisted_members <> expected_members THEN
        RAISE EXCEPTION
            'lineage decision requires the complete reviewed membership'
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

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
