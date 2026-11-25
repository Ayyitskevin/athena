// A comment's draft, kept in this browser tab until the comment is accepted
// (U11, COCKPIT-13). A comment written while the session expired used to be
// lost: the post was refused, and the box on the page came back empty.
//
// A box asks for this with data-draft (the new-comment boxes on an issue and on
// a Mentor page). What is typed there is saved in sessionStorage under the
// form's action path, so each issue or page has its own draft. When the page
// loads with that box empty, the draft goes back in.
//
// The draft is forgotten only when the comment was ACCEPTED. A submit leaves a
// note saying which draft it carried; the next page to load reads the note
// once and throws it away. The draft is forgotten only if that page holds the
// same box again (an accepted comment redirects back to its issue or page) and
// carries no data-refused marker (every refusal page carries one: the shared
// refusal page, and an issue or page re-rendered with an error). A refusal for
// an expired session, the sign-in page a closed instance redirects to, and an
// error page this script never ran on all leave the draft where it was.
//
// Nothing here reaches the server: the draft lives in this tab's
// sessionStorage, which a private window or blocked storage may not offer, and
// then this does nothing at all.
(function () {
  var PREFIX = "athena-draft:";
  var NOTE = "athena-draft-submitted";
  // The submit's note counts only on the page load that directly follows it.
  var NOTE_MAX_AGE_MS = 60 * 1000;

  var storage = null;
  try {
    storage = window.sessionStorage;
    storage.setItem(NOTE + ":probe", "1");
    storage.removeItem(NOTE + ":probe");
  } catch (error) {
    return;
  }

  function keyFor(box) {
    var form = box.form;
    if (!form) {
      return null;
    }
    var action = form.getAttribute("action") || window.location.pathname;
    return PREFIX + new URL(action, window.location.href).pathname;
  }

  function isDraftBox(target) {
    return (
      target instanceof HTMLTextAreaElement && target.hasAttribute("data-draft")
    );
  }

  function forget(key) {
    try {
      storage.removeItem(key);
    } catch (error) {
      // Storage went away mid-session: nothing to forget.
    }
  }

  function remember(key, text) {
    try {
      if (text.trim()) {
        storage.setItem(key, text);
      } else {
        storage.removeItem(key);
      }
    } catch (error) {
      // Full or blocked: the box still works, the draft is just not kept.
    }
  }

  function readNote() {
    var note = null;
    try {
      note = JSON.parse(storage.getItem(NOTE) || "null");
    } catch (error) {
      note = null;
    }
    forget(NOTE);
    return note;
  }

  function navigationType() {
    var entries = window.performance && window.performance.getEntriesByType
      ? window.performance.getEntriesByType("navigation")
      : [];
    return entries.length ? entries[0].type : "navigate";
  }

  var boxes = Array.prototype.slice.call(
    document.querySelectorAll("textarea[data-draft]")
  );

  // Was the last submitted draft accepted?
  var note = readNote();
  var refused = document.querySelector("[data-refused]") !== null;
  if (
    note &&
    typeof note.key === "string" &&
    Date.now() - note.at < NOTE_MAX_AGE_MS &&
    navigationType() === "navigate" &&
    !refused
  ) {
    boxes.forEach(function (box) {
      if (keyFor(box) === note.key) {
        forget(note.key);
      }
    });
  }

  // Put a kept draft back into its empty box.
  boxes.forEach(function (box) {
    var key = keyFor(box);
    var kept = key ? storage.getItem(key) : null;
    if (kept && !box.value.trim()) {
      box.value = kept;
      var section = box.closest("details");
      if (section) {
        section.open = true;
      }
    }
  });

  document.addEventListener("input", function (event) {
    var box = event.target;
    if (!isDraftBox(box)) {
      return;
    }
    var key = keyFor(box);
    if (key) {
      remember(key, box.value);
    }
  });

  // Registered after confirm.js's own listener, so a submit the person
  // cancelled there (defaultPrevented) leaves no note.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) {
      return;
    }
    var box = form.querySelector("textarea[data-draft]");
    var key = box ? keyFor(box) : null;
    if (!key) {
      return;
    }
    remember(key, box.value);
    try {
      storage.setItem(NOTE, JSON.stringify({ key: key, at: Date.now() }));
    } catch (error) {
      // Without the note the draft is simply kept: the safe side.
    }
  });
})();
