// The behaviours a page asks for with a data attribute. Everything here is
// delegated from the document, so markup swapped in later behaves the same,
// and no template carries a script or a handler of its own: the page's
// content security policy (script-src 'self') blocks both.
(function () {
  // data-confirm="question" on a form: ask before it submits.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  // data-autosubmit on a form control: submit its form when it changes. The
  // form keeps its own submit button for a browser without scripts.
  document.addEventListener("change", function (event) {
    var control = event.target;
    if (!(control instanceof Element) || !control.hasAttribute("data-autosubmit")) {
      return;
    }
    if (control.form) {
      control.form.requestSubmit();
    }
  });

  // data-copy-command="text" on a button: copy the text and say so.
  function copied(button) {
    var original = button.textContent;
    button.textContent = "Copied";
    window.setTimeout(function () {
      button.textContent = original;
    }, 1600);
  }

  function copyWithoutClipboard(text) {
    var holder = document.createElement("textarea");
    holder.value = text;
    holder.setAttribute("readonly", "readonly");
    holder.className = "visually-hidden";
    document.body.appendChild(holder);
    holder.select();
    var done = document.execCommand("copy");
    document.body.removeChild(holder);
    return done;
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    var button =
      target instanceof Element ? target.closest("[data-copy-command]") : null;
    if (!button) {
      return;
    }
    var text = button.getAttribute("data-copy-command") || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          copied(button);
        },
        function () {
          if (copyWithoutClipboard(text)) {
            copied(button);
          }
        }
      );
    } else if (copyWithoutClipboard(text)) {
      copied(button);
    }
  });
})();
