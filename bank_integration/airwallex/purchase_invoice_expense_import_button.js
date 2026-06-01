// Client Script
// DocType: Purchase Invoice
// Apply To: List
//
// Adds a prominent "Import Airwallex Expenses" primary button at the top of
// the Purchase Invoice list view, visible only to users with the Accounts
// Manager role. Replaces the older button that lived on the Bank Integration
// Setting form.
//
// Uses the `refresh` hook (not `onload`) because Frappe re-renders the
// listview page header on every refresh, which wipes out buttons added in
// `onload`. A class-based guard prevents the button being added twice.

frappe.listview_settings['Purchase Invoice'] = frappe.listview_settings['Purchase Invoice'] || {};

const _airwallex_prev_refresh = frappe.listview_settings['Purchase Invoice'].refresh;

frappe.listview_settings['Purchase Invoice'].refresh = function (listview) {
    if (_airwallex_prev_refresh) {
        try { _airwallex_prev_refresh(listview); } catch (_) { /* ignore */ }
    }

    // Role gate. Server side also enforces in
    // bank_integration.airwallex.expense_invoice.enqueue_expense_import.
    if (!frappe.user.has_role('Accounts Manager')) return;

    // Wire the completion listener exactly once per page session.
    if (!window._airwallex_expense_listener) {
        window._airwallex_expense_listener = true;
        frappe.realtime.off('expense_import_complete');
        frappe.realtime.on('expense_import_complete', function (data) {
            frappe.msgprint({
                title: __('Airwallex Expense Import'),
                indicator: data.status === 'success' ? 'green' : 'orange',
                message: data.message
            });
        });
    }

    // Skip if our button is already in the header from a prior refresh.
    if (listview.page.inner_toolbar
        && listview.page.inner_toolbar.find('.airwallex-import-btn').length) {
        return;
    }

    const $btn = listview.page.add_inner_button(__('Import Airwallex Expenses'), function () {
        const d = new frappe.ui.Dialog({
            title: __('Import Airwallex Expenses'),
            fields: [
                {
                    fieldname: 'from_date',
                    label: __('From (expense created)'),
                    fieldtype: 'Date',
                    reqd: 1,
                    default: frappe.datetime.add_days(frappe.datetime.get_today(), -30)
                },
                {
                    fieldname: 'to_date',
                    label: __('To (expense created)'),
                    fieldtype: 'Date',
                    reqd: 1,
                    default: frappe.datetime.get_today()
                },
                {
                    fieldtype: 'HTML',
                    options: '<p class="text-muted small">Imports expenses awaiting approval (the "pending" state in Airwallex) as draft Purchase Invoices. Safe to re-run: already imported expenses are skipped. Bank Transaction reference numbers in the same window are also backfilled.</p>'
                }
            ],
            primary_action_label: __('Start Import'),
            primary_action: function (values) {
                d.hide();
                frappe.call({
                    method: 'bank_integration.airwallex.expense_invoice.enqueue_expense_import',
                    args: { from_date: values.from_date, to_date: values.to_date },
                    callback: function (r) {
                        if (!r.exc) {
                            frappe.show_alert({
                                message: __('Import queued. You will be notified when it finishes.'),
                                indicator: 'blue'
                            });
                        }
                    }
                });
            }
        });
        d.show();
    });

    if ($btn && $btn.addClass) {
        $btn.addClass('btn-primary airwallex-import-btn');
    }
};
