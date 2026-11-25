"""Once voting opens, the ballot is fixed in the database too (SEC-001):
candidates cannot be added, accepted, withdrawn or swapped, and the number of
seats cannot change. The services refuse first; this is the backstop."""

from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION governance_freeze_candidates() RETURNS trigger AS $$
DECLARE st text;
BEGIN
    SELECT state INTO st FROM governance_election WHERE id = NEW.election_id;
    IF st IS DISTINCT FROM 'nominations' AND (
        TG_OP = 'INSERT'
        OR NEW.accepted IS DISTINCT FROM OLD.accepted
        OR NEW.withdrawn IS DISTINCT FROM OLD.withdrawn
        OR NEW.person_id IS DISTINCT FROM OLD.person_id
        OR NEW.election_id IS DISTINCT FROM OLD.election_id
    ) THEN
        RAISE EXCEPTION 'The ballot is fixed once voting opens';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS governance_candidate_frozen ON governance_candidate;
CREATE TRIGGER governance_candidate_frozen BEFORE INSERT OR UPDATE ON governance_candidate
    FOR EACH ROW EXECUTE FUNCTION governance_freeze_candidates();

CREATE OR REPLACE FUNCTION governance_freeze_seats() RETURNS trigger AS $$
BEGIN
    IF OLD.state <> 'nominations' AND NEW.seats IS DISTINCT FROM OLD.seats THEN
        RAISE EXCEPTION 'Seats are fixed once voting opens';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS governance_election_seats_frozen ON governance_election;
CREATE TRIGGER governance_election_seats_frozen BEFORE UPDATE ON governance_election
    FOR EACH ROW EXECUTE FUNCTION governance_freeze_seats();
"""

REVERSE = """
DROP TRIGGER IF EXISTS governance_candidate_frozen ON governance_candidate;
DROP FUNCTION IF EXISTS governance_freeze_candidates();
DROP TRIGGER IF EXISTS governance_election_seats_frozen ON governance_election;
DROP FUNCTION IF EXISTS governance_freeze_seats();
"""


class Migration(migrations.Migration):
    dependencies = [("governance", "0006_elections_proxies_consent")]

    operations = [migrations.RunSQL(SQL, REVERSE)]
