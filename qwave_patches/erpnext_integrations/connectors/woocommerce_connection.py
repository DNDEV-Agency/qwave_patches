import json

import frappe
from frappe import _
from frappe.utils import cstr
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from erpnext.erpnext_integrations.connectors.woocommerce_connection import (
	link_items,
	verify_request,
	create_contact,
	create_address,
	rename_address,
	add_tax_details,
)

@frappe.whitelist(allow_guest=True)
def order(*args, **kwargs):
	try:
		_order(*args, **kwargs)
	except Exception:
		error_message = (
			frappe.get_traceback() + "\n\n Request Data: \n" + json.loads(frappe.request.data).__str__()
		)
		frappe.log_error("WooCommerce Error", error_message)
		raise


def _order(*args, **kwargs):
	woocommerce_settings = frappe.get_doc("Woocommerce Settings")
    
	if frappe.flags.woocomm_test_order_data:
		order = frappe.flags.woocomm_test_order_data
		event = "created"
		
	elif frappe.request and frappe.request.data:
		print("verify_request")
		print("event: ", frappe.get_request_header("X-Wc-Webhook-Event"))
		verify_request()
		print("verified_request")
		try:
			order = json.loads(frappe.request.data)
		except ValueError:
			# woocommerce returns 'webhook_id=value' for the first request which is not JSON
			order = frappe.request.data
		event = frappe.get_request_header("X-Wc-Webhook-Event")

	else:
		return "success"

	if event == "created" or event == "updated":
		sys_lang = frappe.get_single("System Settings").language or "en"
		link_customer_and_address(order)
		customer_name = get_customer_name(order)
		link_items(order.get("line_items"), woocommerce_settings, sys_lang)
		so = create_sales_order(order, woocommerce_settings, customer_name, sys_lang)
		create_payment_entry(order, woocommerce_settings, customer_name, so)
		si = make_sales_invoice(so.name)
		si.allocate_advances_automatically = True
		si = si.insert(ignore_permissions=True)
		si.submit()

def get_customer_name(payload):
	customer_woo_com_id = payload.get("customer_id")
	customer_woo_com_email = payload.get("billing").get("email")
	if customer_woo_com_id:
		customer_exists = frappe.get_value("Customer", {"woocommerce_id": customer_woo_com_id})
		if customer_exists:
			return frappe.get_value("Customer", {"woocommerce_id": customer_woo_com_id}, "name")
	else:
		customer_exists = frappe.get_value("Customer", {"woocommerce_email": customer_woo_com_email})
		if customer_exists:
			return frappe.get_value("Customer", {"woocommerce_email": customer_woo_com_email}, "name")
	return None

def link_customer_and_address(payload):
	raw_billing_data = payload.get("billing")
	raw_shipping_data = payload.get("shipping")
	customer_name = raw_billing_data.get("first_name") + " " + raw_billing_data.get("last_name")
	customer_woo_com_id = payload.get("customer_id")
	customer_woo_com_email = raw_billing_data.get("email")
	
	if customer_woo_com_id:
		customer_exists = frappe.get_value("Customer", {"woocommerce_id": customer_woo_com_id})
		if customer_exists:
			customer = frappe.get_doc("Customer", {"woocommerce_id": customer_woo_com_id})
			old_name = customer.customer_name
	else:
		customer_exists = frappe.get_value("Customer", {"woocommerce_email": customer_woo_com_email})
		if customer_exists:
			customer = frappe.get_doc("Customer", {"woocommerce_email": customer_woo_com_email})
			old_name = customer.customer_name	

	if not customer_exists:
		# Create Customer
		customer = frappe.new_doc("Customer")

	customer.customer_name = customer_name
	customer.woocommerce_email = customer_woo_com_email
	if customer_woo_com_id:
		customer.woocommerce_id = customer_woo_com_id
		customer.custom_qwave_id = customer_woo_com_id
	customer.flags.ignore_mandatory = True
	customer.save()

	if customer_exists:
		if old_name != customer_name:
			frappe.rename_doc("Customer", old_name, customer_name)
		for address_type in (
			"Billing",
			"Shipping",
		):
			try:
				address = frappe.get_doc(
					"Address", {"woocommerce_email": customer_woo_com_email, "address_type": address_type}
				)
				rename_address(address, customer)
			except (
				frappe.DoesNotExistError,
				frappe.DuplicateEntryError,
				frappe.ValidationError,
			):
				pass
	else:
		create_address(raw_billing_data, customer, "Billing")
		create_address(raw_shipping_data, customer, "Shipping")
		create_contact(raw_billing_data, customer)

def create_sales_order(order, woocommerce_settings, customer_name, sys_lang):
	acceptable_status_list = ["partial-payment", "completed"]
	if not (order.get("transaction_id") or order.get("status") in acceptable_status_list):
		return frappe.throw("Order not paid yet")
	
	new_sales_order = frappe.new_doc("Sales Order")
	new_sales_order.customer = customer_name
	new_sales_order.po_no = new_sales_order.woocommerce_id = order.get("id")
	new_sales_order.naming_series = woocommerce_settings.sales_order_series or "SO-WOO-"
	new_sales_order.company = woocommerce_settings.company
	new_sales_order.transaction_date = order.get("date_created").split("T")[0]
	new_sales_order.delivery_date = frappe.utils.add_days(order.get("date_created").split("T")[0], woocommerce_settings.delivery_after_days or 7)
	
	set_items_in_sales_order(new_sales_order, woocommerce_settings, order, sys_lang)
	new_sales_order.flags.ignore_mandatory = True
	new_sales_order.insert()
	new_sales_order.submit()

	frappe.db.commit()
	return new_sales_order

def set_items_in_sales_order(new_sales_order, woocommerce_settings, order, sys_lang):
	company_abbr = frappe.db.get_value("Company", woocommerce_settings.company, "abbr")

	default_warehouse = _("Stores - {0}", sys_lang).format(company_abbr)
	if not frappe.db.exists("Warehouse", default_warehouse) and not woocommerce_settings.warehouse:
		frappe.throw(_("Please set Warehouse in Woocommerce Settings"))
	
	for item in order.get("line_items"):
		woocomm_item_id = item.get("product_id")
		found_item = frappe.get_doc("Item", {"woocommerce_id": cstr(woocomm_item_id)})

		ordered_items_tax = item.get("total_tax")

		new_sales_order.append(
			"items",
			{
				"item_code": found_item.name,
				"item_name": found_item.item_name,
				"description": found_item.item_name,
				"uom": woocommerce_settings.uom or _("Nos", sys_lang),
				"qty": item.get("quantity"),
				"rate": int(item["product_data"]["price"]),
				"warehouse": woocommerce_settings.warehouse or default_warehouse,
			},
		)

		add_tax_details(
			new_sales_order, ordered_items_tax, "Ordered Item tax", woocommerce_settings.tax_account
		)

	# shipping_details = order.get("shipping_lines") # used for detailed order

	add_tax_details(
		new_sales_order, order.get("shipping_tax"), "Shipping Tax", woocommerce_settings.f_n_f_account
	)
	add_tax_details(
		new_sales_order,
		order.get("shipping_total"),
		"Shipping Total",
		woocommerce_settings.f_n_f_account,
	)

def create_payment_entry(order, woocommerce_settings, customer_name, ref):
	new_payment_entry = frappe.new_doc("Payment Entry")
	new_payment_entry.posting_date = order.get("date_created").split("T")[0]
	new_payment_entry.payment_type = "Receive"
	new_payment_entry.mode_of_payment = "Credit Card"
	new_payment_entry.paid_to = frappe.db.get_value("Company", woocommerce_settings.company, "default_income_account")
	new_payment_entry.reference_no = order.get("transaction_id")
	new_payment_entry.party_type = "Customer"
	new_payment_entry.party = customer_name
	new_payment_entry.paid_amount = order.get("total")
	new_payment_entry.received_amount = order.get("total")
	new_payment_entry.company = woocommerce_settings.company
	new_payment_entry.cost_center = frappe.db.get_value("Company", woocommerce_settings.company, "cost_center")
	payment_reference = {
		"allocated_amount": float(order.get("total")),
		"due_date": order.get("date_created").split("T")[0],
		"reference_doctype": "Sales Order",
		"reference_name": ref.name,
	}
	new_payment_entry.append("references", payment_reference)
	new_payment_entry.save()
	new_payment_entry.submit()
	frappe.db.commit()