// Drag-to-change-status for the Aegis board.
//
// Progressive enhancement: without JS the board is still a readable kanban and an
// issue's status is changed from its detail page. With JS, a card can be dragged
// between columns; on drop we POST the move and let HTMX swap in the freshly
// rendered board (so the server stays the single source of truth — we never mutate
// the DOM ourselves to "guess" the result).
//
// Listeners are delegated on document so they keep working after HTMX replaces the
// .board on every filter change or move. CSP-safe: this is an external file under
// /static (script-src 'self'), with no inline handlers.
(function () {
  "use strict";

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }

  function clearDropTargets() {
    document.querySelectorAll(".board-column.drag-over").forEach(function (column) {
      column.classList.remove("drag-over");
    });
  }

  function draggingCard() {
    return document.querySelector(".board-card.dragging");
  }

  function laneFor(el) {
    var lane = closest(el, ".board-swimlane");
    return lane ? lane.getAttribute("data-lane") : null;
  }

  function filterValue(name) {
    var control = document.querySelector('.board-filters [name="' + name + '"]');
    return control ? control.value : "";
  }

  function copyFilter(form, name) {
    var input = form.querySelector('input[name="' + name + '"]');
    if (input) input.value = filterValue(name);
  }

  document.addEventListener("submit", function (event) {
    var form = closest(event.target, ".board-move");
    var coordinator = document.getElementById("board-request-coordinator");
    if (!form || !coordinator || !window.htmx) {
      return; // HTMX unavailable: preserve the form's native POST + 303 path
    }
    event.preventDefault();

    // The board is replaced after every response, so its card forms are ephemeral.
    // Snapshot the form values but use the persistent header as HTMX's request
    // source; queued requests then survive a preceding board swap and share the
    // coordinator's queue with filter reads.
    copyFilter(form, "search");
    copyFilter(form, "status");
    copyFilter(form, "project");
    copyFilter(form, "sprint");
    var values = {};
    new FormData(form).forEach(function (value, name) {
      values[name] = value;
    });
    window.htmx.ajax("POST", form.getAttribute("action"), {
      source: coordinator,
      values: values,
    });
  });

  document.addEventListener("dragstart", function (event) {
    var card = closest(event.target, ".board-card");
    if (!card || card.getAttribute("draggable") !== "true") {
      return;
    }
    // The card is draggable, but its links and form controls must keep their native
    // click/selection behaviour. Preventing their drag also avoids a URL becoming
    // text/plain and masquerading as an issue id in the drop handler.
    if (closest(event.target, "a, button, input, select, textarea, label")) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.setData("text/plain", card.getAttribute("data-issue-id"));
    event.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });

  document.addEventListener("dragend", function (event) {
    var card = closest(event.target, ".board-card");
    if (card) {
      card.classList.remove("dragging");
    }
    clearDropTargets();
  });

  document.addEventListener("dragover", function (event) {
    var column = closest(event.target, ".board-column");
    var card = draggingCard();
    if (!column || !card) {
      return;
    }
    if (!laneFor(card) || laneFor(card) !== laneFor(column)) {
      clearDropTargets();
      event.dataTransfer.dropEffect = "none";
      return; // a status-only move never reassigns work across swimlanes
    }
    event.preventDefault(); // mark this column as a valid drop target
    event.dataTransfer.dropEffect = "move";
    column.classList.add("drag-over");
  });

  document.addEventListener("dragleave", function (event) {
    var column = closest(event.target, ".board-column");
    // Only clear when the pointer actually left the column (not just moved onto a
    // child element inside it).
    if (column && !column.contains(event.relatedTarget)) {
      column.classList.remove("drag-over");
    }
  });

  document.addEventListener("drop", function (event) {
    var column = closest(event.target, ".board-column");
    var card = draggingCard();
    clearDropTargets();
    if (!column || !card) {
      return;
    }
    event.preventDefault();
    if (!laneFor(card) || laneFor(card) !== laneFor(column)) {
      return; // leave assignment untouched; the card snaps back to its source lane
    }

    var issueId = card.getAttribute("data-issue-id");
    var newStatus = column.getAttribute("data-status");
    if (!issueId || !newStatus) {
      return;
    }

    var fromColumn = closest(card, ".board-column");
    if (fromColumn && fromColumn.getAttribute("data-status") === newStatus) {
      return; // dropped back into its own column — nothing to do
    }

    // Route drag through the same synchronized, CSRF-protected form as keyboard
    // moves. This keeps every board read/write on one queue and avoids a late
    // response targeting a board node that another request already replaced.
    var form = card.querySelector(".board-move");
    var statusSelect = form && form.querySelector(".board-move-select");
    if (!form || !statusSelect) return;

    // Mixed-project boards may show a destination that is invalid for this card.
    // The select is the card's authoritative target set; the server validates too.
    statusSelect.value = newStatus;
    if (statusSelect.value !== newStatus) return;

    form.requestSubmit();
  });
})();
