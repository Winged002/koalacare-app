/* ==========================================================================
   KoalaCare app — progressive enhancement only.

   Nothing here is required for the app to work. Every interaction has a
   working non-JavaScript fallback:

     * each select that auto-submits is paired with a visible submit button
       inside the same form (data-autosubmit-fallback);
     * the mobile tab bar and the "More" drawer are ordinary links.

   This file only removes the extra click for people who have JavaScript.
   The app's CSP is script-src 'self', which is why this lives in a file
   rather than in an inline onchange="" attribute — inline handlers never run.
   ========================================================================== */

(function () {
  "use strict";

  /* --------------------------------------------- auto-submitting selects */

  var selects = document.querySelectorAll("select[data-autosubmit]");

  Array.prototype.forEach.call(selects, function (select) {
    var form = select.form;
    if (!form) {
      return;
    }

    // JavaScript is available, so the explicit "switch/save" button is
    // redundant. Hide it rather than remove it, so a form submitted from a
    // half-loaded page still has it. The [hidden] rule in app.css uses
    // !important to win over .btn's inline-flex display.
    var fallback = form.querySelector("[data-autosubmit-fallback]");
    if (fallback) {
      fallback.hidden = true;
    }

    select.addEventListener("change", function () {
      // Guard against a second change firing before navigation begins.
      // Note: the control is deliberately left enabled — a disabled control
      // is not submitted, which would drop the chosen value from the POST.
      if (form.getAttribute("data-submitting") === "true") {
        return;
      }
      form.setAttribute("data-submitting", "true");
      form.submit();
    });
  });
})();
