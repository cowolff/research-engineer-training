/* Token streaming for the LLM replies this app generates (docs §5.7).
 *
 * Vendored and loaded as a file, not inlined: the CSP is `script-src 'self'`
 * with no `unsafe-inline`, for the same reason htmx is vendored next to it.
 *
 * The division of labour with htmx is the important part, and it is a
 * security boundary as much as an architectural one:
 *
 *   this file  — appends raw model output as TEXT NODES while it generates.
 *                Never innerHTML. Nothing here can inject markup, because
 *                nothing here ever parses any.
 *   htmx       — replaces the whole element once, when the stream ends, with
 *                markup the server rendered through markdown + nh3.
 *
 * So the animated version is inert by construction and the trusted version is
 * built where sanitisation already lives. This file never needs to know what
 * it is streaming, which URL to swap in, or where to put it — all of that is
 * hx-* attributes on the element (see partials/stream_bubble.html).
 */
(function () {
  "use strict";

  var SCROLL_SLACK_PX = 120;

  function pinnedToBottom() {
    return window.innerHeight + window.scrollY >= document.body.offsetHeight - SCROLL_SLACK_PX;
  }

  function attach(element) {
    if (element.dataset.streamStarted) return;
    element.dataset.streamStarted = "1";
    element.dataset.streaming = "1";

    var source = new EventSource(element.dataset.streamUrl);
    var settled = false;

    function finish() {
      if (settled) return;
      settled = true;
      source.close();
      delete element.dataset.streaming;
      // htmx is listening for this on the element itself; it is what fetches
      // and swaps in the finished, server-rendered version.
      element.dispatchEvent(new CustomEvent("stream-done"));
    }

    source.addEventListener("delta", function (event) {
      var wasPinned = pinnedToBottom();
      element.appendChild(document.createTextNode(JSON.parse(event.data).text));
      // Follow the text down only if the reader was already at the bottom —
      // scrolling someone away from a paragraph they went back to re-read is
      // worse than letting the reply run off-screen.
      if (wasPinned) window.scrollTo(0, document.body.scrollHeight);
    });

    // The generation was restarted (a schema-validation retry server-side), so
    // what is on screen belongs to an attempt that has been abandoned.
    source.addEventListener("reset", function () {
      element.textContent = "";
    });

    source.addEventListener("done", finish);

    source.addEventListener("error", function () {
      // EventSource reconnects by itself while a connection is merely flaky,
      // resuming from Last-Event-ID; only a source it has given up on is
      // terminal. Either way the swap below renders from the database, so a
      // lost connection costs the animation and nothing else.
      if (source.readyState === EventSource.CLOSED) finish();
    });
  }

  function scan(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.dataset && root.dataset.streamUrl) attach(root);
    root.querySelectorAll("[data-stream-url]").forEach(attach);
  }

  document.addEventListener("DOMContentLoaded", function () {
    scan(document.body);
  });

  // Every htmx swap that could carry a new streaming element: a sent message,
  // a swapped help panel, a re-rendered chat panel.
  document.addEventListener("htmx:load", function (event) {
    scan(event.target);
  });
})();
