from django.db import migrations


DOCUMENT_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION fleetline_guard_document_organization()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    linked_organization uuid;
    prior_organization uuid;
    prior_asset uuid;
    prior_document_key uuid;
    prior_version integer;
    attachment_document_key uuid;
    attachment_version integer;
BEGIN
    IF TG_OP = 'UPDATE'
       AND (to_jsonb(NEW) - ARRAY[
            'status', 'processing_detail', 'security_review_reference',
            'security_review_note', 'security_reviewed_by_id',
            'security_reviewed_at', 'processed_at', 'updated_at'
       ]::text[])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
            'status', 'processing_detail', 'security_review_reference',
            'security_review_note', 'security_reviewed_by_id',
            'security_reviewed_at', 'processed_at', 'updated_at'
       ]::text[]) THEN
        RAISE EXCEPTION 'core_document source metadata is immutable; create a replacement revision'
            USING ERRCODE = '55000';
    END IF;

    SELECT organization_id, document_key, version
    INTO linked_organization, attachment_document_key, attachment_version
    FROM core_attachment
    WHERE id = NEW.attachment_id;
    IF linked_organization IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION 'core_document attachment organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM assets_asset
        WHERE id = NEW.asset_id AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'core_document asset organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM core_attachment
        WHERE id = NEW.attachment_id
          AND resource_type = 'asset'
          AND resource_id = NEW.asset_id::text
    ) THEN
        RAISE EXCEPTION 'core_document attachment target must match its source asset'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.security_reviewed_by_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM core_user
        WHERE id = NEW.security_reviewed_by_id
          AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'core_document reviewer organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.supersedes_id IS NOT NULL THEN
        SELECT document.organization_id, document.asset_id, attachment.document_key, attachment.version
        INTO prior_organization, prior_asset, prior_document_key, prior_version
        FROM core_document AS document
        JOIN core_attachment AS attachment ON attachment.id = document.attachment_id
        WHERE document.id = NEW.supersedes_id;
        IF prior_organization IS DISTINCT FROM NEW.organization_id
           OR prior_asset IS DISTINCT FROM NEW.asset_id
           OR prior_document_key IS DISTINCT FROM attachment_document_key
           OR attachment_version IS DISTINCT FROM prior_version + 1 THEN
            RAISE EXCEPTION 'core_document replacement must preserve organization, asset, and attachment lineage'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER core_document_organization_guard
BEFORE INSERT OR UPDATE ON core_document
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_document_organization();

CREATE OR REPLACE FUNCTION fleetline_guard_document_applicability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'core_documentapplicability is append-only; create a replacement document revision'
            USING ERRCODE = '55000';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM core_document
        WHERE id = NEW.document_id AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'document applicability organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.asset_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM assets_asset
        WHERE id = NEW.asset_id AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'document applicability asset organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.asset_type_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM assets_assettype
        WHERE id = NEW.asset_type_id AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'document applicability asset type organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER core_document_applicability_guard
BEFORE INSERT OR UPDATE OR DELETE ON core_documentapplicability
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_document_applicability();

CREATE OR REPLACE FUNCTION fleetline_guard_document_page_organization()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM core_document
        WHERE id = NEW.document_id AND organization_id = NEW.organization_id
    ) THEN
        RAISE EXCEPTION 'document page organization must match document organization'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER core_document_page_organization_guard
BEFORE INSERT ON core_documentpage
FOR EACH ROW EXECUTE FUNCTION fleetline_guard_document_page_organization();
"""


DOCUMENT_GUARDS_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS core_document_page_organization_guard ON core_documentpage;
DROP FUNCTION IF EXISTS fleetline_guard_document_page_organization();
DROP TRIGGER IF EXISTS core_document_applicability_guard ON core_documentapplicability;
DROP FUNCTION IF EXISTS fleetline_guard_document_applicability();
DROP TRIGGER IF EXISTS core_document_organization_guard ON core_document;
DROP FUNCTION IF EXISTS fleetline_guard_document_organization();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0007_document_documentapplicability_documentpage_and_more")]

    operations = [migrations.RunSQL(DOCUMENT_GUARDS_SQL, DOCUMENT_GUARDS_REVERSE_SQL)]
