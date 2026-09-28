from django.db import migrations

DOCUMENT_WORKFLOW_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_document_workflow()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    attachment_document_key uuid;
    attachment_version integer;
    attachment_supersedes uuid;
    prior_attachment uuid;
    prior_document_key uuid;
    prior_version integer;
BEGIN
    SELECT document_key, version, supersedes_id
    INTO attachment_document_key, attachment_version, attachment_supersedes
    FROM core_attachment
    WHERE id = NEW.attachment_id;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'quarantined' THEN
            RAISE EXCEPTION 'core_document must begin quarantined' USING ERRCODE = '55000';
        END IF;
        IF COALESCE(NEW.security_review_reference, '') <> ''
           OR COALESCE(NEW.security_review_note, '') <> ''
           OR NEW.security_reviewed_by_id IS NOT NULL
           OR NEW.security_reviewed_at IS NOT NULL
           OR COALESCE(NEW.processing_detail, '') <> ''
           OR NEW.processed_at IS NOT NULL THEN
            RAISE EXCEPTION 'core_document review and processing fields must begin empty'
                USING ERRCODE = '55000';
        END IF;
        IF NEW.supersedes_id IS NULL THEN
            IF attachment_version <> 1 OR attachment_supersedes IS NOT NULL THEN
                RAISE EXCEPTION 'root technical document requires a root attachment version'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            SELECT document.attachment_id, attachment.document_key, attachment.version
            INTO prior_attachment, prior_document_key, prior_version
            FROM core_document AS document
            JOIN core_attachment AS attachment ON attachment.id = document.attachment_id
            WHERE document.id = NEW.supersedes_id;
            IF prior_attachment IS NULL
               OR attachment_supersedes IS DISTINCT FROM prior_attachment
               OR attachment_document_key IS DISTINCT FROM prior_document_key
               OR attachment_version IS DISTINCT FROM prior_version + 1 THEN
                RAISE EXCEPTION 'technical document replacement must supersede its prior attachment'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF (
        NEW.security_review_reference,
        NEW.security_review_note,
        NEW.security_reviewed_by_id,
        NEW.security_reviewed_at
    ) IS DISTINCT FROM (
        OLD.security_review_reference,
        OLD.security_review_note,
        OLD.security_reviewed_by_id,
        OLD.security_reviewed_at
    ) AND NOT (OLD.status = 'quarantined' AND NEW.status = 'queued') THEN
        RAISE EXCEPTION 'core_document review evidence is immutable after approval'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'quarantined' AND NEW.status = 'queued' THEN
            IF COALESCE(NEW.security_review_reference, '') = ''
               OR COALESCE(NEW.security_review_note, '') = ''
               OR NEW.security_reviewed_by_id IS NULL
               OR NEW.security_reviewed_at IS NULL
               OR NEW.processed_at IS NOT NULL THEN
                RAISE EXCEPTION 'core_document approval requires complete review evidence'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF OLD.status = 'queued' AND NEW.status = 'processing' THEN
            IF NEW.processed_at IS NOT NULL THEN
                RAISE EXCEPTION 'core_document cannot be processed before extraction completes'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF OLD.status = 'processing'
              AND NEW.status IN ('indexed', 'ocr_unavailable', 'needs_review', 'failed') THEN
            IF NEW.processed_at IS NULL THEN
                RAISE EXCEPTION 'core_document terminal processing state requires processed_at'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'core_document status transition is not allowed'
                USING ERRCODE = '55000';
        END IF;
    ELSIF (
        NEW.processing_detail,
        NEW.processed_at
    ) IS DISTINCT FROM (
        OLD.processing_detail,
        OLD.processed_at
    ) THEN
        RAISE EXCEPTION 'core_document processing fields change only with an allowed transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER core_document_workflow_guard
BEFORE INSERT OR UPDATE ON core_document
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_document_workflow();
"""


DOCUMENT_WORKFLOW_GUARDS_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS core_document_workflow_guard ON core_document;
DROP FUNCTION IF EXISTS fleetline_guard_document_workflow();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0008_document_organization_and_metadata_guards")]

    operations = [
        migrations.RunSQL(DOCUMENT_WORKFLOW_GUARDS_SQL, DOCUMENT_WORKFLOW_GUARDS_REVERSE_SQL)
    ]
