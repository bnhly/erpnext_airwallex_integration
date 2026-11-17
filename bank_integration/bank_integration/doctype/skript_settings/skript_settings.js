// Skript Settings: buttons only. Status and failures are on the Bank Feeds page.
frappe.ui.form.on("Skript Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Bank Feeds"), () => frappe.set_route("bank-feeds"));
		frm.add_custom_button(__("Test connection"), () => {
			frm.call("test_connection").then((r) => {
				const l = ((r.message && r.message.logins) || [])[0] || {};
				frappe.msgprint({
					title: __("Test connection"),
					message: l.ok
						? __("Connected. {0} accounts.", [l.accounts]) +
						  (l.not_listed && l.not_listed.length
								? " " + __("Not listed here: {0}", [l.not_listed.map(frappe.utils.escape_html).join(", ")])
								: "")
						: frappe.utils.escape_html(l.message || __("Failed")),
				});
			});
		});
		frm.add_custom_button(__("Fetch accounts"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first."));
				return;
			}
			frm.call("fetch_accounts").then((r) => {
				frappe.show_alert({ message: __("{0} added, {1} updated", [r.message.added, r.message.updated]), indicator: "green" });
				frm.reload_doc();
			});
		});
	},
});
