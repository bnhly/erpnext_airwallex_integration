// Airwallex Settings: buttons only. Status and failures are on the Bank Feeds page.
frappe.ui.form.on("Airwallex Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Bank Feeds"), () => frappe.set_route("bank-feeds"));
		frm.add_custom_button(__("Test connection"), () => {
			frm.call("test_connection").then((r) => bank_feed_show_test(r.message));
		});
	},
});

window.bank_feed_show_test = function (res) {
	const rows = ((res && res.logins) || []).map((l) => {
		const words = l.ok
			? __("Connected") + (l.routes ? ": " + Object.keys(l.routes).join(", ") : "")
			: frappe.utils.escape_html(l.message || __("Failed"));
		return `<tr><td>${frappe.utils.escape_html(l.login)}</td><td style="color:${l.ok ? "var(--green-600,#16a34a)" : "var(--red-600,#dc2626)"}">${words}</td></tr>`;
	});
	const probs = ((res && res.problems) || []).map((p) => `<li>${frappe.utils.escape_html(p)}</li>`).join("");
	frappe.msgprint({
		title: __("Test connection"),
		message: `<table class="table table-sm">${rows.join("")}</table>${probs ? "<ul>" + probs + "</ul>" : ""}`,
	});
};
