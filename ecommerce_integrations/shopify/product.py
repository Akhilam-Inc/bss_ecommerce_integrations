from typing import Optional

import frappe
from frappe import _, msgprint
from frappe.utils import cint, cstr, get_datetime, now, today
from frappe.utils.nestedset import get_root_of
from shopify.resources import Product, Variant

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: Optional[str] = None,
		sku: Optional[str] = None,
		has_variants: Optional[int] = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME, integration_item_code=self.product_id, variant_id=self.variant_id, sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self, force_update: bool = False):
		shopify_product = Product.find(self.product_id)
		product_dict = shopify_product.to_dict()

		if not self.is_synced():
			try:
				self._make_item(product_dict)
				return
			except frappe.UniqueValidationError:
				frappe.clear_messages()

		# Item exists — update only when Shopify has newer data (or force_update requested)
		shopify_ts = _parse_shopify_datetime(product_dict.get("updated_at"))
		stored_ts = ecommerce_item.get_shopify_updated_at(MODULE_NAME, self.product_id)
		stored_dt = get_datetime(stored_ts) if stored_ts else None

		if force_update or not stored_dt or (shopify_ts and shopify_ts > stored_dt):
			self._update_item(product_dict)

	def _get_item_code(self, product_dict, variant=None):
		"""Compute ERPNext item_code based on the item_sync_by setting.

		When variant is provided we are coding a variant child item.
		When variant is None we are coding a template (has_variants=1) or a
		single-variant product that has no meaningful variants.
		"""
		sync_by = getattr(self.setting, "item_sync_by", None) or "Shopify Product ID"

		if variant:
			# ── Variant items ────────────────────────────────────────────────
			if sync_by == "SKU":
				return cstr(variant.get("sku")) or cstr(variant.get("id"))
			elif sync_by == "Shopify Product Handle":
				handle = product_dict.get("handle", "")
				variant_title = variant.get("title", "")
				return f"{handle}-{variant_title}" if variant_title and variant_title != "Default Title" else handle
			elif sync_by == "Shopify Product Title":
				title = product_dict.get("title", "").strip()
				variant_title = variant.get("title", "")
				return f"{title}-{variant_title}" if variant_title and variant_title != "Default Title" else title
			else:  # Shopify Product ID (default)
				return cstr(variant.get("id"))
		else:
			# ── Template / single-variant items ──────────────────────────────
			if sync_by == "SKU":
				# Template items have no single SKU — use the separate
				# "Main Item Code Based On" setting to avoid colliding with a
				# variant's SKU-derived item_code.
				main_by = (
					getattr(self.setting, "main_item_code_based_on", None)
					or "Shopify Product ID"
				)
				if main_by == "Shopify Product Title":
					return cstr(product_dict.get("title", "")).strip() or cstr(product_dict.get("id"))
				elif main_by == "Shopify Product Handle":
					return cstr(product_dict.get("handle")) or cstr(product_dict.get("id"))
				else:  # "Shopify Product ID" (default / safe fallback)
					return cstr(product_dict.get("id"))
			elif sync_by == "Shopify Product Handle":
				return cstr(product_dict.get("handle")) or cstr(product_dict.get("id"))
			elif sync_by == "Shopify Product Title":
				return cstr(product_dict.get("title", "")).strip() or cstr(product_dict.get("id"))
			else:  # Shopify Product ID (default)
				return cstr(product_dict.get("id"))

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.setting.warehouse
		shopify_updated_at = _parse_shopify_datetime(product_dict.get("updated_at"))

		if _has_variants(product_dict):
			self.has_variants = 1
			attributes = self._create_attribute(product_dict)
			product_dict["item_code"] = self._get_item_code(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes, shopify_updated_at=shopify_updated_at)
			self._create_item_variants(product_dict, warehouse, attributes, shopify_updated_at=shopify_updated_at)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			product_dict["item_code"] = self._get_item_code(product_dict, variant=product_dict["variants"][0])
			self._create_item(product_dict, warehouse, shopify_updated_at=shopify_updated_at)

	def _update_item(self, product_dict):
		"""Update existing ERPNext item fields from Shopify product data."""
		integration_item_code = str(product_dict["id"])
		_add_weight_details(product_dict)

		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=integration_item_code, has_variants=1
		)
		if not template_item:
			template_item = ecommerce_item.get_erpnext_item(
				MODULE_NAME, integration_item_code=integration_item_code
			)

		shopify_status = product_dict.get("status", "").lower()

		if template_item:
			template_item.flags.from_integration = True

			new_name = product_dict.get("title", "").strip()
			if new_name:
				template_item.item_name = new_name

			template_item.description = (
				product_dict.get("body_html") or product_dict.get("title") or template_item.description
			)
			template_item.item_group = self._get_item_group(product_dict.get("product_type"))

			image = _get_item_image(product_dict)
			if image:
				template_item.image = image

			if product_dict.get("weight") is not None:
				template_item.weight_per_unit = product_dict["weight"]
			if product_dict.get("weight_unit"):
				template_item.weight_uom = WEIGHT_TO_ERPNEXT_UOM_MAP.get(
					product_dict["weight_unit"], template_item.weight_uom
				)

			if not _has_variants(product_dict) and product_dict.get("variants"):
				price = product_dict["variants"][0].get("price")
				if price is not None:
					template_item.set(ITEM_SELLING_RATE_FIELD, price)

			if shopify_status == "active":
				template_item.disabled = 0
			elif shopify_status in ("draft", "unlisted", "archived"):
				template_item.disabled = 1

			template_item.save(ignore_permissions=True)

		if _has_variants(product_dict):
			warehouse = self.setting.warehouse
			attributes = []
			for opt in product_dict.get("options", []):
				if frappe.db.get_value("Item Attribute", opt.get("name")):
					attributes.append({"attribute": opt.get("name")})

			for variant in product_dict.get("variants", []):
				variant_id = str(variant["id"])
				variant_ecom = frappe.db.get_value(
					"Ecommerce Item",
					{
						"integration": MODULE_NAME,
						"integration_item_code": integration_item_code,
						"variant_id": variant_id,
					},
					["name", "erpnext_item_code"],
					as_dict=True,
				)

				if variant_ecom:
					variant_item = frappe.get_doc("Item", variant_ecom.erpnext_item_code)
					variant_item.flags.from_integration = True

					title = product_dict.get("title", "").strip()
					variant_title = variant.get("title", "")
					if title:
						variant_item.item_name = f"{title}-{variant_title}"

					if variant.get("weight") is not None:
						variant_item.weight_per_unit = variant["weight"]
					if variant.get("weight_unit"):
						variant_item.weight_uom = WEIGHT_TO_ERPNEXT_UOM_MAP.get(
							variant["weight_unit"], variant_item.weight_uom
						)

					if variant.get("price") is not None:
						variant_item.set(ITEM_SELLING_RATE_FIELD, variant["price"])

					if shopify_status == "active":
						variant_item.disabled = 0
					elif shopify_status in ("draft", "unlisted", "archived"):
						variant_item.disabled = 1

					variant_item.save(ignore_permissions=True)
				else:
					# New variant added in Shopify since last sync — create it
					if template_item and attributes:
						shopify_updated_at = _parse_shopify_datetime(product_dict.get("updated_at"))
						shopify_variant_dict = {
							"id": product_dict["id"],
							"variant_id": variant["id"],
							"item_code": self._get_item_code(product_dict, variant=variant),
							"title": f"{product_dict.get('title', '').strip()}-{variant.get('title', '')}",
							"product_type": product_dict.get("product_type"),
							"sku": variant.get("sku"),
							"uom": template_item.stock_uom or _("Nos"),
							"item_price": variant.get("price"),
							"weight_unit": variant.get("weight_unit"),
							"weight": variant.get("weight"),
						}
						variant_attributes = list(attributes)
						for i, attr_key in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
							if variant.get(attr_key) and i < len(variant_attributes):
								variant_attributes[i] = dict(variant_attributes[i])
								variant_attributes[i]["attribute_value"] = self._get_attribute_value(
									variant.get(attr_key), variant_attributes[i]
								)
						self._create_item(
							shopify_variant_dict,
							warehouse,
							0,
							variant_attributes,
							template_item.name,
							shopify_updated_at=shopify_updated_at,
						)

		shopify_updated_at = _parse_shopify_datetime(product_dict.get("updated_at"))
		if shopify_updated_at:
			frappe.db.sql(
				"""UPDATE `tabEcommerce Item`
				   SET shopify_updated_at = %s, item_synced_on = %s
				   WHERE integration = %s AND integration_item_code = %s""",
				(shopify_updated_at, now(), MODULE_NAME, integration_item_code),
			)

	def _create_attribute(self, product_dict):
		attribute = []
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value} for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())
				for d in item_attr.item_attribute_values
			):
				item_attr.append("item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value})

	def _create_item(
		self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None,
		shopify_updated_at=None,
	):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]
		item_code = item_dict["item_code"]

		if not _match_sku_and_link_item(
			item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant,
			shopify_updated_at=shopify_updated_at,
		):
			# If an ERPNext item with the computed item_code already exists (e.g. when
			# item_sync_by = "SKU" and the SKU-named item was created previously, or for
			# variant items whose SKU / handle / title matches an existing item_code),
			# just create the Ecommerce Item link instead of attempting a duplicate insert.
			existing_item = frappe.db.get_value("Item", {"item_code": item_code})
			if existing_item:
				_link_existing_item(
					existing_item,
					integration_item_code,
					variant_id,
					sku,
					variant_of,
					has_variant,
					shopify_updated_at,
				)
			else:
				ecommerce_item.create_ecommerce_item(
					MODULE_NAME,
					integration_item_code,
					item_dict,
					variant_id=variant_id,
					sku=sku,
					variant_of=variant_of,
					has_variants=has_variant,
					shopify_updated_at=shopify_updated_at,
				)

	def _create_item_variants(self, product_dict, warehouse, attributes, shopify_updated_at=None):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": self._get_item_code(product_dict, variant=variant),
					"title": product_dict.get("title", "").strip() + "-" + variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{"attribute_value": self._get_attribute_value(variant.get(variant_attr), attributes[i])}
						)
				self._create_item(shopify_item_variant, warehouse, 0, attributes, template_item.name,
								  shopify_updated_at=shopify_updated_at)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _parse_shopify_datetime(dt_str):
	"""Parse Shopify ISO-8601 datetime string to a naive datetime for DB comparison."""
	if not dt_str:
		return None
	try:
		dt = get_datetime(dt_str)
		if dt and dt.tzinfo is not None:
			dt = dt.replace(tzinfo=None)
		return dt
	except Exception:
		return None


def _link_existing_item(
	item_name, integration_item_code, variant_id, sku, variant_of, has_variants, shopify_updated_at
):
	"""Create an Ecommerce Item link for an ERPNext item that already exists.

	Called when the computed item_code (SKU / handle / title) matches an existing ERPNext
	Item so we must not try to recreate it — only register the integration link.
	"""
	# Skip silently if already linked (idempotent)
	if frappe.db.exists(
		"Ecommerce Item",
		{
			"integration": MODULE_NAME,
			"integration_item_code": cstr(integration_item_code),
			"variant_id": cstr(variant_id),
		},
	):
		return

	try:
		ecom_item = frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": MODULE_NAME,
				"erpnext_item_code": item_name,
				"integration_item_code": cstr(integration_item_code),
				"has_variants": has_variants,
				"variant_id": cstr(variant_id),
				"variant_of": cstr(variant_of) if variant_of else "",
				"sku": cstr(sku) if not has_variants else None,
				"item_synced_on": now(),
				"shopify_updated_at": shopify_updated_at,
			}
		)
		ecom_item.insert()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Shopify: Failed to link existing item")


def _match_sku_and_link_item(
	item_dict, product_id, variant_id, variant_of=None, has_variant=False,
	shopify_updated_at=None,
) -> bool:
	"""Tries to match new item with existing item using Shopify SKU == item_code.

	Returns true if matched and linked.
	"""
	sku = item_dict["sku"]
	if not sku or variant_of or has_variant:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecom_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
					"shopify_updated_at": shopify_updated_at,
				}
			)

			ecom_item.insert()
			return True
		except Exception:
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):

		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id."""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
	"""This hook is called when inserting new or updating existing `Item`.

	New items are pushed to shopify and changes to existing items are
	updated depending on what is configured in "Shopify Setting" doctype.
	"""
	template_item = item = doc  # alias for readability
	# a new item recieved from ecommerce_integrations is being inserted
	if item.flags.from_integration:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	if frappe.flags.in_import:
		return

	if item.has_variants:
		return

	if len(item.attributes) > 3:
		msgprint(_("Template items/Items with 4 or more attributes can not be uploaded to Shopify."))
		return

	if doc.variant_of and not setting.upload_variants_as_items:
		msgprint(_("Enable variant sync in setting to upload item to Shopify."))
		return

	if item.variant_of:
		template_item = frappe.get_doc("Item", item.variant_of)

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	is_new_product = not bool(product_id)

	if is_new_product:
		product = Product()
		product.published = False
		product.status = "active" if setting.sync_new_item_as_active else "draft"

		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
		is_successful = product.save()

		if is_successful:
			update_default_variant_properties(
				product,
				sku=template_item.item_code,
				price=template_item.get(ITEM_SELLING_RATE_FIELD),
				is_stock_item=template_item.is_stock_item,
			)
			if item.variant_of:
				product.options = []
				product.variants = []
				variant_attributes = {
					"title": template_item.item_name,
					"sku": item.item_code,
					"price": item.get(ITEM_SELLING_RATE_FIELD),
				}
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(_("Shopify Error: Missing value for attribute {}").format(attr.attribute))
				product.variants.append(Variant(variant_attributes))

			product.save()  # push variant

			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": str(product.id),
						"variant_id": "" if d.has_variants else str(product.variants[0].id),
						"sku": "" if d.has_variants else str(product.variants[0].sku),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

		write_upload_log(status=is_successful, product=product, item=item)
	elif setting.update_shopify_item_on_update:
		product = Product.find(product_id)
		if product:
			map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
			if not item.variant_of:
				update_default_variant_properties(
					product, is_stock_item=template_item.is_stock_item, price=item.get(ITEM_SELLING_RATE_FIELD)
				)
			else:
				variant_attributes = {"sku": item.item_code, "price": item.get(ITEM_SELLING_RATE_FIELD)}
				product.options = []
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(_("Shopify Error: Missing value for attribute {}").format(attr.attribute))
				product.variants.append(Variant(variant_attributes))

			is_successful = product.save()
			if is_successful and item.variant_of:
				map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

			write_upload_log(status=is_successful, product=product, item=item, action="Updated")


def map_erpnext_variant_to_shopify_variant(
	shopify_product: Product, erpnext_item, variant_attributes
):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.variants:
			if (
				variant.option1 == variant_attributes.get("option1")
				and variant.option2 == variant_attributes.get("option2")
				and variant.option3 == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.id)
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.id),
							"variant_id": variant_product_id,
							"sku": str(variant.sku),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(shopify_product: Product, erpnext_item):
	"""Map erpnext fields to shopify, called both when updating and creating new products."""

	shopify_product.title = erpnext_item.item_name
	shopify_product.body_html = erpnext_item.description
	shopify_product.product_type = erpnext_item.item_group

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		# reverse lookup for key
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		shopify_product.weight = erpnext_item.weight_per_unit
		shopify_product.weight_unit = uom

	if erpnext_item.disabled:
		shopify_product.status = "draft"
		shopify_product.published = False
		msgprint(_("Status of linked Shopify product is changed to Draft."))


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	shopify_product: Product,
	is_stock_item: bool,
	sku: Optional[str] = None,
	price: Optional[float] = None,
):
	"""Shopify creates default variant upon saving the product.

	Some item properties are supposed to be updated on the default variant.
	Input: saved shopify_product, sku and price
	"""
	default_variant: Variant = shopify_product.variants[0]

	# this will create Inventory item and qty will be updated by scheduled job.
	if is_stock_item:
		default_variant.inventory_management = "shopify"

	if price is not None:
		default_variant.price = price
	if sku is not None:
		default_variant.sku = sku


def write_upload_log(status: bool, product: Product, item, action="Created") -> None:
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		msg += _("Shopify reported errors:") + " " + ", ".join(product.errors.full_messages())
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error", request_data=product.to_dict(), message=msg, method="upload_erpnext_item",
		)
	else:
		create_shopify_log(
			status="Success",
			request_data=product.to_dict(),
			message=f"{action} Item: {item.name}, shopify product: {product.id}",
			method="upload_erpnext_item",
		)


@frappe.whitelist()
def update_product_erpnext(payload, request_id=None):
	"""Handle products/update webhook — full field sync with timestamp guard."""
	if not payload:
		return

	product_id = str(payload.get("id", ""))
	if not product_id:
		return

	if not ecommerce_item.is_synced(MODULE_NAME, product_id):
		return

	shopify_ts = _parse_shopify_datetime(payload.get("updated_at"))
	stored_ts = ecommerce_item.get_shopify_updated_at(MODULE_NAME, product_id)
	stored_dt = get_datetime(stored_ts) if stored_ts else None

	if stored_dt and shopify_ts and shopify_ts <= stored_dt:
		return  # Nothing changed

	try:
		product = ShopifyProduct(product_id)
		product._update_item(payload)
	except Exception:
		frappe.log_error(
			title="Shopify Product Update Error",
			message=frappe.get_traceback(),
		)
