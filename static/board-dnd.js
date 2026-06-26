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

  document.addEventListener("dragstart", function (event) {
    var card = closest(event.target, ".board-card");
    if (!card || card.getAttribute("draggable") !== "true") {
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
  });

  document.addEventListener("dragover", function (event) {
    var column = closest(event.target, ".board-column");
    if (!column) {
      return;
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
    if (!column) {
      return;
    }
    event.preventDefault();
    column.classList.remove("drag-over");

    var board = document.querySelector(".board");
    var issueId = event.dataTransfer.getData("text/plain");
    var newStatus = column.getAttribute("data-status");
    if (!board || !issueId || !newStatus) {
      return;
    }

    var card = document.querySelector(
      '.board-card[data-issue-id="' + (window.CSS && CSS.escape ? CSS.escape(issueId) : issueId) + '"]'
    );
    var fromColumn = card ? closest(card, ".board-column") : null;
    if (fromColumn && fromColumn.getAttribute("data-status") === newStatus) {
      return; // dropped back into its own column — nothing to do
    }

    // Preserve the active filters so the re-rendered board keeps the same view.
    var values = {
      new_status: newStatus,
      csrf_token: board.getAttribute("data-csrf") || "",
    };
    var searchInput = document.querySelector(".board-filter-search");
    var statusFilter = document.querySelector(".board-filter-status");
    if (searchInput) {
      values.search = searchInput.value;
    }
    if (statusFilter) {
      values.status = statusFilter.value;
    }

    window.htmx.ajax("POST", "/aegis/boards/move/" + encodeURIComponent(issueId), {
      target: ".board",
      swap: "outerHTML",
      values: values,
    });
  });
})();
