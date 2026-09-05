/* Auto-print helper for the ticket documents.
 *
 * Loaded only when a ticket page is opened with ?print=1, so simply viewing an
 * e-ticket never hijacks the browser with a print dialog.
 *
 * It is an external same-origin script rather than an inline one on purpose: the
 * CSP is `script-src 'self' 'nonce-...'`, and a nonce is minted per response, so
 * an inline snippet in a server-rendered document would need the nonce threaded
 * through the template. 'self' covers this file with no such coupling.
 *
 * Printing is deferred to the load event and then one animation frame, because
 * the QR must be laid out before the dialog opens — a print dialog raised over an
 * unpainted image can produce a blank code, which is the one failure that makes a
 * ticket useless at the gate.
 */
(function () {
  'use strict';

  function print() {
    try {
      window.focus();
    } catch (_) {
      /* focus is a courtesy, not a requirement */
    }
    window.print();
  }

  window.addEventListener('load', function () {
    var images = Array.prototype.slice.call(document.images || []);
    var pending = images.filter(function (img) { return !img.complete; });

    if (!pending.length) {
      window.requestAnimationFrame(function () { window.requestAnimationFrame(print); });
      return;
    }

    // Wait for the last image, but never hang: if one stalls, print anyway rather
    // than leaving the operator staring at a page that will not print.
    var remaining = pending.length;
    var done = false;
    function settle() {
      remaining -= 1;
      if (remaining <= 0 && !done) {
        done = true;
        window.requestAnimationFrame(print);
      }
    }
    pending.forEach(function (img) {
      img.addEventListener('load', settle);
      img.addEventListener('error', settle);
    });
    window.setTimeout(function () {
      if (!done) {
        done = true;
        print();
      }
    }, 1500);
  });
})();
