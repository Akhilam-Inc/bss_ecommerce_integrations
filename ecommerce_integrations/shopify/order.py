import json
from datetime import datetime
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate,add_days,nowtime
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.customer import ShopifyCustomer, get_matching_customer_address
from ecommerce_integrations.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
		return
	try:
		# ==============================================================================
		# NEW: VALIDATE SHIPPING TYPE MASTER (EXISTS & ACTIVE)
		# ==============================================================================
		shipping_lines = order.get("shipping_lines") or []
		if shipping_lines:
			# Shopify uses 'code', fallback to 'title' just in case
			shipping_code = shipping_lines[0].get("title") or shipping_lines[0].get("code")
			
			if shipping_code:
				# Fetch the active status. Returns None if missing, 0 if inactive, 1 if active.
				is_active = frappe.db.get_value("Shipping Type", shipping_code, "active")
				
				if is_active is None:
					frappe.throw(
						f"Sync halted: Missing Shipping Type '{shipping_code}'. "
						f"Please add this exactly as written to the Shipping Type master, then click Retry on this log."
					)
				elif not cint(is_active):
					frappe.throw(
						f"Sync halted: Shipping Type '{shipping_code}' is currently marked as Inactive. "
						f"Please check the 'Active' box in the Shipping Type master, then click Retry on this log."
					)
		# ==============================================================================
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		create_items_if_not_exist(order)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		create_order(order, setting)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)


def create_sales_order(shopify_order, setting, company=None, dry_run=False):
	customer = setting.default_customer
	shipping_address_name = None
	billing_address_name = None
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")
			# Resolve the exact Address matching THIS order's own address content,
			# instead of relying on "whatever the customer's default address is
			# right now" — a customer can have multiple saved addresses over time.
			shipping_address_name = get_matching_customer_address(
				customer, "Shipping", shopify_order.get("shipping_address")
			)
			billing_address_name = get_matching_customer_address(
				customer,
				"Billing",
				shopify_order.get("billing_address") or shopify_order.get("customer", {}).get("default_address"),
			)

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True)

			return ""

		taxes = get_order_taxes(shopify_order, setting, items)
		delivery_date = get_future_delivery_date(shopify_order)

		# Update delivery_date in all items (blank unless Future Date Delivery supplied
		# one — bombaysweets_customization's after_insert logic computes it otherwise)
		for d in items:
			d["delivery_date"] = delivery_date

		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"naming_series": setting.sales_order_series or "SO-Shopify-",
				ORDER_ID_FIELD: str(shopify_order.get("id")),
				ORDER_NUMBER_FIELD: shopify_order.get("name"),
				"customer": customer,
				"shipping_address_name": shipping_address_name,
				"customer_address": billing_address_name,
				"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
				"delivery_date": delivery_date,  # ✅ FIX: Add delivery_date here
				"custom_expected_dispatch_date": delivery_date,
				"custom_payment_status": shopify_order.get("financial_status"),
				"custom_shopify_order_creation_date": getdate(shopify_order.get("created_at")) or getdate(nowdate()),
				"custom_order_source": "Shopify",
				"custom_priority": "Medium",
				"custom_shopify_order_creation_time": (
					get_datetime(shopify_order.get("created_at")).time()
					if shopify_order.get("created_at")
					else nowtime()
				),
				"set_warehouse": setting.warehouse,
				"custom_shopify_order_shipping_type": (shopify_order.get("shipping_lines") or [{}])[0].get("title") or "",
				"custom_order_notes": shopify_order.get("note"),
				"company": setting.company,
				"selling_price_list": get_dummy_price_list(),
				"ignore_pricing_rule": 1,
				"items": items,
				"taxes": taxes,
				"tax_category": get_dummy_tax_category(),
			}
		)

		if company:
			so.update({"company": company, "status": "Draft"})

		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)

		if dry_run:
			return so

		so.save(ignore_permissions=True)
		# Skip the update-after-submit guard on this initial creation submit.
		# save() stores derived fields (total_net_weight, item discount_amount, ...)
		# rounded to DB precision, then submit() recomputes them as raw floats;
		# the guard's raw `!=` rejects mathematically-equal values that differ only
		# in float representation (e.g. 2.514 vs 2.5140000000000002), rolling back
		# the whole order. Nothing is meaningfully "changed after submit" here.
		so.flags.ignore_validate_update_after_submit = True
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def get_future_delivery_date(shopify_order):
    """
    Returns the customer-selected delivery date from the "Delivery-Date" note
    attribute when the order's shipping line is "Future Date Delivery" (Zapiet-style
    future-dated orders); otherwise None so bombaysweets_customization's normal
    zone-based calculation fills in delivery_date / custom_expected_dispatch_date.
    """
    shipping_type = (shopify_order.get("shipping_lines") or [{}])[0].get("title") or ""
    if shipping_type != "Future Date Delivery":
        return None

    for attr in shopify_order.get("note_attributes") or []:
        if attr.get("name") == "Delivery-Date" and attr.get("value"):
            try:
                return datetime.strptime(attr["value"], "%Y/%m/%d").date()
            except ValueError:
                frappe.throw(
                    f"Sync halted: could not parse Delivery-Date note attribute value "
                    f"'{attr.get('value')}' (expected format YYYY/MM/DD)."
                )

    frappe.throw(
        "Sync halted: shipping line is 'Future Date Delivery' but no 'Delivery-Date' "
        "note attribute was found on the order."
    )

def get_shopify_item_extra_fields(line_item):

	qty = cint(line_item.get("quantity")) or 1
	unit_price = flt(line_item.get("price") or 0)

	discount_allocations = line_item.get("discount_allocations") or []
	item_discount = flt(discount_allocations[0].get("amount")) if discount_allocations else 0

	custom_shopify_base_rate = unit_price

	custom_shopify_rate = flt(unit_price - (item_discount / qty), 2)

	return {
		"custom_shopify_base_rate": custom_shopify_base_rate,
		"custom_applied_discount_from_shopify": item_discount,
		"custom_shopify_rate": custom_shopify_rate,
	}

def get_order_items(order_items, setting, delivery_date, taxes_inclusive):
	items = []
	all_product_exists = True
	product_not_exists = []

	# ✅ FIX: First loop through all items to check existence
	for shopify_item in order_items:
		if not shopify_item.get("product_exists"):
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), ORDER_ID_FIELD: shopify_item.get("id")}
			)

	# ✅ FIX: Then build items only if all products exist
	if all_product_exists:
		for shopify_item in order_items:
			item_code = get_item_code(shopify_item)

			extra_fields = get_shopify_item_extra_fields(shopify_item)
			items.append(
				{
					"item_code": item_code,
					"item_name": shopify_item.get("name"),
					"rate": _get_item_price(shopify_item, taxes_inclusive),
					"delivery_date": delivery_date,
					"qty": shopify_item.get("quantity"),
					"stock_uom": shopify_item.get("uom") or "Nos",
					"warehouse": setting.warehouse,
					ORDER_ITEM_DISCOUNT_FIELD: flt(
						_get_total_discount(shopify_item) / cint(shopify_item.get("quantity")), 2
					),
					"custom_applied_discount_from_shopify": extra_fields["custom_applied_discount_from_shopify"],
    				"custom_shopify_rate": extra_fields["custom_shopify_rate"],
					"custom_shopify_base_rate": extra_fields["custom_shopify_base_rate"],
				}
			)

	return items


def _get_item_price(line_item, taxes_inclusive: bool) -> float:

	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return flt(price - (total_discount / qty), 2)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return flt(price - (total_taxes + total_discount) / qty, 2)


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)



def get_order_taxes(shopify_order, setting, items):
	line_items = shopify_order.get("line_items")
	taxes_inclusive = shopify_order.get("taxes_included")

	# {account_head: {meta, item_taxes: {item_code: {tax, taxable}}}}
	# taxable_value mirrors what ERPNext will compute for row.taxable_value (rate × qty).
	# We store it per item so we can compute an effective rate = tax/taxable×100, which
	# satisfies india_compliance's check: taxable_value × rate/100 == tax_amount exactly.
	account_tax_map = {}
	for line_item in line_items:
		item_code = get_item_code(line_item)
		taxable_value = _get_item_price(line_item, taxes_inclusive) * cint(line_item.get("quantity"))

		# Track which accounts have already counted taxable_value for this line_item,
		# because multiple tax_lines can hit the same account (e.g. 2.5% + 9% both → CGST).
		# We must add taxable_value only once per account per line_item.
		accounts_seen = set()

		for tax in line_item.get("tax_lines"):
			account_head = get_tax_account_head(tax, charge_type="sales_tax")
			amount = flt(tax.get("price"))

			if account_head not in account_tax_map:
				account_tax_map[account_head] = {
					"charge_type": "Actual",
					"account_head": account_head,
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": 0,
					"included_in_print_rate": 0,
					"cost_center": setting.cost_center,
					"item_taxes": {},
					"dont_recompute_tax": 1,
				}

			account_tax_map[account_head]["tax_amount"] = flt(account_tax_map[account_head]["tax_amount"] + amount, 2)
			item_taxes = account_tax_map[account_head]["item_taxes"]

			if item_code in item_taxes:
				item_taxes[item_code]["tax"] = flt(item_taxes[item_code]["tax"] + amount, 2)
				if account_head not in accounts_seen:
					item_taxes[item_code]["taxable"] = flt(item_taxes[item_code]["taxable"] + taxable_value, 2)
			else:
				item_taxes[item_code] = {"tax": flt(amount, 2), "taxable": flt(taxable_value, 2)}

			accounts_seen.add(account_head)

	# Build item_wise_tax_detail with effective rate = tax/taxable×100 so that
	# taxable_value × rate/100 == tax_amount, satisfying india_compliance validation.
	taxes = []
	for data in account_tax_map.values():
		item_wise_tax_detail = {}
		for item_code, values in data["item_taxes"].items():
			tax = flt(values["tax"], 2)
			taxable = flt(values["taxable"], 2)
			effective_rate = round(tax / taxable * 100, 2) if taxable else 0
			item_wise_tax_detail[item_code] = [effective_rate, tax]

		taxes.append({
			"charge_type": data["charge_type"],
			"account_head": data["account_head"],
			"description": data["description"],
			"tax_amount": flt(data["tax_amount"], 2),
			"included_in_print_rate": data["included_in_print_rate"],
			"cost_center": data["cost_center"],
			"item_wise_tax_detail": item_wise_tax_detail,
			"dont_recompute_tax": data["dont_recompute_tax"],
		})

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	# {account_head: {meta, item_accumulator: {item_code: {tax, taxable}}}}
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_accumulator": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] = flt(
			tax_account_wise_data[account_head]["tax_amount"] + flt(tax.get("tax_amount")), 2
		)
		if tax.get("item_wise_tax_detail"):
			acc = tax_account_wise_data[account_head]["item_accumulator"]
			for item_code, (rate, amount) in tax["item_wise_tax_detail"].items():
				if item_code in acc:
					acc[item_code]["tax"] = flt(acc[item_code]["tax"] + amount, 2)
				else:
					taxable = flt((amount / rate * 100) if rate else 0, 2)
					acc[item_code] = {"tax": flt(amount, 2), "taxable": taxable}

	result = []
	for data in tax_account_wise_data.values():
		item_wise_tax_detail = {}
		for item_code, values in data["item_accumulator"].items():
			tax = flt(values["tax"], 2)
			taxable = flt(values["taxable"], 2)
			effective_rate = round(tax / taxable * 100, 2) if taxable else 0
			item_wise_tax_detail[item_code] = [effective_rate, tax]

		result.append({
			"charge_type": data["charge_type"],
			"account_head": data["account_head"],
			"description": data["description"],
			"cost_center": data["cost_center"],
			"included_in_print_rate": data["included_in_print_rate"],
			"dont_recompute_tax": data["dont_recompute_tax"],
			"tax_amount": flt(data["tax_amount"], 2),
			"item_wise_tax_detail": item_wise_tax_detail,
		})

	return result


def get_tax_account_head(tax, charge_type: Optional[Literal["shipping", "sales_tax"]] = None):
	tax_title = str(tax.get("title"))

	tax_account = frappe.db.get_value(
		"Shopify Tax Account", {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title}, "tax_account",
	)

	if not tax_account and charge_type:
		tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax):
	tax_title = tax.get("title")

	tax_description = frappe.db.get_value(
		"Shopify Tax Account", {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title}, "tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(shipping_charge, charge_type="shipping"),
						"description": get_tax_account_description(shipping_charge) or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": setting.cost_center,
					}
				)

		for tax in shipping_charge.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax) or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax["price"],
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = order["id"]
		order_status = order["financial_status"]

		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist")
			return

		sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
		delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

		if sales_invoice:
			frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
			sales_order.cancel()
		else:
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


@temp_shopify_session
def sync_old_orders():
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()
