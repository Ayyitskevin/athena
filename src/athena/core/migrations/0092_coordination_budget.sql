-- 0092_coordination_budget: a second budget counter, for coordination writes
-- (requirement C7, decision D6).
-- FORWARD-ONLY: once applied anywhere, never edit this file -- add a new one.
--
-- 0062's budget meters the writes that change the work: an issue created or
-- edited, a page created or edited, a dispatch requested. Comments, claims and
-- delegations were deliberately left out, so an agent with a budget of 3 an
-- hour could still post any number of comments, and a looping agent could
-- fill an issue's history without ever being refused.
--
-- They are counted apart, not against the same number. A seat that spent its
-- action budget must still be able to say so and hand the work back, and a
-- seat that talks too much should not lose the writes that do its work. Two
-- counters, one window: they roll over together.
--
-- NULL means coordination is not limited, so an existing budget behaves
-- exactly as it did until an operator sets the second ceiling. 0 is a
-- legitimate value: no comment, claim or delegation in the window.
ALTER TABLE agent_budgets
    ADD COLUMN coordination_limit INTEGER
    CHECK (coordination_limit IS NULL OR coordination_limit >= 0);
ALTER TABLE agent_budgets
    ADD COLUMN coordination_used INTEGER NOT NULL DEFAULT 0
    CHECK (coordination_used >= 0);
