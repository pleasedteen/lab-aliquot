/* 样品台账前端：无依赖，直接操作 JSON 接口。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  tab: "inventory",
  seq: null,
  inventory: null,
  history: null,
  recalls: null,
};

// ------------------------------------------------------------------ 工具

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  const ct = res.headers.get("Content-Type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error((data && data.error) || `请求失败（${res.status}）`);
    err.details = (data && data.details) || [];
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(title, kind = "", details = []) {
  const box = $("#toasts");
  const el = h(
    "div",
    { class: `toast ${kind}` },
    h("div", { class: "t-title" }, title),
    details.length ? h("ul", {}, details.map((d) => h("li", {}, d))) : null
  );
  box.append(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 320);
  }, kind === "error" ? 7000 : 3800);
}

function showError(err, prefix) {
  toast(prefix || err.message, "error", err.details || []);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const STATUS_BADGE = {
  available: ["ok", "可用"],
  exhausted: ["neutral", "已用尽"],
  retired: ["warn", "已报废"],
};

function statusBadge(t) {
  const [cls, label] = STATUS_BADGE[t.status] || ["neutral", t.status];
  return h("span", { class: `badge ${cls}` }, label);
}

function recalledBadge(t) {
  if (!t.recalled) return h("span", { class: "muted small" }, "—");
  const batches = [...new Set((t.recalls || []).map((r) => r.batch))].join("、");
  const inStock = t.status === "available";
  return h(
    "span",
    { class: `badge ${inStock ? "danger" : "neutral"}`, title: `召回批次：${batches}` },
    inStock ? "召回中 · 禁取样" : "召回（已离库）"
  );
}

function pct(n) {
  return `${n}%`;
}

const COLORS = [
  "#2563eb", "#0f9d8f", "#d97706", "#b42318", "#7c3aed",
  "#0891b2", "#be185d", "#4d7c0f", "#9333ea", "#c2410c",
];
function colorFor(intakeId) {
  return COLORS[Math.abs(Number(intakeId)) % COLORS.length];
}

function compositionBar(composition, compact = false) {
  if (!composition || !composition.length) return null;
  return h(
    "div",
    {},
    h(
      "div",
      { class: "composition-bar", title: composition.map((c) => `${c.batch || ""}/${c.intake_code} ${pct(c.percent)}`).join("  ") },
      composition.map((c) =>
        h("span", {
          style: `width:${Math.max(1, Number(c.percent))}%;background:${colorFor(c.intake_id)}`,
        })
      )
    ),
    compact ? null : h(
      "div",
      { class: "composition-legend" },
      composition.map((c) =>
        h(
          "span",
          {},
          h("i", { style: `background:${colorFor(c.intake_id)}` }),
          `${c.batch || "—"} / ${c.intake_code} · ${pct(c.percent)}`
        )
      )
    )
  );
}

function compositionChips(composition, amountKey) {
  if (!composition || !composition.length) return h("span", { class: "muted" }, "—");
  return h(
    "div",
    { class: "chips" },
    composition.map((c) =>
      h(
        "span",
        { class: "badge info" },
        `${c.batch || "—"}/${c.intake_code} ${pct(c.percent)}` +
          (amountKey && c[amountKey] ? ` · ${c[amountKey]} µL` : "")
      )
    )
  );
}

function compositionText(composition) {
  if (!composition || !composition.length) return "—";
  return composition
    .map((c) => `${c.batch || "—"}/${c.intake_code} ${pct(c.percent)}`)
    .join("；");
}

// ------------------------------------------------------------------ 主框架

function initTabs() {
  $("#tabs").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".tab");
    if (!btn) return;
    state.tab = btn.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t === btn));
    render();
  });
}

async function refreshData() {
  const [inv, hist, rec] = await Promise.all([
    api("/api/inventory"),
    api("/api/history"),
    api("/api/recalls"),
  ]);
  state.inventory = inv;
  state.history = hist;
  state.recalls = rec;
  if (state.seq !== null && state.seq > inv.latest_seq) state.seq = inv.latest_seq;
}

async function render() {
  const app = $("#app");
  try {
    await refreshData();
  } catch (err) {
    app.replaceChildren(
      h("div", { class: "alert error" }, `读取台账失败：${err.message}`)
    );
    return;
  }
  const renderers = {
    inventory: renderInventory,
    intake: renderIntake,
    split: renderSplit,
    retire: renderRetire,
    recall: renderRecall,
    reverse: renderReverse,
    trace: renderTrace,
    history: renderHistory,
  };
  const view = (renderers[state.tab] || renderInventory)();
  app.replaceChildren(view);
}

function seqControl(onChange) {
  const input = h("input", {
    type: "number",
    min: "0",
    placeholder: `最新 ${state.inventory.latest_seq}`,
    value: state.seq === null ? "" : String(state.seq),
  });
  const apply = () => {
    const raw = input.value.trim();
    if (raw === "") {
      state.seq = null;
    } else {
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 0 || n > state.inventory.latest_seq) {
        input.classList.add("invalid");
        return;
      }
      state.seq = n;
    }
    onChange();
  };
  input.addEventListener("change", apply);
  return h(
    "span",
    { class: "seq-control" },
    "按操作序号回看：",
    input,
    h(
      "button",
      {
        class: "btn link small",
        onclick: () => {
          state.seq = null;
          onChange();
        },
      },
      "回到当前"
    ),
    h(
      "span",
      { class: "muted" },
      state.seq === null ? "（当前库存）" : `（截至操作 #${state.seq}）`
    )
  );
}

// ------------------------------------------------------------------ 库存

function renderInventory() {
  const inv = state.inventory;
  const c = inv.counts;
  const body = [];
  const reload = () => render();

  const stats = h(
    "div",
    { class: "stats" },
    h("div", { class: "stat" }, h("div", { class: "n" }, c.total), h("div", { class: "l" }, "全部管数")),
    h("div", { class: "stat" }, h("div", { class: "n" }, c.available), h("div", { class: "l" }, "在库可用")),
    h("div", { class: "stat" }, h("div", { class: "n" }, c.exhausted), h("div", { class: "l" }, "已用尽")),
    h("div", { class: "stat" }, h("div", { class: "n" }, c.retired), h("div", { class: "l" }, "已报废")),
    h("div", { class: "stat danger" }, h("div", { class: "n" }, c.recalled_in_stock), h("div", { class: "l" }, "召回禁取样"))
  );

  const filterText = h("input", { type: "text", placeholder: "按编号 / 批次 / 组成筛选…" });
  const filterState = h(
    "select",
    {},
    h("option", { value: "" }, "全部状态"),
    h("option", { value: "available" }, "可用"),
    h("option", { value: "exhausted" }, "已用尽"),
    h("option", { value: "retired" }, "已报废"),
    h("option", { value: "recalled" }, "召回中"),
    h("option", { value: "intake" }, "仅来样"),
    h("option", { value: "pooled" }, "仅分装/混合")
  );

  const tableWrap = h("div", {});
  let sortKey = "id";
  let sortDir = 1;

  function drawTable() {
    const q = filterText.value.trim().toLowerCase();
    const f = filterState.value;
    let rows = inv.tubes.filter((t) => {
      if (f === "recalled" && !(t.recalled && t.status === "available")) return false;
      if (["available", "exhausted", "retired"].includes(f) && t.status !== f) return false;
      if (f === "intake" && t.kind !== "intake") return false;
      if (f === "pooled" && t.kind !== "pooled") return false;
      if (!q) return true;
      const hay = [
        t.code,
        t.batch || "",
        ...t.composition.map((x) => `${x.intake_code} ${x.batch || ""}`),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
    rows = rows.slice().sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (sortKey === "remaining") {
        return (Number(av) - Number(bv)) * sortDir;
      }
      return String(av ?? "").localeCompare(String(bv ?? ""), "zh") * sortDir;
    });

    if (!rows.length) {
      tableWrap.replaceChildren(h("div", { class: "empty" }, "没有符合条件的管"));
      return;
    }

    const th = (key, label, cls = "") =>
      h(
        "th",
        {
          class: cls,
          style: "cursor:pointer",
          onclick: () => {
            if (sortKey === key) sortDir *= -1;
            else {
              sortKey = key;
              sortDir = 1;
            }
            drawTable();
          },
        },
        label + (sortKey === key ? (sortDir > 0 ? " ▲" : " ▼") : "")
      );

    const table = h(
      "table",
      {},
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          th("code", "编号"),
          th("kind", "类型"),
          th("batch", "来样批次"),
          th("status", "状态"),
          th("recalled", "召回"),
          th("initial_volume", "初始量 µL", "num"),
          th("remaining", "剩余量 µL", "num"),
          th("id", "组成（来源占比）")
        )
      ),
      h(
        "tbody",
        {},
        rows.map((t) =>
          h(
            "tr",
            {},
            h(
              "td",
              {},
              h(
                "button",
                {
                  class: "btn link mono",
                  onclick: () => openTube(t.code),
                  title: "查看这管的来源与去向",
                },
                t.code
              )
            ),
            h("td", {}, t.kind === "intake" ? h("span", { class: "badge neutral" }, "来样") : h("span", { class: "badge info" }, "分装/混合")),
            h("td", {}, t.batch || h("span", { class: "muted" }, "—")),
            h("td", {}, statusBadge(t)),
            h("td", {}, recalledBadge(t)),
            h("td", { class: "num" }, t.initial_volume_display),
            h("td", { class: "num" }, t.remaining_display),
            h(
              "td",
              { style: "min-width:220px" },
              h("div", { class: "small" }, compositionText(t.composition)),
              compositionBar(t.composition, true)
            )
          )
        )
      )
    );
    tableWrap.replaceChildren(table);
  }

  filterText.addEventListener("input", debounce(drawTable, 120));
  filterState.addEventListener("change", drawTable);
  drawTable();

  return h(
    "div",
    {},
    stats,
    h(
      "div",
      { class: "panel" },
      h(
        "div",
        { class: "panel-head" },
        h("h2", {}, state.seq === null ? "当前库存" : `历史库存（截至操作 #${state.seq}）`),
        h("span", { class: "hint" }, "点击编号可查看该管的来源与去向；组成按来样批次摊分。"),
        h("span", { style: "flex:1" }),
        seqControl(reload)
      ),
      h(
        "div",
        { class: "row", style: "margin-bottom:12px" },
        h("div", { style: "flex:2" }, filterText),
        h("div", { style: "flex:0 0 180px" }, filterState)
      ),
      tableWrap
    )
  );
}

function openTube(code) {
  state.tab = "trace";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "trace"));
  render().then(() => {
    const input = $("#trace-tube-code");
    if (input) {
      input.value = code;
      input.dispatchEvent(new Event("change"));
    }
  });
}

// ------------------------------------------------------------------ 来样登记

function renderIntake() {
  const code = h("input", { type: "text", placeholder: "例如 A-20261114-01", required: true });
  const batch = h("input", { type: "text", placeholder: "例如 2026-11-14-LOT3", required: true });
  const volume = h("input", { type: "text", placeholder: "例如 1000 或 12.5", required: true });
  const note = h("textarea", { placeholder: "可选：来源、外观、交接人…" });
  const form = h("form", { class: "panel" });
  const hint = h("p", { class: "help" }, "编号全库唯一（不区分大小写）。用量以微升登记，可含小数或分数。");

  const submit = h("button", { class: "btn", type: "submit" }, "登记来样");
  form.append(
    h("div", { class: "panel-head" }, h("h2", {}, "登记来样"), hint),
    h(
      "div",
      { class: "grid two" },
      h("div", {},
        h("label", { class: "field" }, h("span", {}, "来样管编号 ", h("span", { class: "req" }, "*")), code),
        h("label", { class: "field" }, h("span", {}, "来样批次 ", h("span", { class: "req" }, "*")), batch),
        h("label", { class: "field" }, h("span", {}, "来样量（µL） ", h("span", { class: "req" }, "*")), volume)
      ),
      h("div", {},
        h("label", { class: "field" }, h("span", {}, "备注"), note),
        h("div", { class: "chips", style: "margin-top:6px" },
          h("button", { type: "button", class: "chip", onclick: async () => {
            try {
              const r = await api("/api/suggest-codes?count=3");
              code.value = r.codes[0];
              toast(`建议编号 ${r.codes.join("、")}`);
            } catch (e) { showError(e); }
          } }, "生成建议编号")
        )
      )
    ),
    h("div", { class: "row", style: "margin-top:8px" }, h("div", {}), submit)
  );

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    submit.disabled = true;
    try {
      const result = await api("/api/intake", {
        method: "POST",
        body: {
          code: code.value,
          batch: batch.value,
          volume: volume.value,
          note: note.value,
        },
      });
      toast(`已登记来样 ${result.outputs[0].code}（操作 #${result.id}）`);
      code.value = "";
      volume.value = "";
      note.value = "";
      await render();
    } catch (err) {
      showError(err, "来样登记未提交");
    } finally {
      submit.disabled = false;
    }
  });

  const intakeRows = state.inventory.tubes
    .filter((t) => t.kind === "intake")
    .sort((a, b) => b.id - a.id);
  const list = h(
    "div",
    { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "已登记的来样")),
    intakeRows.length
      ? h(
          "table",
          {},
          h("thead", {}, h("tr", {}, ["编号", "批次", "初始量 µL", "剩余 µL", "状态", "登记时间"].map((x, i) =>
            h("th", { class: i === 2 || i === 3 ? "num" : "" }, x)))),
          h("tbody", {}, intakeRows.map((t) =>
            h("tr", {},
              h("td", {}, h("button", { class: "btn link mono", onclick: () => openTube(t.code) }, t.code)),
              h("td", {}, t.batch || "—"),
              h("td", { class: "num" }, t.initial_volume_display),
              h("td", { class: "num" }, t.remaining_display),
              h("td", {}, statusBadge(t)),
              h("td", { class: "small muted" }, fmtTime(t.created_at))
            )))
        )
      : h("div", { class: "empty" }, "还没有来样，先在上方登记。")
  );

  return h("div", {}, form, list);
}

// ------------------------------------------------------------------ 取样 / 分装

function newLine(kind) {
  return { tube: "", amount: "", code: "" };
}

function renderSplit() {
  const inputLines = [newLine("in")];
  const outputLines = [newLine("out")];
  const note = h("textarea", { placeholder: "可选：操作人、用途…" });
  const inBox = h("div", {});
  const outBox = h("div", {});
  const totalsBox = h("div", { class: "totals" });
  const submit = h("button", { class: "btn", type: "submit" }, "提交取样 / 分装");

  const available = () => state.inventory.tubes.filter((t) => t.status === "available");
  const tubesByCode = () => new Map(state.inventory.tubes.map((t) => [t.code.toLowerCase(), t]));

  function parseAmount(raw) {
    const text = String(raw ?? "").trim();
    if (!text) return { ok: false, empty: true };
    const m = text.match(/^(\d+(?:\.\d+)?)(?:\s*\/\s*(\d+(?:\.\d+)?))?$/);
    if (!m) return { ok: false };
    let value;
    try {
      if (m[2] !== undefined) {
        const den = Number(m[2]);
        if (!Number.isFinite(den) || den === 0) return { ok: false };
        value = Number(m[1]) / den;
      } else {
        value = Number(m[1]);
      }
    } catch {
      return { ok: false };
    }
    if (!Number.isFinite(value) || value <= 0) return { ok: false };
    return { ok: true, value };
  }

  function drawLines() {
    const map = tubesByCode();

    inBox.replaceChildren(
      ...[
      h("div", { class: "section-title" }, "取样来源（可从多管取）"),
      ...inputLines.map((line, idx) =>
        h("div", { class: "line-editor" },
          h("div", { class: "row" },
            h("label", { class: "field", style: "flex:2;margin:0" },
              h("span", {}, `来源管 ${idx + 1}`),
              (() => {
                const input = h("input", {
                  type: "text",
                  list: "tube-codes",
                  value: line.tube,
                  placeholder: "输入编号，或从列表选择",
                  oninput: (e) => { line.tube = e.target.value; updateTotals(); },
                  onchange: () => drawLines(),
                });
                return input;
              })()
            ),
            h("label", { class: "field", style: "flex:1;margin:0" },
              h("span", {}, "取样量 µL"),
              h("input", {
                type: "text",
                value: line.amount,
                placeholder: "如 100 或 1/3",
                oninput: (e) => { line.amount = e.target.value; updateTotals(); },
              })
            ),
            h("button", {
              type: "button",
              class: "btn ghost del small",
              disabled: inputLines.length === 1,
              onclick: () => { inputLines.splice(idx, 1); drawLines(); updateTotals(); },
            }, "删除")
          ),
          (() => {
            const t = map.get((line.tube || "").toLowerCase());
            if (!t) return null;
            const cls = t.status === "available" ? "help" : "help";
            return h("div", { class: cls },
              `当前 ${t.status === "available" ? "可用" : STATUS_BADGE[t.status][1]} · 剩余 ${t.remaining_display} µL · 组成 ${compositionText(t.composition)}` +
              (t.recalled && t.status === "available" ? " · 召回禁取样" : "")
            );
          })()
        )
      ),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { inputLines.push(newLine("in")); drawLines(); } }, "+ 增加来源管")
      ]
    );

    outBox.replaceChildren(
      ...[
      h("div", { class: "section-title" }, "分装成品（可往多管分装）"),
      ...outputLines.map((line, idx) =>
        h("div", { class: "line-editor" },
          h("div", { class: "row" },
            h("label", { class: "field", style: "flex:2;margin:0" },
              h("span", {}, `成品管 ${idx + 1} 编号`),
              h("input", {
                type: "text",
                value: line.code,
                placeholder: "新管编号（全库唯一）",
                oninput: (e) => { line.code = e.target.value; },
              })
            ),
            h("label", { class: "field", style: "flex:1;margin:0" },
              h("span", {}, "分装量 µL"),
              h("input", {
                type: "text",
                value: line.amount,
                placeholder: "如 100",
                oninput: (e) => { line.amount = e.target.value; updateTotals(); },
              })
            ),
            h("button", {
              type: "button",
              class: "btn ghost del small",
              disabled: outputLines.length === 1,
              onclick: () => { outputLines.splice(idx, 1); drawLines(); updateTotals(); },
            }, "删除")
          )
        )
      ),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { outputLines.push(newLine("out")); drawLines(); } }, "+ 增加成品管")
      ]
    );
    updateTotals();
  }

  function updateTotals() {
    const inTotal = inputLines.reduce((sum, l) => {
      const p = parseAmount(l.amount);
      return sum + (p.ok ? p.value : 0);
    }, 0);
    const outTotal = outputLines.reduce((sum, l) => {
      const p = parseAmount(l.amount);
      return sum + (p.ok ? p.value : 0);
    }, 0);
    const diff = inTotal - outTotal;
    const matches = Math.abs(diff) < 1e-9;
    totalsBox.replaceChildren(
      ...[
        h("span", { class: "t-item" }, `取样合计 ${round(inTotal)} µL`),
        h("span", { class: "t-item" }, `分装合计 ${round(outTotal)} µL`),
        h("span", { class: matches ? "match" : "mismatch" }, matches ? "✓ 总量一致" : `✗ 差额 ${round(diff)} µL`),
        h("span", { class: "muted small" }, "提交时按精确分数校验，界面小数仅供预览。"),
      ]
    );
    submit.disabled = false;
  }

  function round(n) {
    return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(6)));
  }

  drawLines();

  const form = h("form", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "取样 / 混合分装"),
      h("p", { class: "hint" }, "逐支成品按各来源取样量比例摊分；用量不足、编号冲突或任一项有误，整次操作撤回。")
    ),
    h("datalist", { id: "tube-codes" }, available().map((t) => h("option", { value: t.code }, `${t.code} · 剩 ${t.remaining_display} µL`))),
    h("div", { class: "grid two" }, inBox, outBox),
    totalsBox,
    h("label", { class: "field" }, h("span", {}, "备注"), note),
    h("div", { class: "row" }, h("div", {}), submit)
  );

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    submit.disabled = true;
    const inputs = inputLines.map((l) => ({ tube: l.tube.trim(), amount: l.amount.trim() }));
    const outputs = outputLines.map((l) => ({ code: l.code.trim(), amount: l.amount.trim() }));
    try {
      const result = await api("/api/split", { method: "POST", body: { inputs, outputs, note: note.value } });
      toast(`操作 #${result.id} 已提交：${result.total_in_display} µL 已分装`);
      await render();
    } catch (err) {
      showError(err, "操作未提交，已整次撤回");
    } finally {
      submit.disabled = false;
    }
  });

  return form;
}

// ------------------------------------------------------------------ 报废

function renderRetire() {
  const selected = new Set();
  const note = h("textarea", { placeholder: "可选：报废原因…" });
  const listBox = h("div", {});
  const submit = h("button", { class: "btn danger", type: "submit" }, "确认报废所选管");

  function drawList() {
    const rows = state.inventory.tubes;
    if (!rows.length) {
      listBox.replaceChildren(h("div", { class: "empty" }, "台账中还没有管"));
      return;
    }
    listBox.replaceChildren(h("table", { class: "mini-table" },
      h("thead", {}, h("tr", {},
        h("th", {}), h("th", {}, "编号"), h("th", {}, "类型"), h("th", {}, "批次"),
        h("th", {}, "状态"), h("th", { class: "num" }, "剩余 µL"), h("th", {}, "组成")
      )),
      h("tbody", {}, rows.map((t) =>
        h("tr", {},
          h("td", {}, h("input", {
            type: "checkbox",
            disabled: t.status === "retired",
            checked: selected.has(t.code),
            onchange: (e) => {
              if (e.target.checked) selected.add(t.code); else selected.delete(t.code);
              updateCount();
            },
          })),
          h("td", { class: "mono" }, t.code),
          h("td", {}, t.kind === "intake" ? "来样" : "分装/混合"),
          h("td", {}, t.batch || "—"),
          h("td", {}, statusBadge(t)),
          h("td", { class: "num" }, t.remaining_display),
          h("td", { class: "small" }, compositionText(t.composition))
        )))
    ));
    updateCount();
  }

  const countLabel = h("span", { class: "muted small" });
  function updateCount() {
    countLabel.textContent = `已选 ${selected.size} 管`;
  }

  drawList();

  const form = h("form", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "报废"),
      h("p", { class: "hint" }, "报废后剩余量记为废弃量，管仍可在追溯与历史中查到，但不能再被取样。"),
      h("span", { style: "flex:1" }),
      countLabel
    ),
    listBox,
    h("label", { class: "field", style: "margin-top:12px" }, h("span", {}, "备注"), note),
    h("div", { class: "row" }, h("div", {}), submit)
  );

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!selected.size) {
      toast("请先勾选要报废的管", "warn");
      return;
    }
    if (!confirm(`确认报废 ${selected.size} 管？报废后不可取样，且不可撤销。`)) return;
    submit.disabled = true;
    try {
      const result = await api("/api/retire", {
        method: "POST",
        body: { tubes: [...selected], note: note.value },
      });
      toast(`操作 #${result.id} 已报废 ${result.retired.length} 管`);
      await render();
    } catch (err) {
      showError(err, "报废未提交");
    } finally {
      submit.disabled = false;
    }
  });

  return form;
}

// ------------------------------------------------------------------ 召回

function renderRecall() {
  const batchInput = h("input", { type: "text", placeholder: "输入来样批次", list: "batch-list" });
  const note = h("textarea", { placeholder: "可选：问题描述、隔离要求…" });
  const previewBox = h("div", {});
  const submit = h("button", { class: "btn danger", type: "submit" }, "发起召回");

  async function preview() {
    const batch = batchInput.value.trim();
    if (!batch) {
      previewBox.replaceChildren();
      submit.disabled = true;
      return;
    }
    try {
      const data = await api(`/api/batch?batch=${encodeURIComponent(batch)}`);
      const inStock = data.tubes.filter((t) => t.status === "available");
      previewBox.replaceChildren(
        h("div", { class: `alert ${inStock.length ? "warn" : "info"}` },
          inStock.length
            ? `批次 ${data.batch} 当前有 ${inStock.length} 管在库且含该来样，将被标记禁取样。`
            : `批次 ${data.batch} 当前没有仍在库的受影响管；召回记录仍会保存。`
        ),
        h("table", { class: "mini-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "受影响管"), h("th", {}, "状态"), h("th", { class: "num" }, "剩余 µL"),
            h("th", {}, "该批占比"), h("th", {}, "组成")
          )),
          h("tbody", {}, inStock.length
            ? inStock.map((t) =>
                h("tr", {},
                  h("td", { class: "mono" }, t.code),
                  h("td", {}, statusBadge(t)),
                  h("td", { class: "num" }, t.batch_amount_remaining_display),
                  h("td", { class: "num" }, `${t.batch_share_percent}%`),
                  h("td", { class: "small" }, compositionText(t.composition))
                ))
            : h("tr", {}, h("td", { colspan: "5", class: "muted" }, "无")))
        )
      );
      submit.disabled = inStock.length === 0;
    } catch (err) {
      previewBox.replaceChildren(h("div", { class: "alert error" }, err.message));
      submit.disabled = true;
    }
  }

  batchInput.addEventListener("change", preview);
  batchInput.addEventListener("input", debounce(preview, 350));

  const form = h("form", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "发起召回"),
      h("p", { class: "hint" }, "召回只标记仍在库且含该来样的管，阻止其被再次取样；其他管照常使用。所有受影响清单与当时用量都会留档。")
    ),
    h("datalist", { id: "batch-list" }, state.inventory.batches.map((b) => h("option", { value: b }))),
    h("label", { class: "field" }, h("span", {}, "来样批次 ", h("span", { class: "req" }, "*")), batchInput),
    previewBox,
    h("label", { class: "field" }, h("span", {}, "备注"), note),
    h("div", { class: "row" }, h("div", {}), submit)
  );

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!confirm(`确认对批次 ${batchInput.value.trim()} 发起召回？`)) return;
    submit.disabled = true;
    try {
      const result = await api("/api/recall", {
        method: "POST",
        body: { batch: batchInput.value, note: note.value },
      });
      toast(`召回已发起：${result.recall.affected_count} 管被标记禁取样`);
      await render();
    } catch (err) {
      showError(err, "召回未提交");
    } finally {
      submit.disabled = false;
    }
  });

  const records = state.recalls.recalls;
  const history = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "召回记录"),
      h("span", { class: "hint" }, "显示发起当时受影响的管与用量；括号内为当前状态。")),
    records.length
      ? h("div", { class: "stack" }, records.slice().reverse().map((r) =>
          h("div", { class: "line-editor", style: "background:#fff" },
            h("div", { class: "row", style: "align-items:baseline" },
              h("strong", {}, `召回 #${r.id}`),
              h("span", { class: "badge danger" }, `批次 ${r.batch}`),
              h("span", { class: "muted small" }, fmtTime(r.created_at)),
              h("span", { class: "muted small" }, `关联操作 #${r.op_id}`),
              h("span", { style: "flex:1" }),
              h("span", { class: "muted small" }, `受影响 ${r.affected_count} 管`)
            ),
            r.note ? h("p", { class: "small muted", style: "margin:4px 0" }, r.note) : null,
            r.affected.length
              ? h("table", { class: "mini-table" },
                  h("thead", {}, h("tr", {},
                    h("th", {}, "管"), h("th", {}, "当时剩余 µL"), h("th", {}, "当时组成"),
                    h("th", {}, "当前状态"), h("th", {}, "当前剩余 µL")
                  )),
                  h("tbody", {}, r.affected.map((a) =>
                    h("tr", {},
                      h("td", { class: "mono" }, a.code),
                      h("td", { class: "num" }, a.remaining_display),
                      h("td", { class: "small" }, compositionText(a.composition)),
                      h("td", {}, h("span", { class: "badge neutral" }, a.current_status === "available" ? "仍在库" : a.current_status === "retired" ? "已报废" : a.current_status === "exhausted" ? "已用尽" : a.current_status === "voided" ? "已冲销失效" : a.current_status)),
                      h("td", { class: "num" }, a.current_remaining_display ?? "—")
                    )))
                )
              : h("p", { class: "muted small" }, "发起时没有仍在库的受影响管。")
          )
        ))
      : h("div", { class: "empty" }, "还没有召回记录")
  );

  return h("div", {}, form, history);
}

// ------------------------------------------------------------------ 冲销更正

function openReverse(opId) {
  state.reverseOp = opId;
  state.tab = "reverse";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "reverse"));
  render();
}

function renderReverse() {
  const opInput = h("input", {
    type: "number",
    min: "1",
    step: "1",
    placeholder: "输入要冲销的分装操作号，例如 3",
  });
  const note = h("textarea", { placeholder: "可选：冲销原因，例如“分装量录错，实际未执行”…" });
  const previewBox = h("div", {});
  let preview = null;
  let busy = false;

  async function runPreview() {
    const raw = opInput.value.trim();
    preview = null;
    if (!raw) {
      previewBox.replaceChildren(
        h("div", { class: "empty" }, "输入操作序号后会自动预览影响；确认前不会改动任何数据。")
      );
      return;
    }
    previewBox.replaceChildren(h("div", { class: "loading" }, "正在计算影响…"));
    try {
      preview = await api(`/api/reverse/preview?op=${encodeURIComponent(raw)}`);
    } catch (err) {
      previewBox.replaceChildren(
        h("div", { class: "alert error" },
          err.message,
          (err.details || []).length
            ? h("ul", {}, err.details.map((d) => h("li", {}, d)))
            : null)
      );
      return;
    }
    drawPreview();
  }

  function opLine(op) {
    const ins = (op.inputs || []).map((i) => `${i.code} ${i.amount_display} µL`).join("、");
    const outs = (op.outputs || []).map((o) => `${o.code} ${o.amount_display} µL`).join("、");
    return `#${op.id}（${fmtTime(op.created_at)}）：取 ${ins || "—"} → 分 ${outs || "—"}`;
  }

  function drawPreview() {
    const p = preview;
    const confirmBtn = h(
      "button",
      { class: "btn danger", disabled: !p.ok || busy, onclick: doReverse },
      p.ok ? "确认整批冲销" : "存在问题，无法冲销"
    );
    previewBox.replaceChildren(
      h("div", { class: "panel", style: "background:#fbfcfe" },
        h("div", { class: "panel-head" },
          h("h2", {}, `影响预览：冲销分装操作 #${p.root_op}`),
          h("span", { class: "hint" }, `基于操作 #${p.latest_seq} 的库存计算`)
        ),
        p.problems.length
          ? h("div", { class: "alert error" },
              h("strong", {}, "存在阻止冲销的问题："),
              h("ul", {}, p.problems.map((d) => h("li", {}, d))))
          : h("div", { class: "alert info" },
              `将整单作废 ${p.void_ops.length} 笔分装` +
              (p.void_retire_items.length ? `、作废 ${p.void_retire_items.length} 条报废明细` : "") +
              `；${p.doomed_tubes.length} 支管将视为从未存在；` +
              `误扣用量退回 ${p.refunds.length} 支仍有效的原管。其他操作与已发召回不受影响。`),
        h("div", { class: "section-title" }, "退回用量（退回仍有效的原管）"),
        p.refunds.length
          ? h("table", { class: "mini-table" },
              h("thead", {}, h("tr", {},
                h("th", {}, "原管"), h("th", { class: "num" }, "退回 µL"),
                h("th", { class: "num" }, "剩余（前 → 后）"), h("th", {}, "退回后状态"))),
              h("tbody", {}, p.refunds.map((r) =>
                h("tr", {},
                  h("td", { class: "mono" }, r.code),
                  h("td", { class: "num" }, `+${r.amount_display}`),
                  h("td", { class: "num" }, `${r.remaining_before_display} → ${r.remaining_after_display}`),
                  h("td", {}, r.recalled
                    ? h("span", { class: "badge danger" }, `召回中（${r.recall_batches.join("、")}），退回后仍禁取样`)
                    : h("span", { class: "badge ok" }, "可用"))))))
          : h("p", { class: "muted small" }, "没有需要退回的用量。"),
        h("div", { class: "section-title" }, "将整单作废的分装操作"),
        h("div", { class: "stack" }, p.void_ops.map((op) =>
          h("div", { class: "small" }, "• " + opLine(op)))),
        p.void_retire_items.length
          ? h("div", {},
              h("div", { class: "section-title" }, "将作废的报废明细（同单其他管不受影响）"),
              h("div", { class: "stack" }, p.void_retire_items.map((r) =>
                h("div", { class: "small" }, `• 报废操作 #${r.op_id} 中的 ${r.code}（当时废弃 ${r.discarded_display} µL）`))))
          : null,
        h("div", { class: "section-title" }, "将视为从未存在的管"),
        p.doomed_tubes.length
          ? h("table", { class: "mini-table" },
              h("thead", {}, h("tr", {},
                h("th", {}, "编号"), h("th", { class: "num" }, "初始 µL"),
                h("th", { class: "num" }, "当前剩余 µL"), h("th", {}, "建立于"))),
              h("tbody", {}, p.doomed_tubes.map((t) =>
                h("tr", {},
                  h("td", { class: "mono" }, t.code),
                  h("td", { class: "num" }, t.initial_volume_display),
                  h("td", { class: "num" }, t.remaining_display),
                  h("td", {}, `操作 #${t.created_op}`)))))
          : h("p", { class: "muted small" }, "无。"),
        h("label", { class: "field", style: "margin-top:12px" }, h("span", {}, "冲销备注"), note),
        h("div", { class: "row", style: "align-items:center" },
          h("span", { class: "muted small" },
            "提交后原操作保留并标注“已冲销”，可随时按序号回看冲销前的账；" +
            "提交前若相关库存有变动需重新预览确认；重复提交不会重复退回。"),
          h("div", { class: "shrink" }, confirmBtn))
      )
    );
  }

  async function doReverse() {
    if (!preview || busy) return;
    busy = true;
    drawPreview();
    try {
      const result = await api("/api/reverse", {
        method: "POST",
        body: { op: preview.root_op, token: preview.token, note: note.value },
      });
      const rev = result.reversal;
      toast(
        `冲销完成（操作 #${result.operation.id}）：作废 ${rev.void_ops.length} 笔分装，` +
        `退回 ${rev.refunds.map((r) => `${r.code} +${r.amount_display} µL`).join("、") || "无"}`
      );
      state.reverseOp = null;
      await render();
    } catch (err) {
      if (err.status === 409) {
        // 预览之后相关库存有变动：强制重新预览确认
        showError(err, "库存已变动，请重新确认下面的最新影响");
        busy = false;
        await runPreview();
        return;
      }
      showError(err, "冲销未提交");
      busy = false;
      drawPreview();
    }
  }

  opInput.addEventListener("change", runPreview);
  const previewBtn = h("button", { class: "btn", type: "button", onclick: runPreview }, "预览影响");

  const form = h("div", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "冲销误录的分装"),
      h("p", { class: "hint" },
        "整批作废这笔分装和依赖它的后续混样、报废；误扣的用量退回仍有效的原管，其他取样照旧。" +
        "已发的召回继续有效，退回召回管的量也不能取用。")
    ),
    h("div", { class: "row" },
      h("div", { style: "flex:2" },
        h("label", { class: "field", style: "margin:0" },
          h("span", {}, "分装操作号 ", h("span", { class: "req" }, "*")), opInput)),
      h("div", { class: "shrink" }, previewBtn)
    )
  );

  previewBox.replaceChildren(
    h("div", { class: "empty" }, "输入操作序号后会自动预览影响；确认前不会改动任何数据。")
  );
  if (state.reverseOp != null) {
    opInput.value = String(state.reverseOp);
    state.reverseOp = null;
    runPreview();
  }

  return h("div", {}, form, previewBox);
}

// ------------------------------------------------------------------ 追溯

function renderTrace() {
  const mode = h("select", {}, h("option", { value: "tube" }, "按管追溯（构成 →）"), h("option", { value: "batch" }, "按来样批次追溯（→ 去向）"));
  const q = h("input", { type: "text", placeholder: "输入管编号或来样批次", id: "trace-tube-code" });
  const result = h("div", {});
  const form = h("form", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "追溯查询"),
      h("span", { class: "hint" }, "按管看它由哪些来样组成；按批次看这批来样流向哪些管、当时各有多少、现在还剩多少。"),
      h("span", { style: "flex:1" }),
      seqControl(() => render())
    ),
    h("div", { class: "row" },
      h("div", { style: "flex:0 0 220px" }, mode),
      h("div", { style: "flex:2" }, q),
      h("div", { class: "shrink" }, h("button", { class: "btn", type: "submit" }, "查询"))
    )
  );

  const seqSuffix = state.seq === null ? "" : `&seq=${state.seq}`;

  async function run() {
    const value = q.value.trim();
    if (!value) return;
    result.replaceChildren(h("div", { class: "loading" }, "查询中…"));
    try {
      if (mode.value === "tube") {
        const data = await api(`/api/tube?code=${encodeURIComponent(value)}${seqSuffix}`);
        result.replaceChildren(renderTubeTrace(data));
      } else {
        const data = await api(`/api/batch?batch=${encodeURIComponent(value)}${seqSuffix}`);
        result.replaceChildren(renderBatchTrace(data));
      }
    } catch (err) {
      result.replaceChildren(h("div", { class: "alert error" }, err.message));
    }
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    run();
  });
  q.addEventListener("change", run);
  mode.addEventListener("change", () => {
    q.placeholder = mode.value === "tube" ? "输入管编号" : "输入来样批次";
    result.replaceChildren();
  });

  return h("div", {}, form, result);
}

function renderTubeTrace(data) {
  const t = data.tube;
  const lineageTree = (node, depth = 0) => {
    const isIntake = node.kind === "intake";
    return h("li", {},
      h("div", { class: "row", style: "align-items:baseline" },
        h("span", { class: "mono", style: "font-weight:600" }, node.code),
        isIntake
          ? h("span", { class: "badge neutral" }, `来样 ${node.batch || ""}`)
          : h("span", { class: "badge info" }, "混合/分装"),
        node.amount_taken
          ? h("span", { class: "small muted" }, `本次取用 ${node.amount_taken_display} µL`)
          : null,
        h("span", {}, statusBadge(node)),
        h("span", { class: "small muted" }, compositionText(node.composition))
      ),
      node.children && node.children.length
        ? h("ul", { class: "timeline", style: "margin-top:8px" }, node.children.map((c) => lineageTree(c, depth + 1)))
        : null
    );
  };

  return h("div", {},
    h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        h("h2", {}, `管 ${t.code}`),
        h("span", { class: "badge " + (t.kind === "intake" ? "neutral" : "info") }, t.kind === "intake" ? "来样" : "分装/混合"),
        h("span", {}, statusBadge(t)),
        recalledBadge(t),
        h("span", { class: "muted small" }, `建立于操作 #${t.created_op} · ${fmtTime(t.created_at)}`)
      ),
      h("dl", { class: "kv", style: "margin-bottom:14px" },
        h("dt", {}, "初始量"), h("dd", {}, `${t.initial_volume_display} µL`),
        h("dt", {}, "剩余量"), h("dd", {}, `${t.remaining_display} µL`),
        t.batch ? h("dt", {}, "来样批次") : null,
        t.batch ? h("dd", {}, t.batch) : null,
        t.status === "retired" ? h("dt", {}, "报废") : null,
        t.status === "retired" ? h("dd", {}, `操作 #${t.retired_op} · 废弃 ${t.discarded_display} µL · ${fmtTime(t.retired_at)}`) : null,
        h("dt", {}, "备注"), h("dd", {}, t.note || "—")
      ),
      h("div", { class: "section-title" }, "组成（来自哪些来样）"),
      compositionBar(data.composition),
      h("table", { class: "mini-table", style: "margin-top:10px" },
        h("thead", {}, h("tr", {},
          h("th", {}, "来样批次"), h("th", {}, "来样管"), h("th", { class: "num" }, "占比"),
          h("th", { class: "num" }, "折合初始量 µL"), h("th", { class: "num" }, "折合剩余量 µL")
        )),
        h("tbody", {}, data.composition.map((c) =>
          h("tr", {},
            h("td", {}, c.batch || "—"),
            h("td", {}, h("button", { class: "btn link mono", onclick: () => openTube(c.intake_code) }, c.intake_code)),
            h("td", { class: "num" }, `${c.percent}%`),
            h("td", { class: "num" }, c.amount_in_initial_display),
            h("td", { class: "num" }, c.amount_in_remaining_display)
          )))
      )
    ),
    h("div", { class: "grid two" },
      h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h2", {}, "来源链"), h("span", { class: "hint" }, "这管由哪些管、按多少量混合而来。")),
        h("ul", { class: "timeline" }, lineageTree(data.lineage))
      ),
      h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h2", {}, "去向"), h("span", { class: "hint" }, "这管被取用到了哪些操作与成品。")),
        data.used_in.length
          ? h("ul", { class: "timeline" }, data.used_in.map((u) =>
              h("li", {},
                h("div", {}, h("strong", {}, `操作 #${u.op_id}`), " ", h("span", { class: "badge neutral" }, u.kind === "split" ? "取样/分装" : u.kind === "retire" ? "报废" : u.kind),
                  u.voided_by ? h("span", { class: "badge danger" }, `已冲销 #${u.voided_by}`) : null),
                h("div", { class: "small muted" }, `${fmtTime(u.created_at)} · 取用 ${u.amount_display} µL`),
                u.outputs.length
                  ? h("div", { class: "small" }, "→ " + u.outputs.map((o) => `${o.code}（${o.amount_display} µL）`).join("、"))
                  : null
              )))
          : h("div", { class: "empty" }, "尚未被取用"),
        data.retired_at.length
          ? h("div", { class: "alert warn", style: "margin-top:12px" },
              data.retired_at.map((r) => `操作 #${r.op_id} 报废，废弃 ${r.discarded_display} µL（${fmtTime(r.created_at)}）`).join("；"))
          : null
      )
    )
  );
}

function renderBatchTrace(data) {
  const inStock = data.tubes.filter((t) => t.status === "available");
  return h("div", {},
    h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        h("h2", {}, `来样批次 ${data.batch}`),
        h("span", { class: "badge info" }, `来样管 ${data.intake_tubes.length} 支`),
        h("span", { class: "badge " + (inStock.length ? "ok" : "neutral") }, `在库含此批 ${inStock.length} 管`)
      ),
      h("table", { class: "mini-table" },
        h("thead", {}, h("tr", {},
          h("th", {}, "来样管"), h("th", {}, "状态"), h("th", { class: "num" }, "初始 µL"),
          h("th", { class: "num" }, "剩余 µL"), h("th", {}, "登记时间")
        )),
        h("tbody", {}, data.intake_tubes.map((t) =>
          h("tr", {},
            h("td", {}, h("button", { class: "btn link mono", onclick: () => openTube(t.code) }, t.code)),
            h("td", {}, statusBadge(t)),
            h("td", { class: "num" }, t.initial_volume_display),
            h("td", { class: "num" }, t.remaining_display),
            h("td", { class: "small muted" }, fmtTime(t.created_at))
          )))
      )
    ),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        h("h2", {}, "流向的管（含此批来样的全部管）"),
        h("span", { class: "hint" }, "“当时”为该管建立时的量，“现在”为当前（或所选历史序号）剩余。")
      ),
      data.tubes.length
        ? h("table", {},
            h("thead", {}, h("tr", {},
              h("th", {}, "管"), h("th", {}, "类型"), h("th", {}, "状态"), h("th", {}, "召回"),
              h("th", { class: "num" }, "此批占比"), h("th", { class: "num" }, "此批初始 µL"),
              h("th", { class: "num" }, "此批剩余 µL"), h("th", { class: "num" }, "整管剩余 µL"),
              h("th", {}, "组成")
            )),
            h("tbody", {}, data.tubes.map((t) =>
              h("tr", {},
                h("td", {}, h("button", { class: "btn link mono", onclick: () => openTube(t.code) }, t.code)),
                h("td", {}, t.kind === "intake" ? h("span", { class: "badge neutral" }, "来样") : h("span", { class: "badge info" }, "分装/混合")),
                h("td", {}, statusBadge(t)),
                h("td", {}, recalledBadge(t)),
                h("td", { class: "num" }, `${t.batch_share_percent}%`),
                h("td", { class: "num" }, t.batch_amount_initial_display),
                h("td", { class: "num" }, t.batch_amount_remaining_display),
                h("td", { class: "num" }, t.remaining_display),
                h("td", { class: "small" }, compositionText(t.composition))
              )))
          )
        : h("div", { class: "empty" }, "没有含此批来样的管")
    ),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "相关操作"), h("span", { class: "hint" }, "按登记先后排列。")),
      data.flow.length
        ? h("div", { class: "stack" }, data.flow.map((op) => OperationCard(op)))
        : h("div", { class: "empty" }, "没有相关操作")
    )
  );
}

// ------------------------------------------------------------------ 操作历史

function OperationCard(op) {
  const kindLabel = {
    intake: ["neutral", "来样登记"],
    split: ["info", "取样 / 分装"],
    retire: ["warn", "报废"],
    recall: ["danger", "召回"],
    reverse: ["warn", "冲销"],
  }[op.kind] || ["neutral", op.kind];
  const [cls, label] = kindLabel;

  const inputList = op.inputs.length
    ? h("table", { class: "mini-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "取样管"), h("th", {}, "批次"), h("th", { class: "num" }, "取用量 µL"), h("th", { class: "num" }, "占比"))),
        h("tbody", {}, op.inputs.map((i) =>
          h("tr", {},
            h("td", { class: "mono" }, i.code),
            h("td", {}, i.batch || "—"),
            h("td", { class: "num" }, i.amount_display),
            h("td", { class: "num" }, (op.input_total_map && op.input_total_map[i.tube_id]?.share_percent) ? op.input_total_map[i.tube_id].share_percent + "%" : "—")
          )))
      )
    : null;

  const isIntake = op.kind === "intake";
  const outputList = op.outputs.length
    ? h("table", { class: "mini-table" },
        h("thead", {}, h("tr", {},
          h("th", {}, isIntake ? "来样管" : "成品管"),
          h("th", { class: "num" }, isIntake ? "登记量 µL" : "分装量 µL"),
          h("th", {}, isIntake ? "批次" : "逐管实际来源")
        )),
        h("tbody", {}, op.outputs.map((o) =>
          h("tr", {},
            h("td", { class: "mono" }, o.code),
            h("td", { class: "num" }, o.amount_display),
            h("td", { class: "small" }, isIntake
              ? (o.batch || "—")
              : (o.sources && o.sources.length
                  ? o.sources.map((s) => `${s.code}: ${s.amount_display} µL（${s.share_percent}%）`).join("；")
                  : "—"))
          )))
      )
    : null;

  const reversalBlock = op.reversal
    ? h("div", { style: "margin:8px 0" },
        h("div", { class: "section-title", style: "margin-top:4px" },
          `冲销明细（目标：分装操作 #${op.reversal.root_op}）`),
        h("table", { class: "mini-table" },
          h("thead", {}, h("tr", {},
            h("th", {}, "整单作废的操作"), h("th", {}, "取样"), h("th", {}, "分装成品"))),
          h("tbody", {}, op.reversal.void_ops.map((v) =>
            h("tr", {},
              h("td", {}, `#${v.op_id}`),
              h("td", { class: "small" }, v.inputs.length
                ? v.inputs.map((i) => `${i.code} ${i.amount_display} µL`).join("、")
                : "—"),
              h("td", { class: "small" }, v.outputs.length
                ? v.outputs.map((o) => `${o.code} ${o.amount_display} µL`).join("、")
                : "—"))))),
        op.reversal.void_retire_items.length
          ? h("p", { class: "small" },
              "作废的报废明细：" +
              op.reversal.void_retire_items
                .map((r) => `操作 #${r.op_id} 的 ${r.code}（废弃 ${r.discarded_display} µL）`)
                .join("；"))
          : null,
        h("p", { class: "small" },
          "退回用量：" +
          (op.reversal.refunds.length
            ? op.reversal.refunds.map((r) => `${r.code} +${r.amount_display} µL`).join("、")
            : "无")))
    : null;

  return h("div", { class: "line-editor" + (op.voided_by ? " voided" : ""), style: "background:#fff" },
    h("div", { class: "row", style: "align-items:baseline" },
      h("strong", {}, `#${op.id}`),
      h("span", { class: `badge ${cls}` }, label),
      op.voided_by
        ? h("span", { class: "badge danger", title: "该操作已随冲销作废，不再影响库存" },
            `已被冲销 #${op.voided_by}`)
        : null,
      h("span", { class: "muted small" }, fmtTime(op.created_at)),
      op.kind === "split"
        ? h("span", { class: "small muted" }, `取样 ${op.total_in_display} µL = 分装 ${op.total_out_display} µL`)
        : null,
      h("span", { style: "flex:1" }),
      op.kind === "split" && !op.voided_by
        ? h("button", {
            class: "btn ghost danger small",
            title: "整批冲销这笔分装和依赖它的后续操作",
            onclick: () => openReverse(op.id),
          }, "冲销…")
        : null,
      h("button", {
        class: "btn ghost small",
        onclick: () => {
          state.seq = op.id;
          state.tab = "inventory";
          $$(".tab").forEach((t) =>
            t.classList.toggle("active", t.dataset.tab === "inventory")
          );
          render();
        },
      }, "回看此刻库存")
    ),
    op.note ? h("p", { class: "small muted", style: "margin:4px 0" }, op.note) : null,
    op.retired && op.retired.length
      ? h("p", { class: "small" }, "报废：" + op.retired.map((r) =>
          `${r.code}（废弃 ${r.discarded_display} µL）` +
          (r.voided_by ? `［已随冲销 #${r.voided_by} 作废］` : "")
        ).join("、"))
      : null,
    op.recall
      ? h("p", { class: "small" },
          `召回批次 ${op.recall.batch}，当时受影响 ${op.recall.affected_count} 管：` +
          (op.recall.affected.length
            ? op.recall.affected.map((a) => `${a.code}（剩 ${a.remaining_display} µL）`).join("、")
            : "无"))
      : null,
    reversalBlock,
    inputList,
    outputList
  );
}

function renderHistory() {
  const ops = state.history.operations;
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", {}, "操作历史"),
      h("span", { class: "hint" }, "按登记先后排列；点“回看此刻库存”可查该操作完成后的库存与组成。"),
      h("span", { style: "flex:1" }),
      seqControl(() => render())
    ),
    ops.length
      ? h("div", { class: "stack" }, ops.slice().reverse().map((op) => OperationCard(op)))
      : h("div", { class: "empty" }, "还没有任何操作")
  );
}

// ------------------------------------------------------------------ 启动

initTabs();
render().catch((err) => showError(err));
