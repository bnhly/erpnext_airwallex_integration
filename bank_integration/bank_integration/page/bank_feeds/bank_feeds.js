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

	if (!document.getElementById("bkf-css-1")) {
		$("head").append(`<style id="bkf-css-1">
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
					<span>${__("Last run")}</span><span>${
						rep.ended ? `${when(rep.ended)}: ${rep.created || 0} ${__("added")}, <span class="${rep.failed ? "bkf-bad" : ""}">${rep.failed || 0} ${__("failed")}</span>` : "-"}</span>
				</div>
				${probs ? `<div class="bkf-prob" role="note">${probs}</div>` : ""}
				<div class="bkf-acts">
					<button class="btn btn-xs btn-default" data-act="test" data-p="${p.key}">${__("Test connection")}</button>
					<button class="btn btn-xs btn-default" data-act="sync" data-p="${p.key}" ${p.enabled ? "" : "disabled"}>${__("Sync now")}</button>
					<button class="btn btn-xs btn-default" data-act="catch" data-p="${p.key}">${__("Catch up")}</button>
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
			catchUp(p);
		}
	});

	function catchUp(provider) {
		const awx = provider === "airwallex";
		let checked = null;
		const d = new frappe.ui.Dialog({
			title: __("Catch up {0}", [awx ? "Airwallex" : "Skript"]),
			fields: [
				{ fieldname: "from_date", fieldtype: "Date", label: __("From"), reqd: 1 },
				{ fieldname: "to_date", fieldtype: "Date", label: __("To"), reqd: 1, default: frappe.datetime.get_today() },
				...(awx ? [{ fieldname: "currencies", fieldtype: "Data", label: __("Currencies"), default: "AUD, USD" }] : []),
				{ fieldname: "out", fieldtype: "HTML" },
			],
			primary_action_label: __("Check"),
			primary_action(v) {
				const args = { provider, from_date: v.from_date, to_date: v.to_date, currencies: v.currencies || "" };
				if (!checked) {
					frappe.call({ method: "bank_integration.feed.tools.catch_up", args: { ...args, dry_run: 1 },
						freeze: true, freeze_message: __("Reading the bank") }).then((r) => {
						checked = r.message;
						const list = (checked.would_add || []).slice(0, 200).map((x) =>
							`<tr><td>${esc(String(x.when || "").slice(0, 10))}</td><td>${esc(x.bank_account || "")}</td>
							 <td class="n">${esc(String(x.amount ?? ""))}</td><td>${esc(x.description || "")}</td></tr>`).join("");
						const unr = Object.entries(checked.unrouted || {}).map(([c, n]) => `${esc(c)} ${n}`).join(", ");
						d.fields_dict.out.$wrapper.html(`
							<p><b>${checked.count}</b> ${__("lines to add")}${unr ? ` &middot; ${__("not set up")}: ${unr}` : ""}${
								checked.by_hand_count ? ` &middot; <span title="${esc((checked.by_hand || []).slice(0, 50).map((x) => String(x.when || "").slice(0, 10) + " " + (x.amount ?? "") + " = " + x.hand_line).join("\n"))}">${checked.by_hand_count} ${__("already loaded by hand, left out")}</span>` : ""}</p>
							${(checked.problems || []).map((x) => `<div class="bkf-prob">${esc(x)}</div>`).join("")}
							${list ? `<div style="max-height:320px;overflow:auto"><table class="bkf-tbl">${list}</table></div>` : ""}`);
						if (checked.count) d.set_primary_action(__("Add {0} lines", [checked.count]), () => add(args));
						else d.set_primary_action(__("Close"), () => d.hide());
					});
				}
			},
		});
		const add = (args) =>
			frappe.call("bank_integration.feed.tools.catch_up", { ...args, dry_run: 0, confirm: "YES_ADD" }).then(() => {
				d.hide();
				frappe.show_alert({ message: __("Adding in the background"), indicator: "blue" });
				setTimeout(load, 5000);
			});
		// A changed date or currency needs a fresh check.
		d.$wrapper.on("change", "input", () => {
			if (!checked) return;
			checked = null;
			d.fields_dict.out.$wrapper.empty();
			d.set_primary_action(__("Check"), () => d.primary_action(d.get_values()));
		});
		d.show();
	}

	load();
};
