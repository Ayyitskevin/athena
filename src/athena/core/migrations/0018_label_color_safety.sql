-- Existing installs may already have arbitrary label color strings from
-- before colors were validated. Keep only strict #RRGGBB values and reset
-- everything else to the default safe gray used by new labels.
UPDATE labels
SET color = '#6b7280'
WHERE color IS NULL
   OR length(color) != 7
   OR color NOT GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]';

UPDATE labels
SET color = lower(color);
