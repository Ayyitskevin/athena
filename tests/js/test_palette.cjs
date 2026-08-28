"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  createSingleFlight,
  focusTrapTarget,
  moveActiveIndex,
} = require("../../src/athena/static/palette.js");

test("keyboard selection includes an exact-ref lookup and wraps", () => {
  assert.equal(moveActiveIndex(0, 1, 2, true), 1);
  assert.equal(moveActiveIndex(1, 1, 2, true), -2);
  assert.equal(moveActiveIndex(-2, 1, 2, true), 0);
  assert.equal(moveActiveIndex(0, -1, 2, true), -2);
  assert.equal(moveActiveIndex(-1, 1, 0, true), -2);
  assert.equal(moveActiveIndex(-1, -1, 0, true), -2);
  assert.equal(moveActiveIndex(-1, 1, 0, false), -1);
});

test("focus trapping wraps only at the dialog edges", () => {
  assert.equal(focusTrapTarget(0, 3, true), 2);
  assert.equal(focusTrapTarget(2, 3, false), 0);
  assert.equal(focusTrapTarget(1, 3, true), null);
  assert.equal(focusTrapTarget(1, 3, false), null);
  assert.equal(focusTrapTarget(0, 0, false), null);
});

test("one logical submit cannot start twice while its request is pending", async () => {
  const flight = createSingleFlight();
  let calls = 0;
  let release;
  const first = flight.run(
    () =>
      new Promise((resolve) => {
        calls += 1;
        release = resolve;
      }),
  );
  const duplicate = flight.run(async () => {
    calls += 1;
  });

  assert.equal(calls, 1);
  assert.equal(duplicate, null);
  assert.equal(flight.busy(), true);

  release();
  await first;
  assert.equal(flight.busy(), false);

  await flight.run(async () => {
    calls += 1;
  });
  assert.equal(calls, 2);
});
