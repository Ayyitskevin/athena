// The gated command palette (MWS-18): keyboard-first capture, claim, yield,
// complete, approve, and inspect for the signed-in operator.
//
// The palette is a CLIENT of /aegis/palette/actions, never an authority: the
// server projects exactly the actions this actor may run in this context, and
// the palette renders only those. Every run POSTs to the action's own endpoint,
// which is a thin adapter over the existing command — one authorization
// decision, one activity event. Unknown or clipped issue refs come back as the
// server's 404 and are shown as a visible refusal; the palette never guesses a
// target. Stale ETags / lease generations come back as the command's own
// 412/409 refusal and are shown verbatim.
//
// CSP-safe: external file under /static (script-src 'self'), no inline handlers.
(function () {
  "use strict";

  var REF_RE = /^([A-Za-z][A-Za-z0-9]*-\d+|\d+)$/;

  function createSingleFlight() {
    var running = false;
    return {
      busy: function () {
        return running;
      },
      run: function (task) {
        if (running) {
          return null;
        }
        running = true;
        var result;
        try {
          result = task();
        } catch (error) {
          running = false;
          throw error;
        }
        return Promise.resolve(result).finally(function () {
          running = false;
        });
      },
    };
  }

  function focusTrapTarget(activeIndex, count, shiftKey) {
    if (!count) {
      return null;
    }
    if (shiftKey && activeIndex === 0) {
      return count - 1;
    }
    if (!shiftKey && activeIndex === count - 1) {
      return 0;
    }
    return null;
  }

  function moveActiveIndex(current, direction, actionCount, hasLookup) {
    var choices = [];
    for (var index = 0; index < actionCount; index += 1) {
      choices.push(index);
    }
    if (hasLookup) {
      choices.push(-2);
    }
    if (!choices.length) {
      return -1;
    }
    var position = choices.indexOf(current);
    if (position === -1) {
      return direction < 0 ? choices[choices.length - 1] : choices[0];
    }
    return choices[(position + direction + choices.length) % choices.length];
  }

  function defaultActiveIndex(query, actionCount) {
    if (REF_RE.test(query.trim())) {
      return -2;
    }
    return actionCount ? 0 : -1;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      createSingleFlight: createSingleFlight,
      defaultActiveIndex: defaultActiveIndex,
      focusTrapTarget: focusTrapTarget,
      moveActiveIndex: moveActiveIndex,
    };
  }
  if (typeof document === "undefined") {
    return;
  }

  var root = document.getElementById("palette");
  if (!root) {
    return; // signed-out pages render no palette at all
  }
  var input = root.querySelector(".palette-input");
  var list = root.querySelector(".palette-actions");
  var detail = root.querySelector(".palette-detail");
  var status = root.querySelector(".palette-status");

  var actions = [];
  var activeIndex = -1;
  var submitFlight = createSingleFlight();
  var lastFocus = null;

  function csrf() {
    return root.getAttribute("data-csrf") || "";
  }

  function setStatus(message, isError) {
    status.textContent = message || "";
    status.setAttribute("data-tone", isError ? "error" : "info");
  }

  function open() {
    lastFocus = document.activeElement;
    root.hidden = false;
    input.value = "";
    detail.textContent = "";
    setStatus("");
    input.focus();
    loadActions();
  }

  function close() {
    root.hidden = true;
    detail.textContent = "";
    if (lastFocus && lastFocus.focus) {
      lastFocus.focus(); // return focus where the operator was
    }
    lastFocus = null;
  }

  function loadActions(issueRef) {
    var url = "/aegis/palette/actions";
    var ref = typeof issueRef === "string" ? issueRef : root.getAttribute("data-issue-ref");
    if (ref) {
      url += "?issue_ref=" + encodeURIComponent(ref);
    }
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) {
            // Unknown/stale/clipped identity: a visible refusal, never a guess.
            actions = [];
            render();
            setStatus(body.detail || "Refused.", true);
            return null;
          }
          actions = body.actions || [];
          activeIndex = actions.length ? 0 : -1;
          render();
          return body;
        });
      })
      .catch(function () {
        setStatus("Could not reach the palette service.", true);
        return null;
      });
  }

  function filtered() {
    var q = input.value.trim().toLowerCase();
    if (!q || REF_RE.test(input.value.trim())) {
      return actions; // a ref-shaped query is a lookup, not a filter
    }
    return actions.filter(function (a) {
      return a.label.toLowerCase().indexOf(q) !== -1;
    });
  }

  function render() {
    list.textContent = "";
    var shown = filtered();
    shown.forEach(function (action, i) {
      var item = document.createElement("li");
      item.setAttribute("role", "option");
      item.className = "palette-action" + (i === activeIndex ? " palette-active" : "");
      item.setAttribute("aria-selected", i === activeIndex ? "true" : "false");
      item.textContent = action.label;
      item.addEventListener("click", function () {
        activate(action);
      });
      list.appendChild(item);
    });
    // A ref-shaped query always offers an explicit inspect entry: activation
    // resolves the ref through the SERVER first — only a resolved issue navigates.
    var query = input.value.trim();
    if (REF_RE.test(query)) {
      var lookup = document.createElement("li");
      lookup.setAttribute("role", "option");
      lookup.className =
        "palette-action" + (activeIndex === -2 ? " palette-active" : "");
      lookup.textContent = "Inspect " + query;
      lookup.addEventListener("click", function () {
        inspectRef(query);
      });
      list.appendChild(lookup);
    }
    if (!list.children.length) {
      var empty = document.createElement("li");
      empty.className = "palette-empty";
      empty.textContent = "No actions available here.";
      list.appendChild(empty);
    }
  }

  function inspectRef(ref) {
    loadActions(ref).then(function (body) {
      if (body && body.issue) {
        window.location.assign("/aegis/issues/" + body.issue.id + "/work-context");
      }
      // refusal already shown by loadActions; nothing is guessed
    });
  }

  function activate(action) {
    if (submitFlight.busy()) {
      return; // a command is already running; Enter/click is not a second submit
    }
    if (action.navigate) {
      window.location.assign(action.navigate);
      return;
    }
    renderForm(action);
  }

  function renderForm(action) {
    detail.textContent = "";
    var form = document.createElement("form");
    form.className = "palette-form";
    (action.fields || []).forEach(function (field) {
      if (field.type === "hidden") {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = field.name;
        hidden.value = field.value;
        form.appendChild(hidden);
        return;
      }
      var label = document.createElement("label");
      label.className = "palette-field";
      label.textContent = field.label || field.name;
      var control;
      if (field.type === "select") {
        control = document.createElement("select");
        (field.options || []).forEach(function (opt) {
          var option = document.createElement("option");
          option.value = opt;
          option.textContent = opt;
          control.appendChild(option);
        });
      } else if (field.type === "textarea") {
        control = document.createElement("textarea");
        control.rows = 3;
      } else {
        control = document.createElement("input");
        control.type = "text";
      }
      control.name = field.name;
      if (field.required) {
        control.required = true;
      }
      label.appendChild(control);
      form.appendChild(label);
    });
    var run = document.createElement("button");
    run.type = "submit";
    run.className = "button small palette-run";
    run.textContent = action.label;
    form.appendChild(run);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submit(action, form, run);
    });
    detail.appendChild(form);
    var first = form.querySelector(
      "input:not([type=hidden]), select, textarea"
    );
    (first || run).focus();
  }

  function submit(action, form, button) {
    submitFlight.run(function () {
      button.disabled = true;
      setStatus("Running " + action.label + "…");
      var body = new URLSearchParams(new FormData(form));
      return fetch(action.endpoint, {
        method: "POST",
        headers: {
          "X-CSRF-Token": csrf(),
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json",
        },
        body: body,
      })
        .then(function (res) {
          return res.json().then(function (payload) {
            if (!res.ok) {
              // The command's own refusal (stale identity, conflict, forbidden),
              // shown verbatim — the palette never retries or rewrites it.
              setStatus(payload.detail || "Refused.", true);
              return;
            }
            detail.textContent = "";
            var done = action.label + " — done.";
            if (payload.href) {
              var link = document.createElement("a");
              link.href = payload.href;
              link.textContent = payload.ref || payload.href;
              setStatus(done + " ");
              status.appendChild(link);
            } else {
              setStatus(done);
            }
            return loadActions(); // keep single-flight until the new state arrives
          });
        })
        .catch(function () {
          setStatus("Request failed — nothing was recorded client-side.", true);
        })
        .finally(function () {
          button.disabled = false;
        });
    });
  }

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (root.hidden) {
        open();
      } else {
        close();
      }
      return;
    }
    if (root.hidden) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    // Focus trap: Tab never leaves the dialog while it is open.
    if (event.key === "Tab") {
      var focusables = root.querySelectorAll(
        "input, select, textarea, button, a[href]"
      );
      if (!focusables.length) {
        return;
      }
      var active = Array.prototype.indexOf.call(
        focusables,
        document.activeElement,
      );
      var focusTarget = focusTrapTarget(
        active,
        focusables.length,
        event.shiftKey,
      );
      if (focusTarget !== null) {
        event.preventDefault();
        focusables[focusTarget].focus();
      }
      return;
    }
    if (document.activeElement !== input) {
      return; // form fields own their own keys
    }
    var shown = filtered();
    var hasLookup = REF_RE.test(input.value.trim());
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeIndex = moveActiveIndex(activeIndex, 1, shown.length, hasLookup);
      render();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeIndex = moveActiveIndex(activeIndex, -1, shown.length, hasLookup);
      render();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (activeIndex === -2 || (activeIndex === -1 && hasLookup)) {
        inspectRef(input.value.trim());
      } else if (shown[activeIndex]) {
        activate(shown[activeIndex]);
      }
    }
  });

  input.addEventListener("input", function () {
    activeIndex = defaultActiveIndex(input.value, filtered().length);
    render();
  });
})();
