-- Other domains an outlet publishes on (outlets.yaml `also:`), space-separated.
ALTER TABLE outlets ADD COLUMN aliases TEXT NOT NULL DEFAULT '';
