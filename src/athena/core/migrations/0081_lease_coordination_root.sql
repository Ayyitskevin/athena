-- Root bindings are possession-bound evidence for declared work, not authority.
-- Existing rows remain unresolved; never infer topology from project or branch.
-- Forward-only: retain all previous migration bytes and lease generations.
ALTER TABLE issue_leases ADD COLUMN coordination_root TEXT;
