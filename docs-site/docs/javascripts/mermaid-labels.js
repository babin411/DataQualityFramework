/* Let mermaid classDef text colours win over Material's theme colour.
 *
 * Material renders each diagram in a closed shadow root and styles it through
 * mermaid.initialize({ themeCSS }), including `.nodeLabel p { color: <theme> }`.
 * That rule beats the `color:` of a classDef (mermaid applies it to the label's
 * <span>, not the <p> inside), so a dark label landed on a light node in dark
 * mode and vice versa. Material uses an already-loaded `mermaid` global (see
 * extra_javascript in mkdocs.yml), so wrap initialize() and append one rule that
 * makes the <p> inherit the span's colour. Nodes without a classDef still get
 * Material's theme colour, because their span keeps it.
 */
(function () {
  if (typeof window.mermaid === "undefined") return;
  var EXTRA_CSS = ".node .nodeLabel p { color: inherit; }";
  var initialize = window.mermaid.initialize.bind(window.mermaid);
  window.mermaid.initialize = function (config) {
    config = Object.assign({}, config);
    config.themeCSS = (config.themeCSS || "") + EXTRA_CSS;
    return initialize(config);
  };
})();
