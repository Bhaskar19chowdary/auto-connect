/* Shared code of the client portal and the bank workbench: API calls, formatting, application forms, timeline. */
(function () {
  const P = {};
  window.P = P;
  P.headers = {};

  // ------------------------------------------------------------------ storage (may be unavailable)
  P.load = (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } };
  P.save = (k, v) => { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };

  // ------------------------------------------------------------------ API
  P.api = async function (path, opts = {}) {
    const init = { method: opts.method || "GET", headers: { ...P.headers } };
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const r = await fetch(path, init);
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (!r.ok) {
      const det = data && data.detail !== undefined ? data.detail : data;
      const err = new Error(typeof det === "string" ? det : (det && det.message) || ("Request failed (" + r.status + ")"));
      err.status = r.status; err.errors = (det && det.errors) || []; throw err;
    }
    return data;
  };
  P.download = async function (url, fallback) {
    const r = await fetch(url, { headers: { ...P.headers } });
    if (!r.ok) { P.toast("Download failed (" + r.status + ")", "err"); return; }
    const blob = await r.blob();
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = (m && m[1]) || fallback || "download";
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };

  // ------------------------------------------------------------------ formatting
  P.esc = (s) => String(s === undefined || s === null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  P.money = (v, ccy) => (v === null || v === undefined || v === "") ? "-" :
    Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (ccy ? " " + ccy : "");
  P.date = (s) => { if (!s) return "-"; const d = new Date(s.length <= 10 ? s + "T00:00:00" : s); return isNaN(d) ? s : d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); };
  P.when = (s) => { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); };
  P.pill = (tone, label) => `<span class="pill ${P.esc(tone || "gray")}">${P.esc(label)}</span>`;
  P.size = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? Math.round(n / 1024) + " KB" : n + " bytes";
  P.addDays = (iso, n) => { const d = new Date(iso + "T00:00:00"); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
  P.greeting = () => { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; };

  P.toast = function (msg, kind) {
    let box = document.getElementById("toast");
    if (!box) { box = document.createElement("div"); box.id = "toast"; document.body.appendChild(box); }
    const t = document.createElement("div"); t.className = "t " + (kind || ""); t.textContent = msg; box.appendChild(t);
    setTimeout(() => t.remove(), kind === "err" ? 7000 : 4000);
  };
  P.modal = function (html) {
    const o = document.createElement("div"); o.className = "overlay";
    o.innerHTML = `<div class="modal">${html}</div>`;
    o.addEventListener("click", (e) => { if (e.target === o || e.target.closest("[data-close]")) o.remove(); });
    document.body.appendChild(o); return o;
  };

  // ------------------------------------------------------------------ building blocks
  P.progress = (steps) => `<div class="progress">${steps.map((s) =>
    `<div class="st ${s.state}"><div class="dot">${s.state === "done" ? "&#10003;" : s.state === "stopped" ? "!" : ""}</div>${P.esc(s.label)}</div>`).join("")}</div>`;

  P.timeline = (items, showTechnical) => {
    const list = items.filter((i) => showTechnical || !i.technical);
    if (!list.length) return `<div class="empty">Nothing yet.</div>`;
    return `<div class="timeline">${list.slice().reverse().map((i) => `<div class="ev ${P.esc(i.tone)}">
      <div class="t">${P.esc(i.title)}</div>${i.text ? `<div class="d">${P.esc(i.text)}</div>` : ""}
      <div class="w">${P.when(i.at)}${i.ref ? " &middot; " + P.esc(i.ref) : ""}${i.customer_name ? " &middot; " + P.esc(i.customer_name) : ""}</div></div>`).join("")}</div>`;
  };

  P.facts = (facts) => `<div class="facts">${facts.map((f) => `<div class="fact"><div class="k">${P.esc(f.label)}</div><div class="v">${P.esc(f.value)}</div></div>`).join("")}</div>`;

  P.docsTable = (docs, urlOf) => !docs.length ? `<div class="empty">No documents uploaded.</div>` :
    `<div class="table-wrap"><table class="list"><thead><tr><th>Document</th><th>Uploaded</th><th>By</th><th class="num">Size</th><th>Fingerprint (SHA-256)</th><th></th></tr></thead><tbody>${
      docs.map((d) => `<tr><td>&#128196; ${P.esc(d.filename)}</td><td>${P.when(d.uploaded_at)}</td><td>${P.esc(d.uploaded_by || "-")}</td>
      <td class="num">${P.size(d.size)}</td><td class="tiny muted" title="${P.esc(d.sha256)}">${P.esc((d.sha256 || "").slice(0, 16))}...</td>
      <td><button class="btn sec small" data-dl="${P.esc(urlOf(d))}" data-name="${P.esc(d.filename)}">Download</button></td></tr>`).join("")}</tbody></table></div>
      <p class="tiny muted" style="margin-top:8px">Originals are stored in the bank's database exactly as uploaded. The fingerprint proves the file was never changed.</p>`;

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-dl]");
    if (b) { e.preventDefault(); P.download(b.dataset.dl, b.dataset.name); }
  });

  // ------------------------------------------------------------------ application forms
  const COUNTRIES = [["DE", "Germany"], ["AT", "Austria"], ["BE", "Belgium"], ["CH", "Switzerland"], ["CN", "China"], ["CZ", "Czech Republic"],
    ["DK", "Denmark"], ["ES", "Spain"], ["FR", "France"], ["GB", "United Kingdom"], ["HU", "Hungary"], ["IN", "India"], ["IT", "Italy"],
    ["JP", "Japan"], ["KR", "South Korea"], ["NL", "Netherlands"], ["NO", "Norway"], ["PL", "Poland"], ["PT", "Portugal"], ["SA", "Saudi Arabia"],
    ["SE", "Sweden"], ["SG", "Singapore"], ["TR", "Turkey"], ["AE", "United Arab Emirates"], ["US", "United States"], ["VN", "Vietnam"],
    ["BR", "Brazil"], ["MX", "Mexico"], ["ZA", "South Africa"], ["EG", "Egypt"]];
  const DOCS = [["COMMERCIAL_INVOICE", "Commercial invoice", 1], ["BILL_OF_LADING", "Bill of lading (sea)", 3], ["AIR_WAYBILL", "Air waybill", 1],
    ["INSURANCE_DOCUMENT", "Insurance certificate", 1], ["PACKING_LIST", "Packing list", 1], ["CERTIFICATE_OF_ORIGIN", "Certificate of origin", 1],
    ["INSPECTION_CERTIFICATE", "Inspection certificate", 1]];
  const INCOTERMS = [["EXW", "EXW - buyer collects at seller's site"], ["FCA", "FCA - free carrier"], ["FOB", "FOB - free on board (sea)"],
    ["CFR", "CFR - cost and freight"], ["CIF", "CIF - cost, insurance, freight"], ["CPT", "CPT - carriage paid to"], ["CIP", "CIP - carriage and insurance paid"],
    ["DAP", "DAP - delivered at place"], ["DDP", "DDP - delivered duty paid"]];

  const party = (prefix, who, help) => ({
    title: who, help, fields: [
      { k: prefix + ".name", label: "Company name", req: true, wide: true },
      { k: prefix + ".address", label: "Address", wide: true },
      { k: prefix + ".country", label: "Country", type: "select", opts: COUNTRIES, req: true },
      { k: prefix + ".email", label: "Email (optional)", hint: "We can inform them directly." },
    ] });
  const money = (help) => ({ title: "Amount", help, fields: [
    { k: "currency", label: "Currency", type: "currency", req: true },
    { k: "amount", label: "Amount", type: "number", req: true, hint: "Without thousand separators, e.g. 125000" } ] });

  P.FORMS = {
    LC: { icon: "&#128674;", name: "Import Letter of Credit", short: "Pay a foreign supplier safely",
      help: "The bank promises your supplier to pay as soon as they show documents proving shipment. You only pay for goods that were really shipped.",
      sections: [
        party("beneficiary", "Who is the seller?", "The supplier who will receive the money."),
        { title: "Seller's bank", help: "The bank that will receive the credit for the seller.", fields: [
          { k: "advising_bank_bic", label: "Seller's bank SWIFT code (BIC)", req: true, hint: "8 or 11 characters, e.g. BNCAITMMXXX" } ] },
        money("How much should the bank guarantee to pay?"),
        { title: "Goods and shipment", help: "What is shipped, from where to where, and by when.", fields: [
          { k: "goods_description", label: "Description of goods", req: true, wide: true, type: "textarea" },
          { k: "incoterms", label: "Delivery terms (Incoterms)", type: "select", opts: INCOTERMS, def: "FOB" },
          { k: "incoterms_place", label: "Place for the delivery terms", hint: "e.g. Genoa" },
          { k: "port_of_loading", label: "Port / place of loading", req: true },
          { k: "port_of_discharge", label: "Port / place of discharge", req: true },
          { k: "latest_shipment_date", label: "Latest shipment date", type: "date", req: true },
          { k: "partial_shipments_allowed", label: "Partial shipments allowed", type: "check" },
          { k: "transhipment_allowed", label: "Transhipment allowed", type: "check" } ] },
        { title: "Payment and validity", help: "When the seller is paid and how long the credit is valid.", fields: [
          { k: "available_by", label: "Payment", type: "select", def: "SIGHT_PAYMENT", opts: [["SIGHT_PAYMENT", "At sight - pay when correct documents arrive"],
            ["DEFERRED_PAYMENT", "Deferred - pay some days later"], ["ACCEPTANCE", "Acceptance of a bill of exchange"], ["NEGOTIATION", "Negotiation"]] },
          { k: "usance_days", label: "Days until payment (if not at sight)", type: "number", def: "0" },
          { k: "expiry_date", label: "Valid until", type: "date", req: true },
          { k: "presentation_period_days", label: "Days to present documents after shipment", type: "number", def: "21" } ] },
        { title: "Documents the seller must present", help: "The bank pays only when exactly these documents are presented.", fields: [
          { k: "documents_required", type: "docs", wide: true, def: ["COMMERCIAL_INVOICE", "BILL_OF_LADING"] } ] },
        { title: "Anything else?", help: "Optional special conditions, one per line.", fields: [
          { k: "additional_conditions", label: "Special conditions", type: "lines", wide: true },
          { k: "confirmation", label: "Should another bank confirm?", type: "select", def: "WITHOUT", opts: [["WITHOUT", "No"], ["MAY ADD", "May add"], ["CONFIRM", "Yes, confirm"]] } ] },
      ] },
    GUARANTEE: { icon: "&#128737;", name: "Bank Guarantee", short: "Secure your contract obligations",
      help: "The bank promises the other party to pay if you do not meet your obligations, for example a performance or advance payment guarantee.",
      sections: [
        party("beneficiary", "Who receives the guarantee?", "The party that is protected by the guarantee."),
        { title: "Type of guarantee", help: "What should the guarantee secure?", fields: [
          { k: "guarantee_type", label: "Guarantee type", type: "select", def: "PERFORMANCE", opts: [["PERFORMANCE", "Performance - you deliver as agreed"],
            ["ADVANCE_PAYMENT", "Advance payment - repay a down payment"], ["WARRANTY", "Warranty - defects after delivery"], ["BID", "Bid / tender"],
            ["PAYMENT", "Payment - you pay your supplier"], ["RETENTION", "Retention money"], ["STANDBY_LC", "Standby letter of credit"]] },
          { k: "beneficiary_bank_bic", label: "Beneficiary's bank BIC (optional)", hint: "Needed if the guarantee goes through a foreign bank" },
          { k: "counter_guarantee", label: "A local bank abroad should issue it (counter-guarantee)", type: "check", wide: true },
          { k: "local_guarantee_expiry", label: "Expiry of the local guarantee (only for counter-guarantees)", type: "date" } ] },
        money("Maximum amount the bank would pay."),
        { title: "Contract and validity", help: "Which contract is secured, and until when.", fields: [
          { k: "underlying_transaction", label: "Contract / project", req: true, wide: true, hint: "e.g. Supply contract 2026-118 for turbine parts" },
          { k: "underlying_contract_value", label: "Total contract value", type: "number" },
          { k: "expiry_date", label: "Valid until", type: "date", req: true },
          { k: "exclude_statement_of_breach", label: "Pay on simple demand (no statement of breach needed)", type: "check", wide: true } ] },
      ] },
    SURETY: { icon: "&#127959;", name: "Surety Bond (Buergschaft)", short: "German-law surety for construction, rent, customs",
      help: "The bank stands in for your obligation under German law (Buergschaft), for example for a building contract, rent deposit or customs.",
      sections: [
        party("obligee", "Who is protected?", "The party that receives the surety (e.g. the client of your building project or the landlord)."),
        { title: "Type of surety", help: "", fields: [
          { k: "bond_type", label: "Surety type", type: "select", def: "CONTRACT_PERFORMANCE", opts: [["CONTRACT_PERFORMANCE", "Contract performance (Vertragserfuellung)"],
            ["WARRANTY", "Warranty (Gewaehrleistung)"], ["ADVANCE_PAYMENT", "Advance payment (Anzahlung)"], ["RENT_DEPOSIT", "Rent deposit (Mietkaution)"],
            ["CUSTOMS", "Customs (Zoll)"], ["BID", "Bid (Bietung)"]] },
          { k: "on_first_demand", label: "Payable on first demand (Buergschaft auf erstes Anfordern)", type: "check", wide: true } ] },
        money("Maximum amount of the surety."),
        { title: "Contract and validity", help: "", fields: [
          { k: "underlying_contract", label: "Contract", req: true, wide: true, hint: "e.g. Construction contract WM-77 (VOB/B)" },
          { k: "contract_value", label: "Contract value", type: "number" },
          { k: "advance_payment_amount", label: "Advance payment amount (if any)", type: "number" },
          { k: "monthly_rent", label: "Monthly rent (rent deposits only)", type: "number" },
          { k: "expiry_date", label: "Valid until (leave empty if open-ended)", type: "date" } ] },
      ] },
    COLLECTION: { icon: "&#128230;", name: "Documentary Collection", short: "Get paid for your export",
      help: "You give us your shipping documents. We send them to the buyer's bank, which hands them to the buyer only after payment (or acceptance of a bill).",
      sections: [
        party("counterparty", "Who is the buyer?", "The foreign buyer who must pay."),
        { title: "Buyer's bank", help: "", fields: [
          { k: "counterparty_bank_bic", label: "Buyer's bank SWIFT code (BIC)", req: true, hint: "8 or 11 characters" } ] },
        money("Amount the buyer must pay."),
        { title: "How the buyer gets the documents", help: "", fields: [
          { k: "release_against", label: "Release documents against", type: "select", def: "PAYMENT", opts: [["PAYMENT", "Payment (D/P)"], ["ACCEPTANCE", "Acceptance of a bill - pay later (D/A)"]] },
          { k: "usance_days", label: "Days until payment (for acceptance)", type: "number", def: "0" },
          { k: "goods_description", label: "Description of goods", wide: true },
          { k: "charges_for", label: "Bank charges paid by", type: "select", def: "DRAWEE", opts: [["DRAWEE", "Buyer"], ["DRAWER", "Us (exporter)"]] },
          { k: "protest_if_unpaid", label: "Protest if unpaid", type: "check" } ] },
        { title: "Documents you hand over", help: "", fields: [ { k: "documents", type: "docs", wide: true, def: ["COMMERCIAL_INVOICE", "BILL_OF_LADING"] } ] },
      ] },
  };

  P.examples = function (product, today) {
    const d = (n) => P.addDays(today, n);
    return {
      LC: { "beneficiary.name": "Milano Meccanica S.p.A.", "beneficiary.address": "Via Roma 12, 16121 Genova", "beneficiary.country": "IT",
        advising_bank_bic: "BNCAITMMXXX", currency: "EUR", amount: "85000", goods_description: "2 CNC milling machines model MM-500 incl. tooling",
        incoterms: "FOB", incoterms_place: "Genoa", port_of_loading: "Genoa", port_of_discharge: "Hamburg", latest_shipment_date: d(30),
        expiry_date: d(60), available_by: "SIGHT_PAYMENT", presentation_period_days: "21", documents_required: ["COMMERCIAL_INVOICE", "BILL_OF_LADING", "PACKING_LIST"] },
      GUARANTEE: { "beneficiary.name": "Nordwind Energie GmbH", "beneficiary.address": "Am Hafen 4, 26382 Wilhelmshaven", "beneficiary.country": "DE",
        guarantee_type: "PERFORMANCE", currency: "EUR", amount: "45000", underlying_transaction: "Supply contract NW-2026-118 for turbine parts",
        underlying_contract_value: "450000", expiry_date: d(365) },
      SURETY: { "obligee.name": "Wohnbau Mitte GmbH", "obligee.address": "Hohenzollernring 50, 50672 Koeln", "obligee.country": "DE",
        bond_type: "CONTRACT_PERFORMANCE", currency: "EUR", amount: "38000", underlying_contract: "Construction contract WM-77 (VOB/B), residential building",
        contract_value: "380000", expiry_date: d(420) },
      COLLECTION: { "counterparty.name": "Anatolia Makina A.S.", "counterparty.address": "Organize Sanayi Bolgesi, Bursa", "counterparty.country": "TR",
        counterparty_bank_bic: "AKBKTRISXXX", currency: "EUR", amount: "52000", release_against: "PAYMENT", goods_description: "Spare parts for packaging machines",
        documents: ["COMMERCIAL_INVOICE", "BILL_OF_LADING", "CERTIFICATE_OF_ORIGIN"] },
    }[product];
  };

  function fieldHtml(f, currencies) {
    const id = "f_" + f.k.replace(/\W/g, "_");
    const req = f.req ? " <em>*</em>" : "";
    const hint = f.hint ? `<span class="hint">${P.esc(f.hint)}</span>` : "";
    const wide = f.wide ? " wide" : "";
    if (f.type === "check") return `<label class="check${wide}" data-k="${f.k}"><input type="checkbox" id="${id}" data-k="${f.k}" data-t="check"> ${P.esc(f.label)}</label>`;
    if (f.type === "docs") return `<div class="docs-pick${wide}" data-k="${f.k}" data-t="docs">${DOCS.map(([code, name, orig]) =>
      `<label class="doc"><input type="checkbox" value="${code}"${(f.def || []).includes(code) ? " checked" : ""}><span class="grow">${name}</span>
       <span class="tiny muted">originals</span><input type="number" min="1" max="9" value="${orig}" data-orig="${code}"></label>`).join("")}</div>`;
    let input;
    if (f.type === "select" || f.type === "currency") {
      const opts = f.type === "currency" ? (currencies || ["EUR"]).map((c) => [c, c]) : f.opts;
      input = `<select id="${id}" data-k="${f.k}">${f.req || f.def || f.type === "currency" ? "" : '<option value="">- choose -</option>'}${
        opts.map(([v, l]) => `<option value="${P.esc(v)}"${(f.def || (f.type === "currency" ? "EUR" : "")) === v ? " selected" : ""}>${P.esc(l)}</option>`).join("")}</select>`;
    } else if (f.type === "textarea" || f.type === "lines") {
      input = `<textarea id="${id}" data-k="${f.k}" data-t="${f.type}" ${f.type === "lines" ? 'placeholder="One condition per line"' : ""}></textarea>`;
    } else {
      input = `<input id="${id}" type="${f.type === "number" ? "text" : f.type || "text"}" ${f.type === "number" ? 'inputmode="decimal"' : ""} data-k="${f.k}" data-t="${f.type || "text"}" value="${P.esc(f.def || "")}">`;
    }
    return `<label class="f${wide}" data-k="${f.k}"><span>${P.esc(f.label)}${req}</span>${input}${hint}</label>`;
  }

  P.renderForm = function (el, product, currencies) {
    const form = P.FORMS[product];
    el.innerHTML = `<div class="stack">${form.sections.map((s) => `<div class="form-section"><h3>${P.esc(s.title)}</h3>
      <div class="help">${P.esc(s.help || "")}</div><div class="fields">${s.fields.map((f) => fieldHtml(f, currencies)).join("")}</div></div>`).join("")}</div>`;
    // a country select needs a placeholder when required
    el.querySelectorAll('select[data-k$=".country"]').forEach((s) => { s.insertAdjacentHTML("afterbegin", '<option value="" selected>- choose -</option>'); });
  };

  P.fillForm = function (el, values) {
    Object.entries(values || {}).forEach(([k, v]) => {
      const box = el.querySelector(`[data-t="docs"][data-k="${k}"]`);
      if (box) {
        const items = (v || []).map((x) => typeof x === "string" ? { doc_type: x } : x);
        box.querySelectorAll('input[type=checkbox]').forEach((c) => {
          const it = items.find((x) => x.doc_type === c.value); c.checked = !!it;
          if (it && it.originals) box.querySelector(`[data-orig="${c.value}"]`).value = it.originals;
        });
        return;
      }
      const inp = el.querySelector(`[data-k="${k}"]:is(input,select,textarea)`);
      if (!inp) return;
      if (inp.type === "checkbox") inp.checked = !!v; else inp.value = Array.isArray(v) ? v.join("\n") : v;
    });
  };

  P.flatten = function (obj, prefix, out) {  // {beneficiary: {name}} -> {"beneficiary.name": ...} for fillForm
    out = out || {};
    Object.entries(obj || {}).forEach(([k, v]) => {
      if (v && typeof v === "object" && !Array.isArray(v)) P.flatten(v, (prefix || "") + k + ".", out);
      else if (v !== null && v !== undefined) out[(prefix || "") + k] = v;
    });
    return out;
  };

  P.collectForm = function (el) {
    const out = {};
    const set = (path, v) => { const parts = path.split("."); let o = out; parts.slice(0, -1).forEach((p) => { o = o[p] = o[p] || {}; }); o[parts[parts.length - 1]] = v; };
    el.querySelectorAll("[data-k]:is(input,select,textarea)").forEach((inp) => {
      const k = inp.dataset.k, t = inp.dataset.t;
      if (inp.type === "checkbox") { set(k, inp.checked); return; }
      const v = inp.value.trim();
      if (v === "") return;
      if (t === "lines") { set(k, v.split("\n").map((x) => x.trim()).filter(Boolean)); return; }
      set(k, t === "number" ? v.replace(/[\s,]/g, "") : v);
    });
    el.querySelectorAll('[data-t="docs"]').forEach((box) => {
      const list = [];
      box.querySelectorAll('input[type=checkbox]').forEach((c) => {
        if (c.checked) list.push({ doc_type: c.value, originals: Number(box.querySelector(`[data-orig="${c.value}"]`).value || 1) });
      });
      set(box.dataset.k, list);
    });
    return out;
  };

  P.labelOf = function (product, key) {
    for (const s of P.FORMS[product].sections) for (const f of s.fields) if (f.k === key || key.startsWith(f.k + ".")) return f.label || s.title;
    return key;
  };

  P.showErrors = function (el, product, errors) {
    el.querySelectorAll(".f.error").forEach((x) => { x.classList.remove("error"); const e = x.querySelector(".err"); if (e) e.remove(); });
    const unplaced = [];
    (errors || []).forEach((e) => {
      const lab = el.querySelector(`label.f[data-k="${e.field}"]`) || el.querySelector(`label.f[data-k^="${e.field.split(".")[0]}."]`);
      if (lab && !lab.classList.contains("error")) { lab.classList.add("error"); lab.insertAdjacentHTML("beforeend", `<span class="err">${P.esc(e.message)}</span>`); }
      else if (!lab) unplaced.push(`${P.labelOf(product, e.field)}: ${e.message}`);
    });
    const first = el.querySelector(".f.error"); if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
    return unplaced;
  };

  // ------------------------------------------------------------------ file upload box
  P.uploader = function (el) {
    const files = [];
    el.innerHTML = `<div class="drop"><div style="font-size:26px">&#128206;</div><div class="bold">Drop files here or click to choose</div>
      <div class="tiny">PDF, scans, Word, Excel - for example pro-forma invoice, contract, purchase order (max 20 MB each)</div>
      <input type="file" multiple class="hidden"></div><div class="filelist"></div>`;
    const drop = el.querySelector(".drop"), input = el.querySelector("input"), list = el.querySelector(".filelist");
    const render = () => { list.innerHTML = files.map((f, i) => `<div class="fileitem"><span>&#128196; ${P.esc(f.filename)} <span class="muted tiny">${P.size(f.size)}</span></span>
      <button class="btn sec small" data-rm="${i}">Remove</button></div>`).join(""); };
    const add = (fl) => Array.from(fl).forEach((file) => {
      const r = new FileReader();
      r.onload = () => { files.push({ filename: file.name, size: file.size, content_base64: String(r.result).split(",")[1] || "" }); render(); };
      r.readAsDataURL(file);
    });
    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => { add(input.files); input.value = ""; });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); add(e.dataTransfer.files); });
    list.addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (b) { files.splice(Number(b.dataset.rm), 1); render(); } });
    return { files: () => files.map(({ filename, content_base64 }) => ({ filename, content_base64 })), clear: () => { files.length = 0; render(); } };
  };

  P.chooser = function (el, onPick) {
    el.innerHTML = `<div class="choose">${Object.entries(P.FORMS).map(([k, f]) => `<button class="opt" data-p="${k}">
      <div class="icon">${f.icon}</div><h3>${P.esc(f.name)}</h3><p><b>${P.esc(f.short)}.</b> ${P.esc(f.help)}</p></button>`).join("")}</div>`;
    el.addEventListener("click", (e) => { const b = e.target.closest("[data-p]"); if (!b) return;
      el.querySelectorAll(".opt").forEach((o) => o.classList.toggle("active", o === b)); onPick(b.dataset.p); });
  };
})();
