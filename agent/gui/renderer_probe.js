/* Assertions for the Markdown renderer in app.js, run for real in a browser.
 *
 * gui_test.py cannot reach this code: it is Python, and a reimplementation of a parser in the
 * language that tests it proves nothing about the parser. So this file drives the genuine
 * `renderMarkdown` and reports into the DOM, which `agent/renderer_probe.py` reads back from a
 * headless browser.
 *
 * The termination guard exists because of a specific regression: a line containing "|" that is
 * not followed by a table separator row used to wedge the paragraph loop, appending empty
 * <p> nodes forever. A renderer that hangs is not a slow pass, so the guard fails it.
 */
(function () {
  "use strict";

  var CAP = 2000;                 // elements created before we call it a runaway loop
  var results = [];
  var realCreate = document.createElement.bind(document);
  var created = 0;
  var pageErrors = [];

  window.onerror = function (msg) { pageErrors.push(String(msg)); return false; };

  document.createElement = function (tag) {
    if (++created > CAP) {
      var e = new Error("createElement budget exhausted");
      e.runaway = true;
      throw e;
    }
    return realCreate(tag);
  };

  function check(name, conditions) {
    var box = realCreate("div");
    var outcome = {name: name, pass: true, notes: []};
    created = 0;              // budget per case: a runaway first case must not fail the rest
    var started = performance.now();
    try {
      renderMarkdown(box, conditions.input);
    } catch (err) {
      outcome.pass = false;
      outcome.notes.push(err && err.runaway
        ? "DID NOT TERMINATE: exceeded " + CAP + " element creations"
        : "threw " + String(err));
    }
    outcome.notes.push(((performance.now() - started).toFixed(1)) + "ms");

    if (conditions.empty) {
      expect(outcome, box.childNodes.length === 0, "expected no children, got " + box.childNodes.length);
    }
    if (conditions.tags) {
      var all = Array.prototype.map.call(box.children, function (n) { return n.tagName; });
      // A wedged loop can create two thousand nodes; printing them buries the one line that
      // explains the failure.
      var got = all.length > 8 ? all.slice(0, 8).join(",") + ",… (" + all.length + " total)"
                               : all.join(",");
      var want = all.length > 8 ? conditions.tags + " (only)" : conditions.tags;
      expect(outcome, got === want, "expected " + want + ", got " + got);
    }
    if (conditions.text !== undefined) {
      var seen = brief(box.textContent.replace(/\s+/g, " ").trim());
      expect(outcome, box.textContent.replace(/\s+/g, " ").trim() === conditions.text,
        "expected text “" + brief(conditions.text) + "”, got “" + seen + "”");
    }
    if (conditions.rows !== undefined) {
      // Counted as `tr`, not `tbody tr`: insertRow() does not guarantee a tbody querySelector
      // can see, and which wrapper element the row landed in is not what this probe is about.
      var rows = box.querySelectorAll("tr").length;
      expect(outcome, rows === conditions.rows, "expected " + conditions.rows + " rows, got " + rows);
    }
    if (conditions.items !== undefined) {
      expect(outcome, box.querySelectorAll("li").length === conditions.items,
        "expected " + conditions.items + " list items, got " + box.querySelectorAll("li").length);
    }
    if (conditions.silentHtml) {
      // The dangerous case: markup must survive as text, never as a node or an attribute.
      expect(outcome, box.querySelectorAll("img,script,iframe,svg,object").length === 0,
        "hostile markup created live elements");
      expect(outcome, !/\son\w+=/.test(serialize(box)), "an event handler attribute was created");
      expect(outcome, box.querySelectorAll("strong").length === conditions.strong || 0,
        "expected " + conditions.strong + " <strong>, got " + box.querySelectorAll("strong").length);
    }
    results.push(outcome);
  }

  function expect(outcome, ok, detail) {
    if (!ok) { outcome.pass = false; outcome.notes.push(detail); }
  }

  function brief(value) {
    var text = String(value);
    return text.length > 70 ? text.slice(0, 70) + "…" : text;
  }

  function serialize(box) {
    var out = "";
    Array.prototype.forEach.call(box.querySelectorAll("*"), function (n) {
      out += n.tagName.toLowerCase() + " ";
      Array.prototype.forEach.call(n.attributes, function (a) { out += a.name + "=" + a.value + " "; });
    });
    return out;
  }

  if (typeof renderMarkdown !== "function") {
    report([{name: "renderMarkdown is reachable", pass: false, notes: ["not a global function"]}],
      ["app.js did not expose the renderer"]);
    return;
  }

  // Regression: the wedge cases. These two are what used to spin forever.
  check("pipe line without a separator row", {
    input: "engine=cudf | rows=2075259\n", tags: "P",
    text: "engine=cudf | rows=2075259"
  });
  check("pipe inside prose", {
    input: "结论：峰谷比 4.28 | 关键\n", tags: "P", text: "结论：峰谷比 4.28 | 关键"
  });
  check("pipe on the final line, nothing after it", {
    input: "最后一行 a | b", tags: "P", text: "最后一行 a | b"
  });

  // The structures the renderer claims to support must still work.
  check("table", {
    input: "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n",
    tags: "TABLE", rows: 3, text: "ab1234"
  });
  check("prose with a pipe, then a real table", {
    input: "前置说明 a | b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n",
    tags: "P,TABLE"
  });
  check("heading and list", {
    input: "### 小标题\n\n- 一\n- 二\n", tags: "H5,UL", items: 2, text: "小标题一二"
  });
  check("multi-line paragraph joins", {
    input: "第一行\n第二行\n", tags: "P"
  });
  check("empty input creates nothing", {input: "", empty: true});
  check("only blank lines create nothing", {input: "\n\n  \n", empty: true});

  // Escape-first: markup arrives as text, and the one inline transform still works.
  check("hostile markup stays inert", {
    input: '<img src=x onerror=alert(1)> <script>alert(2)<\/script> 和 **粗体**',
    tags: "P", silentHtml: true, strong: 1,
    text: "<img src=x onerror=alert(1)> <script>alert(2)</script> 和 粗体"
  });

  report(results, pageErrors);

  function report(rows, errors) {
    var host = document.getElementById("results");
    var failed = 0;
    rows.forEach(function (row) {
      if (!row.pass) failed++;
      var line = document.createTextNode(
        (row.pass ? "PASS " : "FAIL ") + row.name + "  [" + row.notes.join("; ") + "]");
      var pre = realCreate("pre");
      pre.setAttribute("data-result", row.pass ? "PASS" : "FAIL");
      pre.appendChild(line);
      host.appendChild(pre);
    });
    var summary = document.getElementById("summary");
    summary.setAttribute("data-result", failed || errors.length ? "FAIL" : "PASS");
    summary.textContent = "PROBE SUMMARY: " + (rows.length - failed) + " passed, " + failed
      + " failed" + (errors.length ? ", page errors: " + errors.join(" | ") : "");
    document.title = summary.textContent;
  }
})();
