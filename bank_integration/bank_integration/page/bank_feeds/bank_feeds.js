// Bank Feeds: one page for Airwallex and Skript. 17 Nov 26.
// Per provider: on / off, schedule, status, last good run, last run, problems,
// and Test connection / Sync now / Catch up. Per Bank Account: last line,
// lines in 30 days, weekdays with no line, drafts, failures with the reason.

frappe.pages["bank-feeds"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Bank Feeds"), single_column: true });
	const api = "bank_integration.feed.page.";
	const esc = frappe.utils.escape_html;
	const $root = $('<div class="bkf"></div>').appendTo(page.main);
	let data = null;

	$("#bkf-css-1").remove();
	if (!document.getElementById("bkf-css-2")) {
		$("head").append(`<style id="bkf-css-2">
.bkf{padding:4px 0 24px}
.bkf-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px;margin-bottom:16px}
.bkf-card{background:var(--fg-color,#fff);border:1px solid var(--border-color,#e3e8eb);border-radius:8px;padding:12px 14px}
.bkf-card h4{margin:0 0 8px;font-size:15px;display:flex;align-items:center;gap:8px;color:var(--text-color,#1b2631)}
.bkf-kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px;color:var(--text-color,#1b2631)}
.bkf-kv span:nth-child(odd){color:var(--text-muted,#8a969e)}
.bkf-acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.bkf-prob{margin:8px 0 0;padding:6px 10px;border-radius:6px;background:#fef3c7;color:#78350f;font-size:12px}
.bkf-tbl{width:100%;font-size:13px;border-collapse:separate;border-spacing:0;background:var(--fg-color,#fff);border:1px solid var(--border-color,#e3e8eb);border-radius:8px}
.bkf-tbl th{color:var(--text-muted,#8a969e);font-weight:500;text-align:left;padding:7px 10px;border-bottom:1px solid var(--border-color,#e3e8eb)}
.bkf-tbl td{padding:7px 10px;border-bottom:1px solid var(--border-color,#e3e8eb);color:var(--text-color,#1b2631);vertical-align:top}
.bkf-tbl td.n{text-align:right}
.bkf-open{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:4px;background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd;vertical-align:-3px;margin-right:6px}
.bkf-open:hover{background:#bfdbfe;color:#1e40af;border-color:#60a5fa}
.bkf-open svg{width:12px;height:12px}
.bkf-bad{color:#b91c1c;font-weight:600}
.bkf-warn{color:#b45309}
.bkf-fails{margin:6px 0 0;padding-left:16px;font-size:12px}
.bkf-pill{font-size:11px;padding:1px 8px;border-radius:10px;font-weight:500}
.bkf-on{background:#dcfce7;color:#166534}.bkf-off{background:#f1f5f9;color:#475569}
.bkf-note{font-size:12px;color:var(--text-muted,#8a969e);margin-top:10px}
.bkf-v{font-size:11px;padding:1px 7px;border-radius:9px;white-space:nowrap}
.bkf-new{background:#dcfce7;color:#166534}.bkf-unsure{background:#fef3c7;color:#78350f}.bkf-there{background:#e2e8f0;color:#334155}
.bkf-sum{margin:4px 0 8px;font-size:13px;color:var(--text-color,#1b2631)}
.bkf-months{width:auto;margin-bottom:8px}.bkf-toggle{font-size:12px;color:var(--text-muted,#8a969e);margin:4px 0}
[data-theme="dark"] .bkf-tbl,[data-theme="dark"] .bkf-card{border-color:var(--border-color,#333)}
</style>`);
	}

	const OPEN_SVG =
		'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';
	const chip = (route, label) =>
		`<a class="bkf-open" href="/app/${route}" target="_blank" rel="noopener" aria-label="${esc(__("Open {0}", [label]))}">${OPEN_SVG}</a>`;
	const when = (v) => (v ? esc(frappe.datetime.prettyDate(v)) : "-");
	const day = (v) => (v ? esc(frappe.datetime.str_to_user(v)) : "-");

	page.set_secondary_action(__("Refresh"), load);

	function load() {
		$root.html(`<div class="text-muted">${__("Loading")}</div>`);
		frappe.call(api + "overview").then((r) => {
			data = r.message;
			render();
		});
	}

	function render() {
		const cards = data.providers.map((p) => {
			const rep = p.report || {};
			const bad = p.status === "Completed with Errors" || p.status === "Failed";
			const probs = (rep.problems || []).map((x) => `<div>${esc(x)}</div>`).join("");
			return `<section class="bkf-card" aria-label="${esc(p.label)}">
				<h4>${chip(frappe.router.slug(p.doctype), p.doctype)}${esc(p.label)}
					<span class="bkf-pill ${p.enabled ? "bkf-on" : "bkf-off"}">${p.enabled ? __("On") : __("Off")}</span></h4>
				<div class="bkf-kv">
					<span>${__("Runs")}</span><span>${esc(p.schedule || "-")}</span>
					<span>${__("Status")}</span><span class="${bad ? "bkf-bad" : ""}">${esc(p.status || "-")}${
						p.status === "In Progress" ? " (" + when(p.started) + ")" : ""}</span>
					<span>${__("Last good run")}</span><span>${when(p.last_good)}</span>
					<span>${__("Reconciled up to")}</span><span>${day(p.floor)}</span>
					<span>${__("Last run")}</span><span>${
						rep.ended ? `${when(rep.ended)}: ${rep.created || 0} ${__("added")}, <span class="${rep.failed ? "bkf-bad" : ""}">${rep.failed || 0} ${__("failed")}</span>` : "-"}</span>
				</div>
				${probs ? `<div class="bkf-prob" role="note">${probs}</div>` : ""}
				<div class="bkf-acts">
					<button class="btn btn-xs btn-default" data-act="test" data-p="${p.key}">${__("Test connection")}</button>
					<button class="btn btn-xs btn-default" data-act="sync" data-p="${p.key}" ${p.enabled ? "" : "disabled"}>${__("Sync now")}</button>
					<button class="btn btn-xs btn-default" data-act="catch" data-p="${p.key}" data-floor="${esc(p.floor || "")}">${__("Catch up")}</button>
				</div>
			</section>`;
		});

		const rows = data.accounts.map((a, i) => {
			const fails = a.failed || [];
			const gaps = a.gaps || [];
			return `<tr>
				<td>${chip("bank-account/" + encodeURIComponent(a.bank_account), a.bank_account)}${esc(a.bank_account)}</td>
				<td>${esc(a.provider === "airwallex" ? "Airwallex" : "Skript")}<div class="text-muted small">${esc(a.source || "")}</div></td>
				<td>${esc(a.currency || "-")}</td>
				<td>${day(a.last_date)}<div class="text-muted small">${a.last_made ? __("made") + " " + when(a.last_made) : ""}</div></td>
				<td class="n">${a.recent}</td>
				<td class="n ${gaps.length > 5 ? "bkf-warn" : ""}" title="${esc(gaps.join(", "))}">${gaps.length}</td>
				<td class="n ${a.drafts ? "bkf-warn" : ""}">${a.drafts}</td>
				<td>${fails.length
					? `<button class="btn btn-xs btn-link bkf-bad" data-act="fails" data-i="${i}" aria-expanded="false">${fails.length} ${__("failed")}</button>
					   <ul class="bkf-fails" data-fails="${i}" hidden>${fails.map((f) =>
							`<li>${esc(String(f.when || "").slice(0, 10))} ${esc(String(f.amount ?? ""))} ${esc(f.description || "")}: ${esc(f.reason || "")}</li>`).join("")}</ul>`
					: "-"}</td>
			</tr>`;
		});

		const noacc = (data.no_account || []).map((x) => `${esc(x.currency || "?")} ${x.n}`).join(", ");
		$root.html(`
			<div class="bkf-cards">${cards.join("")}</div>
			<table class="bkf-tbl" aria-label="${__("Bank accounts")}">
				<thead><tr><th>${__("Bank Account")}</th><th>${__("Feed")}</th><th>${__("Currency")}</th><th>${__("Last line")}</th>
				<th class="n">${__("Lines, {0} days", [data.days])}</th><th class="n">${__("Weekdays with none")}</th>
				<th class="n">${__("Drafts")}</th><th>${__("Last run failures")}</th></tr></thead>
				<tbody>${rows.join("") || `<tr><td colspan="8" class="text-muted">${__("No bank accounts set up")}</td></tr>`}</tbody>
			</table>
			<div class="bkf-note">${noacc ? `<span class="bkf-warn">${__("Lines with no Bank Account")}: ${noacc}</span> &middot; ` : ""}
				<a href="/app/bank-integration-log">${__("Log rows")}: ${data.logs}</a></div>`);
	}

	$root.on("click", "button[data-act]", function () {
		const act = this.dataset.act;
		const p = this.dataset.p;
		if (act === "fails") {
			const $ul = $root.find(`[data-fails="${this.dataset.i}"]`);
			const open = $ul.prop("hidden");
			$ul.prop("hidden", !open);
			this.setAttribute("aria-expanded", open ? "true" : "false");
		} else if (act === "test") {
			frappe.call({ method: api + "test_connection", args: { provider: p }, freeze: true }).then((r) => {
				const res = r.message || {};
				const lines = (res.logins || []).map((l) => {
					const ok = l.ok
						? __("Connected") + (l.routes ? ": " + esc(Object.keys(l.routes).join(", ")) : "") +
						  (l.accounts != null ? ". " + __("{0} accounts", [l.accounts]) : "") +
						  (l.not_listed && l.not_listed.length ? ". " + __("Not set up: {0}", [esc(l.not_listed.join(", "))]) : "")
						: `<span class="bkf-bad">${esc(l.message || __("Failed"))}</span>`;
					return `<tr><td>${esc(l.login)}</td><td>${ok}</td></tr>`;
				});
				const probs = (res.problems || []).map((x) => `<div class="bkf-prob">${esc(x)}</div>`).join("");
				frappe.msgprint({ title: __("Test connection"), message: `<table class="table table-sm">${lines.join("")}</table>${probs}` });
			});
		} else if (act === "sync") {
			frappe.call(api + "sync_now", { provider: p }).then(() => {
				frappe.show_alert({ message: __("Started"), indicator: "blue" });
				setTimeout(load, 4000);
			});
		} else if (act === "catch") {
			catchUp(p, this.dataset.floor);
		}
	});

	function catchUp(provider, floor) {
		const awx = provider === "airwallex";
		const tool = "bank_integration.feed.tools.catch_up";
		const VCLS = { NEW: "bkf-new", UNSURE: "bkf-unsure", "ALREADY THERE": "bkf-there" };
		let res = null;
		let showThere = false;
		const pick = { add: {}, unsure: {}, adopt: {} };
		const d = new frappe.ui.Dialog({
			title: __("Catch up {0}", [awx ? "Airwallex" : "Skript"]),
			size: "extra-large",
			fields: [
				{ fieldname: "from_date", fieldtype: "Date", label: __("From"), reqd: 1,
				  default: floor ? frappe.datetime.add_days(floor, 1) : undefined },
				{ fieldname: "c1", fieldtype: "Column Break" },
				{ fieldname: "to_date", fieldtype: "Date", label: __("To"), reqd: 1, default: frappe.datetime.get_today() },
				...(awx ? [{ fieldname: "c2", fieldtype: "Column Break" },
					{ fieldname: "currencies", fieldtype: "Data", label: __("Currencies"), default: "AUD, USD" }] : []),
				{ fieldname: "s1", fieldtype: "Section Break" },
				{ fieldname: "out", fieldtype: "HTML" },
			],
			primary_action_label: __("Check"),
			primary_action() { check(); },
		});
		const args = () => {
			const v = d.get_values(true) || {};
			return { provider, from_date: v.from_date, to_date: v.to_date, currencies: v.currencies || "" };
		};
		function check() {
			const a = args();
			if (!a.from_date || !a.to_date) return;
			frappe.call({ method: tool, args: { ...a, dry_run: 1 }, freeze: true, freeze_message: __("Reading the bank") })
				.then((r) => {
					res = r.message;
					pick.add = {}; pick.unsure = {}; pick.adopt = {};
					(res.lines || []).forEach((x) => { if (x.verdict === "NEW") pick.add[x.id] = 1; });
					paint();
				});
		}
		function counts() {
			return { add: Object.keys(pick.add).length + Object.keys(pick.unsure).length, adopt: Object.keys(pick.adopt).length };
		}
		function setButton() {
			const c = counts();
			if (!res) return d.set_primary_action(__("Check"), check);
			if (!c.add && !c.adopt) return d.set_primary_action(__("Close"), () => d.hide());
			const words = [c.add ? __("Add {0}", [c.add]) : "", c.adopt ? __("adopt {0}", [c.adopt]) : ""].filter(Boolean).join(", ");
			d.set_primary_action(words, apply);
		}
		function paint() {
			const c = res.counts || {};
			const months = (res.months || []).map((m) => `<tr><td>${esc(m.month)}</td><td class="n">${m.by_id}</td>
				<td class="n">${m["ALREADY THERE"]}</td><td class="n">${m.UNSURE}</td><td class="n"><b>${m.NEW}</b></td></tr>`).join("");
			const extra = [
				Object.keys(res.unrouted || {}).length ? __("Not set up") + ": " + Object.entries(res.unrouted).map(([k, n]) => `${esc(k)} ${n}`).join(", ") : "",
				Object.keys(res.unmapped || {}).length ? __("No Bank Account") + ": " + Object.entries(res.unmapped).map(([k, n]) => `${esc(k)} ${n}`).join(", ") : "",
			].filter(Boolean).join(" &middot; ");
			const rows = (res.lines || []).filter((x) => showThere || x.verdict !== "ALREADY THERE").map((x) => {
				let tick = "";
				if (x.verdict === "NEW") tick = `<input type="checkbox" data-k="add" data-id="${esc(x.id)}" ${pick.add[x.id] ? "checked" : ""} aria-label="${__("Add")}">`;
				else if (x.verdict === "UNSURE") tick = `<input type="checkbox" data-k="unsure" data-id="${esc(x.id)}" ${pick.unsure[x.id] ? "checked" : ""} aria-label="${__("Add anyway")}">`;
				else if (x.adopt) tick = `<input type="checkbox" data-k="adopt" data-id="${esc(x.id)}" data-line="${esc(x.line)}" ${pick.adopt[x.id] ? "checked" : ""} aria-label="${__("Adopt")}">`;
				const li = x.line_info || {};
				const there = x.line
					? `${chip("bank-transaction/" + encodeURIComponent(x.line), x.line)}${esc(x.line)} <span class="text-muted">${esc(li.date || "")} ${esc(li.bank_account || __("no Bank Account"))}${li.matched ? " &middot; " + __("matched") : ""}</span>${
						x.candidates && x.candidates.length > 1 ? `<div class="text-muted small">${__("also")}: ${x.candidates.slice(1).map(esc).join(", ")}</div>` : ""}${
						x.verdict === "ALREADY THERE" && !x.adopt && x.why ? `<div class="text-muted small">${esc(x.why)}</div>` : ""}`
					: "";
				return `<tr><td>${tick}</td><td>${esc(x.date || "")}</td><td>${esc(x.bank_account || "")}</td>
					<td class="n">${esc(String(x.amount ?? ""))}</td><td>${esc(x.description || "")}</td>
					<td><span class="bkf-v ${VCLS[x.verdict] || ""}">${esc(__(x.verdict))}</span></td><td>${there}</td></tr>`;
			}).join("");
			d.fields_dict.out.$wrapper.html(`
				<div class="bkf-sum"><b>${c.NEW || 0}</b> ${__("new")} &middot; <b>${c.UNSURE || 0}</b> ${__("unsure")} &middot;
					<b>${c["ALREADY THERE"] || 0}</b> ${__("already there")} (${res.adoptable || 0} ${__("can be adopted")})${extra ? " &middot; " + extra : ""}</div>
				${(res.problems || []).map((x) => `<div class="bkf-prob">${esc(x)}</div>`).join("")}
				<table class="bkf-tbl bkf-months" aria-label="${__("By month")}"><thead><tr><th>${__("Month")}</th>
					<th class="n">${__("In by id")}</th><th class="n">${__("Already there")}</th><th class="n">${__("Unsure")}</th><th class="n">${__("New")}</th></tr></thead>
					<tbody>${months || `<tr><td colspan="5" class="text-muted">${__("Nothing in this range")}</td></tr>`}</tbody></table>
				<label class="bkf-toggle"><input type="checkbox" data-k="showthere" ${showThere ? "checked" : ""}> ${__("Show lines already there")}</label>
				<div style="max-height:46vh;overflow:auto"><table class="bkf-tbl" aria-label="${__("Lines")}"><thead><tr>
					<th>${__("Add / adopt")}</th><th>${__("Date")}</th><th>${__("Bank Account")}</th><th class="n">${__("Amount")}</th><th>${__("Description")}</th>
					<th>${__("Verdict")}</th><th>${__("Line in ERPNext")}</th></tr></thead><tbody>${rows}</tbody></table></div>`);
			setButton();
		}
		d.fields_dict.out.$wrapper.on("change", "input[type=checkbox]", function () {
			const k = this.dataset.k;
			if (k === "showthere") { showThere = this.checked; return paint(); }
			const id = this.dataset.id;
			if (this.checked) pick[k][id] = k === "adopt" ? this.dataset.line : 1;
			else delete pick[k][id];
			setButton();
		});
		function apply() {
			const c = counts();
			frappe.confirm(
				[c.add ? __("Add {0} lines to ERPNext.", [c.add]) : "", c.adopt ? __("Write the bank's id onto {0} existing lines (nothing else on them changes).", [c.adopt]) : ""].join(" "),
				() => frappe.call(tool, {
					...args(), dry_run: 0, confirm: "YES_ADD",
					add: JSON.stringify(Object.keys(pick.add)),
					add_unsure: JSON.stringify(Object.keys(pick.unsure)),
					adopt: JSON.stringify(pick.adopt),
				}).then(() => {
					d.hide();
					frappe.show_alert({ message: __("Working in the background"), indicator: "blue" });
					setTimeout(load, 6000);
				})
			);
		}
		// A changed date or currency needs a fresh check.
		d.$wrapper.on("change", ".frappe-control input", () => {
			if (!res) return;
			res = null;
			d.fields_dict.out.$wrapper.empty();
			setButton();
		});
		d.show();
	}

	load();
};
