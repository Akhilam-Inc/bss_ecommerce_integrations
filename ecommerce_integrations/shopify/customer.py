from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cstr, validate_phone_number

from ecommerce_integrations.controllers.customer import EcommerceCustomer
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
)


class ShopifyCustomer(EcommerceCustomer):
	def __init__(self, customer_id: str):
		self.setting = frappe.get_doc(SETTING_DOCTYPE)
		super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

	def sync_customer(self, customer: Dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("email")

		customer_group = self.setting.customer_group
		super().sync_customer(customer_name, customer_group)

		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=customer.get("email")
			)
		if shipping_address:
			self.create_customer_address(
				customer_name, shipping_address, address_type="Shipping", email=customer.get("email")
			)

		self.create_customer_contact(customer)

	def create_customer_address(
		self,
		customer_name,
		shopify_address: Dict[str, Any],
		address_type: str = "Billing",
		email: Optional[str] = None,
	) -> Optional[str]:
		"""Create customer address(es) using Customer dict provided by shopify."""
		address_fields = _map_address_fields(shopify_address, customer_name, address_type, email)
		return super().create_customer_address(address_fields)

	def update_existing_addresses(self, customer):
		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		email = customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		shopify_address: Dict[str, Any],
		address_type: str = "Billing",
		email: Optional[str] = None,
	) -> Optional[str]:
		old_address = self.get_customer_address_doc(address_type)

		if not old_address:
			return self.create_customer_address(customer_name, shopify_address, address_type, email)

		new_values = _map_address_fields(shopify_address, customer_name, address_type, email)

		if _address_content_matches(old_address, new_values):
			exclude_in_update = ["address_title", "address_type"]
			old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
			old_address.flags.ignore_mandatory = True
			old_address.save()
			return old_address.name

		# Address content genuinely changed (customer shipped/billed to a
		# different place) — create a distinct Address instead of overwriting
		# the one that older Sales Orders/Delivery Notes/Shipments already
		# link to, which would silently change their address too.
		return self.create_customer_address(customer_name, shopify_address, address_type, email)

	def create_customer_contact(self, shopify_customer: Dict[str, Any]) -> None:

		if not (shopify_customer.get("first_name") and shopify_customer.get("email")):
			return

		contact_fields = {
			"status": "Passive",
			"first_name": shopify_customer.get("first_name"),
			"last_name": shopify_customer.get("last_name"),
			"unsubscribed": not shopify_customer.get("accepts_marketing"),
		}

		if shopify_customer.get("email"):
			contact_fields["email_ids"] = [{"email_id": shopify_customer.get("email"), "is_primary": True}]

		phone_no = shopify_customer.get("phone") or shopify_customer.get("default_address", {}).get(
			"phone"
		)

		if validate_phone_number(phone_no, throw=False):
			contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

		super().create_customer_contact(contact_fields)


COMPARE_ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "pincode")


def _address_content_matches(address, values: Dict[str, Any]) -> bool:
	return all(cstr(address.get(field)) == cstr(values.get(field)) for field in COMPARE_ADDRESS_FIELDS)


def get_matching_customer_address(
	customer_link_name: str, address_type: str, shopify_address: Optional[Dict[str, Any]]
) -> Optional[str]:
	"""Return the Address linked to this customer whose content matches shopify_address, if any."""
	if not shopify_address:
		return None

	values = _map_address_fields(shopify_address, customer_link_name, address_type, None)
	addresses = frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Customer"],
			["Dynamic Link", "link_name", "=", customer_link_name],
			["address_type", "=", address_type],
		],
		fields=["name", *COMPARE_ADDRESS_FIELDS],
	)

	for address in addresses:
		if _address_content_matches(address, values):
			return address.name

	return None


def _map_address_fields(shopify_address, customer_name, address_type, email):
	""" returns dict with shopify address fields mapped to equivalent ERPNext fields"""
	address_fields = {
		"address_title": customer_name,
		"address_type": address_type,
		ADDRESS_ID_FIELD: shopify_address.get("id"),
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	# ── Bombaysweets customization: capture the shipping-address recipient ──
	# The receiver (name + phone on the Shopify shipping address) is often not the
	# account customer. Store it on the Address so it flows to SO/DN/Shipment.
	recipient_name = shopify_address.get("name") or " ".join(
		p for p in [shopify_address.get("first_name"), shopify_address.get("last_name")] if p
	).strip()
	if recipient_name:
		address_fields["custom_recipient_name"] = recipient_name
	if phone:
		address_fields["custom_recipient_phone"] = phone

	return address_fields
