-- Validation queries for turn metadata backfill.
-- Run after backfill_turn_metadata.py to confirm data integrity.
--
-- Usage (PostgreSQL):
--   psql -d drowai -f backend/scripts/validate_turn_metadata.sql
--
-- Expected: no rows with turn_id IS NULL; turn numbers sequential per task;
-- turn boundaries and first event per turn as expected.

-- 1. Verify all events have turn metadata (should return 0)
SELECT COUNT(*) AS events_without_turn_id
FROM agent_logs
WHERE turn_id IS NULL;

-- 2. Verify turn numbers sequential per task
SELECT task_id, turn_number, COUNT(*) AS event_count
FROM agent_logs
GROUP BY task_id, turn_number
ORDER BY task_id, turn_number;

-- 3. Turn boundaries: min/max sequence and count per turn
SELECT task_id, turn_id, MIN(sequence) AS min_seq, MAX(sequence) AS max_seq, COUNT(*) AS event_count
FROM agent_logs
GROUP BY task_id, turn_id
ORDER BY task_id, MIN(sequence);

-- 4. Verify first event per turn is user_message (should be empty if all turns start with user_message)
-- Uses distinct on to get the first event per turn by sequence
SELECT DISTINCT ON (turn_id) turn_id, sequence, type
FROM agent_logs
ORDER BY turn_id, sequence
LIMIT 20;

-- 5. Turns where the first event (by sequence) is NOT user_message (investigate if non-empty)
WITH first_per_turn AS (
    SELECT DISTINCT ON (turn_id) turn_id, sequence, type
    FROM agent_logs
    ORDER BY turn_id, sequence
)
SELECT * FROM first_per_turn
WHERE type != 'user_message';
