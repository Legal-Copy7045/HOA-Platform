"""Review D6: once a vote opens, its rules, kind and secrecy are fixed."""

from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION governance_vote_freeze() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'draft' AND (
       NEW.kind IS DISTINCT FROM OLD.kind
    OR NEW.secret IS DISTINCT FROM OLD.secret
    OR NEW.quorum_percent IS DISTINCT FROM OLD.quorum_percent
    OR NEW.pass_percent IS DISTINCT FROM OLD.pass_percent
    OR NEW.eligible_lots IS DISTINCT FROM OLD.eligible_lots
    OR NEW.rules_citation IS DISTINCT FROM OLD.rules_citation
    OR NEW.opened_at IS DISTINCT FROM OLD.opened_at
  ) THEN
    RAISE EXCEPTION 'vote % is open or closed: its rules are fixed', OLD.id
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.status = 'closed' AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'vote % is closed and cannot reopen', OLD.id
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER governance_vote_freeze BEFORE UPDATE ON governance_vote
  FOR EACH ROW EXECUTE FUNCTION governance_vote_freeze();
"""

REVERSE = """
DROP TRIGGER IF EXISTS governance_vote_freeze ON governance_vote;
DROP FUNCTION IF EXISTS governance_vote_freeze();
"""


class Migration(migrations.Migration):
    dependencies = [("governance", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
