
import base64
import json
import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .inxpress_client import InXpressClient

_logger = logging.getLogger(__name__)

# Statuses a shipment does not move on from, so polling can stop
INXPRESS_TRACKING_TERMINAL = {
    "DELIVERED", "VOIDED", "VOID", "CANCELLED", "EXPIRED",
}


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("inxpress", "InXpress")],
        ondelete={"inxpress": "set default"},
    )

    # ---- InXpress configuration fields ----
    inxpress_base_url = fields.Char(
        string="InXpress API URL",
        help="Base URL of the InXpress server (e.g. https://api.webship-x.com)",
    )
    inxpress_api_token = fields.Char(
        string="API Token",
        help="InXpress API token (ixpx_...). Leave empty to use username/password.",
    )
    inxpress_username = fields.Char(
        string="Username",
        help="InXpress login username. Used when API Token is empty.",
    )
    inxpress_password = fields.Char(
        string="Password",
        help="InXpress login password. Used when API Token is empty.",
    )
    inxpress_carrier_code = fields.Char(
        string="Carrier Code",
        help="Filter quotes to this carrier (e.g. UPS, FEDEX, DHL). "
             "Leave empty to return all carriers.",
    )
    inxpress_service_code = fields.Char(
        string="Service Code",
        help="Optionally restrict to a specific service code.",
    )
    inxpress_default_package_type_id = fields.Many2one(
        "stock.package.type",
        string="Default Package Type",
        help="Box used to quote an order that names no package type of its "
             "own. Its length, width and height are read from the package "
             "type record, so the quote measures the same box the dispatch "
             "will.",
    )
    inxpress_shipping_mode = fields.Selection(
        [("parcel", "Parcel"), ("freight", "Freight")],
        string="Shipping Mode",
        default="parcel",
        required=True,
        help="Parcel rates small packages. Freight rates LTL: the quote and "
             "the dispatch are sent with mode FREIGHT, which the InXpress "
             "token must carry the freight:quote and freight:write scopes "
             "for.",
    )
    inxpress_freight_class = fields.Char(
        string="Default Freight Class",
        help="NMFC-derived freight class sent as packageDetails.classCode "
             "(e.g. 50, 92.5). Freight mode only. Used only for orders whose "
             "products declare no class of their own; a product's own Freight "
             "Class always wins.",
    )
    inxpress_handling_unit_type = fields.Char(
        string="Handling Unit Type",
        default="Pallet",
        help="Handling unit sent as packageDetails.packageType (e.g. Pallet, "
             "Crate, Skid). Freight mode only.",
    )
    inxpress_freight_package_type_id = fields.Many2one(
        "stock.package.type",
        string="Freight Handling Unit",
        help="Package type a freight quote is measured on, e.g. a pallet. "
             "Freight declares a handling unit rather than a carton, so "
             "measuring the parcel box would rate a pallet at carton size. "
             "Falls back to the Default Package Type when empty.",
    )
    inxpress_freight_residential = fields.Boolean(
        string="Residential Delivery",
        help="Adds the RESIDENTIAL accessorial to freight quotes and "
             "dispatches.",
    )
    inxpress_freight_liftgate_delivery = fields.Boolean(
        string="Liftgate Delivery",
        help="Adds the HYDRAUL accessorial to freight quotes and dispatches.",
    )
    inxpress_freight_liftgate_pickup = fields.Boolean(
        string="Liftgate Pickup",
        help="Adds the TAILPICK accessorial to freight quotes and dispatches.",
    )
    inxpress_duties_paid_by = fields.Selection(
        [("recipient", "Recipient"), ("sender", "Sender")],
        string="Duties Paid By",
        default="recipient",
        required=True,
        help="Who pays duties and taxes on an international shipment whose "
             "order and company set no Incoterm: Recipient sends DAP, Sender "
             "sends DDP. An Incoterm on the order, or the company default, "
             "always wins.",
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _inxpress_opaque_error(self, exc, action, record=None):
        """Turn an unexpected failure into a message that says nothing.

        Anything the carrier, the network or a bug raises can carry a URL, a
        payload, a stack frame or a token fragment in its text, and that text
        lands in front of whoever clicked the button. The user is told to call
        InXpress and nothing else; the real cause goes to the log under a
        reference, so support can find it without the screen ever holding it.

        Deliberate UserErrors are not routed here. Those are written for the
        user and tell them what to fix.
        """
        reference = uuid.uuid4().hex[:8].upper()
        _logger.exception(
            "InXpress %s failed [ref %s] for %s: %s",
            action, reference, record.name if record else "-", exc,
        )
        return UserError(_(
            "We were unable to process your shipment. Please review your shipment "
            "details or contact your InXpress Representative for assistance."
            "\n\nReference: %s",
            reference,
        ))

    def _inxpress_client(self):
        """Return an authenticated InXpressClient instance."""
        self.ensure_one()
        if not self.inxpress_base_url:
            raise UserError(_(
                "Please configure the InXpress API URL on carrier '%s'.",
                self.name,
            ))
        if self.inxpress_api_token:
            return InXpressClient(self.inxpress_base_url, api_token=self.inxpress_api_token)
        if self.inxpress_username and self.inxpress_password:
            return InXpressClient(
                self.inxpress_base_url,
                username=self.inxpress_username,
                password=self.inxpress_password,
            )
        raise UserError(_(
            "Please configure either an API Token or Username/Password on carrier '%s'.",
            self.name,
        ))

    def _inxpress_address_payload(self, partner, prefix):
        """Convert a res.partner into a InXpress AddressDetail dict."""
        return {
            f"{prefix}City": partner.city or "",
            f"{prefix}State": partner.state_id.code or "",
            f"{prefix}AddressCode": partner.zip or "",
            f"{prefix}Country": partner.country_id.code or "",
            f"{prefix}Name": partner.commercial_company_name or partner.name or "",
            f"{prefix}ContactName": partner.name or "",
            f"{prefix}Email": partner.email or "",
            f"{prefix}Phone": partner.phone or partner.mobile or "",
            f"{prefix}Address1": partner.street or "",
            f"{prefix}Address2": partner.street2 or "",
        }

    def _inxpress_odoo_weight_unit(self):
        """Unit Odoo itself stores product weights in ("kg" or "lb")."""
        name = self.env["product.template"]._get_weight_uom_name_from_ir_config_parameter()
        return "lb" if name and name.lower().startswith("lb") else "kg"

    def _inxpress_weight_value(self, weight, unit):
        """Convert an Odoo weight into the unit selected on the order."""
        if unit == self._inxpress_odoo_weight_unit():
            return round(weight, 3)
        if unit == "lb":
            return round(weight * 2.20462, 3)
        return round(weight / 2.20462, 3)

    def _inxpress_odoo_length_unit(self):
        """Unit Odoo itself stores package type dimensions in.

        stock.package.type length/width/height follow the product length UoM,
        which is millimeters unless the cubic-feet setting is on.
        """
        name = self.env["product.template"]._get_length_uom_name_from_ir_config_parameter()
        return "ft" if name and name.lower().startswith(("ft", "foot", "feet")) else "mm"

    def _inxpress_length_value(self, value, target):
        """Convert a package type dimension into cm or inches."""
        factors = {
            ("mm", "cm"): 0.1,
            ("mm", "in"): 1 / 25.4,
            ("ft", "cm"): 30.48,
            ("ft", "in"): 12.0,
        }
        return round(value * factors[(self._inxpress_odoo_length_unit(), target)], 2)

    def _inxpress_volume_side(self, volume, target):
        """Side of the cube that holds `volume`, expressed in cm or inches.

        Odoo stores product volume in cubic meters, or cubic feet when the
        cubic-feet setting is on, so the cube root comes out in meters or feet
        and is converted from there.
        """
        name = self.env["product.template"]._get_volume_uom_name_from_ir_config_parameter()
        source = "ft" if name and "ft" in name.lower() else "m"
        factors = {
            ("m", "cm"): 100.0,
            ("m", "in"): 39.3701,
            ("ft", "cm"): 30.48,
            ("ft", "in"): 12.0,
        }
        return round(volume ** (1.0 / 3.0) * factors[(source, target)], 2)

    def _inxpress_dimensions_payload(self, order, weight_unit):
        """One parcel's dimensions, from the box this carrier declares.

        Metric weight ships with cm, imperial with inches, so InXpress never
        receives a mixed system.

        The Default Package Type wins, the way UPS and the other connectors
        rate an order: a declared box is one that exists, and the carrier
        prices the box it is handed rather than a shape derived from what is
        inside it.

        Volume is the fallback for an order quoted with no box configured. It
        measures the goods honestly but describes a cube no carrier stocks, so
        it is second, not first. With neither, dimensions are left off and the
        rate comes back on weight alone - reported, because a quote priced
        without dimensions is not what a dimensional carrier will bill.
        """
        target = "cm" if weight_unit == "kg" else "in"

        package_type = self.inxpress_default_package_type_id
        if self.inxpress_shipping_mode == "freight":
            # A freight payload declares packageType as a handling unit, so the
            # sides sent with it have to be that unit's, not the carton's
            package_type = self.inxpress_freight_package_type_id or package_type
        if package_type:
            return {
                "length": self._inxpress_length_value(package_type.packaging_length, target),
                "width": self._inxpress_length_value(package_type.width, target),
                "height": self._inxpress_length_value(package_type.height, target),
                "dimensionUnit": target,
            }

        volume = order._inxpress_goods_volume() if order else 0.0
        if volume > 0:
            side = self._inxpress_volume_side(volume, target)
            return {
                "length": side,
                "width": side,
                "height": side,
                "dimensionUnit": target,
            }

        _logger.warning(
            "InXpress: %s has no product volumes and %s has no Default "
            "Package Type, so it is quoted without dimensions. Set a Default "
            "Package Type to be rated on a real box.",
            order.name if order else "this order", self.name,
        )
        return {}

    def _inxpress_piece_count(self, picking):
        """Number of physical parcels, not the quantity of goods inside them.

        Real packages win when the picking has been packed, counted through
        Odoo's own rules so a partly packed picking includes its Bulk Content
        parcel.

        An unpacked picking falls back to the count declared on the transfer,
        and to one parcel when that is blank. Software cannot know how many
        boxes the goods went into: the only honest sources are the packing
        Odoo recorded and the number a person typed. Counting units instead
        would book a parcel per item, which is a different shipment.
        """
        packages = self._inxpress_delivery_packages(picking)
        return len(packages) or picking.inxpress_number_of_packages or 1

    def _inxpress_shipment_weight(self, picking):
        """Total shipment weight in the Odoo weight unit, one source only.

        The picking is the truth once it exists: Odoo sums it from the packages.
        The sale order is only consulted when the picking carries no weight at
        all, and a shipment with no weight anywhere is reported rather than
        silently rated as 1 kg.
        """
        weight = picking.shipping_weight or picking.weight or 0.0
        if weight < 0.1 and picking.sale_id:
            weight = picking.sale_id._inxpress_goods_weight()
        if weight < 0.1:
            _logger.warning(
                "InXpress: %s has no weight on its packages, its products or "
                "its order, so it is rated at the 1.0 %s minimum. Set a weight "
                "on the products to be rated correctly.",
                picking.name, self._inxpress_odoo_weight_unit(),
            )
            weight = 1.0
        return weight

    def _inxpress_delivery_packages(self, picking):
        """The picking's real parcels, using Odoo's own packing rules.

        Reuses stock_delivery's _get_packages_from_picking, so a packed picking
        yields one DeliveryPackage per box with that box's own weight and
        package type, and anything left unpacked is swept into Bulk Content.
        Returns an empty list when the picking has not been packed.
        """
        try:
            return self._get_packages_from_picking(
                picking, self.env["stock.package.type"]
            )
        except UserError:
            # Raised when nothing is packed and the total weight is 0
            return []

    def _inxpress_packages_payload(self, picking, order, weight_unit):
        """One entry per parcel, each carrying its own weight and dimensions.

        Same choice OCA's delivery_ups_oca makes: real packages when the
        picking is packed, otherwise identical parcels splitting the total
        weight evenly.

        Not sent to InXpress. The public API carries one packageDetails block
        per shipment and nothing else, so this list feeds
        _inxpress_details_dimensions and the uneven-weight warning locally.
        """
        target = "cm" if weight_unit == "kg" else "in"
        packages = self._inxpress_delivery_packages(picking)

        if packages:
            entries = []
            for package in packages:
                dimension = package.dimension or {}
                if any(dimension.get(k) for k in ("length", "width", "height")):
                    dims = {
                        "length": self._inxpress_length_value(dimension["length"], target),
                        "width": self._inxpress_length_value(dimension["width"], target),
                        "height": self._inxpress_length_value(dimension["height"], target),
                        "dimensionUnit": target,
                    }
                else:
                    # No package type on the box: the order's numbers are all there is
                    dims = self._inxpress_dimensions_payload(order, weight_unit)
                entries.append({
                    "weight": self._inxpress_weight_value(package.weight, weight_unit),
                    "weightUnit": weight_unit,
                    **dims,
                })
            return entries

        total_weight = self._inxpress_shipment_weight(picking)
        count = self._inxpress_piece_count(picking)
        return [{
            "weight": self._inxpress_weight_value(total_weight / count, weight_unit),
            "weightUnit": weight_unit,
            **self._inxpress_dimensions_payload(order, weight_unit),
        } for _index in range(count)]

    def _inxpress_details_dimensions(self, packages_payload):
        """Dimensions for packageDetails, which holds one set for all parcels.

        The largest parcel wins: with a single slot, over-declaring the small
        boxes is safer than under-declaring the big one, which is what gets a
        shipment re-billed on dimensional weight.
        """
        largest = max(
            packages_payload,
            key=lambda e: e["length"] * e["width"] * e["height"],
        )
        return {
            "length": largest["length"],
            "width": largest["width"],
            "height": largest["height"],
            "dimensionUnit": largest["dimensionUnit"],
        }

    def _inxpress_warn_uneven_packages(self, picking, packages_payload):
        """Flag boxes whose weights the even split misrepresents.

        packageDetails carries one weight for the whole shipment, so uneven
        boxes are rated at their average. Carriers price per parcel and apply
        surcharges at weight thresholds, so the heaviest box can be under-rated
        and re-billed on arrival.
        """
        weights = [entry["weight"] for entry in packages_payload]
        if len(weights) < 2:
            return
        average = sum(weights) / len(weights)
        if average and max(abs(w - average) for w in weights) / average > 0.2:
            _logger.warning(
                "InXpress: %s ships %d parcels weighing %s but is rated at "
                "%.3f per parcel, because packageDetails carries a single "
                "weight. The heaviest box may be under-rated.",
                picking.name, len(weights),
                ", ".join(f"{w:g}" for w in weights), average,
            )

    def _inxpress_check_quote_drift(self, picking, weight):
        """Refuse to book a shipment the accepted rate was not priced on.

        Weight only. Packing wins on the parcel count: the quote is taken
        before anyone packs, so the count it carries is a guess and the boxes
        on the picking are the fact. A 1 parcel quote booked as 3 parcels goes
        through and is billed at the carrier's real price.

        Orders quoted before this snapshot existed carry no basis and are let
        through.
        """
        order = picking.sale_id
        if not order or not order.inxpress_quoted_weight:
            return

        quoted_weight = order.inxpress_quoted_weight
        unit = self._inxpress_odoo_weight_unit()

        # Tolerate rounding and packaging tare, not a real difference
        if abs(weight - quoted_weight) <= max(0.5, quoted_weight * 0.05):
            return

        drift = _(
            "quoted at %(quoted).3f %(unit)s but weighs %(actual).3f %(unit)s",
            quoted=quoted_weight, actual=weight, unit=unit,
        )

        raise UserError(_(
            "InXpress: %(picking)s was %(drift)s.\n\n"
            "The accepted rate was priced on the old numbers. Refresh the "
            "rates on %(order)s and pick a rate again before validating.",
            picking=picking.name,
            drift=drift,
            order=order.name,
        ))

    def _inxpress_customs_currency(self, company=None):
        """Currency InXpress reads monetary values in: the company's own.

        The dispatch declares it as payload.currency, so converting customs
        values into anything else would label them with a currency they are
        not in.
        """
        self.ensure_one()
        return (company or self.env.company).currency_id

    def _inxpress_shipper_country(self, picking):
        """Country the goods ship from: warehouse address, then company address."""
        company = picking.company_id or self.env.company
        return (
            picking.picking_type_id.warehouse_id.partner_id.country_id.code
            or company.partner_id.country_id.code
            or ""
        )

    def _inxpress_is_international(self, picking):
        """True when the shipment crosses a border and needs customs data."""
        receiver_country = picking.partner_id.country_id.code or ""
        shipper_country = self._inxpress_shipper_country(picking)
        return bool(
            receiver_country
            and shipper_country
            and receiver_country != shipper_country
        )

    def _inxpress_incoterm(self, picking):
        """Incoterm to declare, and where it came from, as (code, source).

        DHL requires content/incoterm on a customs-declarable shipment, and its
        dispatch transform reads the field unguarded: sending nothing aborted
        the transform and DHL rejected the resulting error body with every
        required key reported missing, never mentioning the incoterm.

        The sale order's Incoterm wins, then the company default (Accounting >
        Settings > Default Incoterm), both guarded with _fields because they
        are optional installs. Last comes this method's Duties Paid By, the
        same choice Odoo's UPS connector asks for: Sender is DDP, Recipient is
        DAP. It used to be a hard-coded DAP, which a company paying its
        customers' duties never saw. The source is returned so the dispatch
        can say on the transfer which one was used.
        """
        order = picking.sale_id
        if order and "incoterm" in order._fields and order.incoterm:
            return order.incoterm.code, _("the sales order")

        company = picking.company_id or self.env.company
        if "incoterm_id" in company._fields and company.incoterm_id:
            return company.incoterm_id.code, _("the company default")

        code = "DDP" if self.inxpress_duties_paid_by == "sender" else "DAP"
        return code, _("Duties Paid By on %s", self.name)

    def _inxpress_line_weight(self, commodity, weight_unit):
        """Weight of this whole customs line, in the order's weight unit.

        DHL puts it on the export declaration as the line's net and gross
        weight. Odoo's DeliveryCommodity carries no weight of its own, so it is
        the product weight times the declared quantity - product.weight is per
        unit, in the database weight UoM, which is what _inxpress_weight_value
        converts from.

        Returns None for a weightless product rather than 0: the API rejects a
        non-positive weight, and omitting the field leaves the line as it was
        before, which the carrier accepts.
        """
        unit_weight = commodity.product_id.weight or 0.0
        if not unit_weight:
            return None
        return self._inxpress_weight_value(
            unit_weight * (commodity.qty or 0.0), weight_unit
        )

    def _inxpress_unit_value(self, move, target_currency):
        """Customs value of ONE item: discount applied, tax excluded.

        Converted into the company currency, because customsItems carries no
        currency of its own while sale order lines are in the order currency.
        """
        company = move.company_id or self.env.company
        line = move.sale_line_id
        if line and line.product_uom_qty:
            value = line.price_subtotal / line.product_uom_qty
            currency = line.currency_id
        else:
            value = move.product_id.list_price
            currency = company.currency_id
        if target_currency and currency and target_currency != currency:
            value = currency._convert(
                value, target_currency, company, fields.Date.context_today(self)
            )
        # Free and fully discounted lines would be rejected as unitValue < 0.01
        return round(value, 2) or 0.01

    def _inxpress_customs_items(self, picking, weight_unit):
        """Build the customsItems array for an international dispatch.

        One entry per commodity for the whole shipment, which is the shape both
        DHL and UPS declare. Quantities come from Odoo's own commodity rule, so
        they are the quantities done rather than ordered, converted to the
        product's UoM and grouped per product: ship 2 of 3 totes and the
        declaration says 2. HS code and origin country come from the product
        form (Inventory tab, Logistics group).
        """
        target_currency = self._inxpress_customs_currency(picking.company_id)
        origin_default = self._inxpress_shipper_country(picking)

        # Nothing picked yet leaves no done quantity to declare, so fall back
        # to the demand rather than sending an empty declaration
        move_lines = picking.move_line_ids.filtered(lambda line: line.quantity)
        commodities = self._get_commodities_from_stock_move_lines(
            move_lines or picking.move_line_ids
        )

        items = []
        missing_hs_code = []
        for commodity in commodities:
            product = commodity.product_id
            move = picking.move_ids.filtered(
                lambda m, product=product: m.product_id == product
            )[:1]
            hs_code = (product.hs_code or "")[:20]
            if not hs_code:
                missing_hs_code.append(product.display_name)
            items.append({
                "description": (product.name or "")[:255],
                "quantity": int(commodity.qty),
                "unitValue": self._inxpress_unit_value(move, target_currency),
                "originCountry": (
                    product.country_of_origin.code
                    or commodity.country_of_origin
                    or origin_default
                ),
                "hsCode": hs_code,
            })
            line_weight = self._inxpress_line_weight(commodity, weight_unit)
            if line_weight:
                items[-1]["weight"] = line_weight

        if missing_hs_code:
            _logger.warning(
                "InXpress customs: no HS Code on %s for %s. "
                "Set it on the product under Inventory > Logistics > HS Code "
                "to avoid customs delays.",
                ", ".join(missing_hs_code), picking.name,
            )
        return items

    def _inxpress_freight_accessorials(self):
        """WSX optional_accessorials codes for the boxes ticked on the carrier."""
        codes = []
        if self.inxpress_freight_residential:
            codes.append("RESIDENTIAL")
        if self.inxpress_freight_liftgate_delivery:
            codes.append("HYDRAUL")
        if self.inxpress_freight_liftgate_pickup:
            codes.append("TAILPICK")
        return codes

    def _inxpress_freight_class(self, products):
        """The class to rate these goods at, highest of the ones declared.

        WSX rates the whole order as a single handling unit, so several classes
        cannot be sent. The highest present is the conservative choice: a class
        declared too low is not refused at quote time, it is reclassified by the
        carrier after delivery and billed as an adjustment, which is the outcome
        this is picked to avoid.

        Products with no class set are ignored rather than treated as the
        lowest, so one unconfigured product cannot drag the whole order down.
        Nothing declared at all falls back to the delivery method's own class.
        """
        declared = [
            product.inxpress_freight_class
            for product in products
            if product.inxpress_freight_class
        ]
        if not declared:
            return self.inxpress_freight_class
        return max(declared, key=float)

    def _inxpress_apply_freight(self, payload, products=None):
        """Turn a parcel payload into a freight one, in place.

        Parcel is the API default, so nothing is sent in parcel mode and
        existing behaviour is untouched.
        """
        if self.inxpress_shipping_mode != "freight":
            return payload

        payload["mode"] = "FREIGHT"
        # WSX never defaults this and maps it straight onto an enum downstream,
        # so a null reaches inxpress-shipment-rest and is rejected there. The
        # rejection comes back as an opaque 500, not a 400. One handling unit
        # is what this module quotes, so SINGLE is the honest value.
        payload["stackType"] = "SINGLE"
        accessorials = self._inxpress_freight_accessorials()
        if accessorials:
            payload["accessorials"] = accessorials

        details = payload.setdefault("packageDetails", {})
        freight_class = self._inxpress_freight_class(products or self.env["product.product"])
        if freight_class:
            details["classCode"] = freight_class
        # Never leave this off. Omitting it does not mean "no preference": WSX
        # substitutes its parcel default, Customer Packaging, which the outbound
        # mapping turns into Carton, which LTL providers reject. The rejection
        # comes back as an opaque 500.
        details["packageType"] = self.inxpress_handling_unit_type or "Pallet"
        return payload

    def _inxpress_quote_payload(self, order):
        """Build the quote request payload from a sale.order."""
        receiver = order.partner_shipping_id

        product_lines = order.order_line.filtered(
            lambda line: line.product_id and not line.is_delivery
        )
        # Use shipping_weight from the order if set, otherwise sum from products
        total_weight = order._inxpress_total_weight()
        weight_unit = self._inxpress_odoo_weight_unit()

        # Recorded so the dispatch can refuse to ship a different weight
        order.inxpress_quoted_weight = total_weight

        payload = {
            "destination": {
                "name": receiver.name or "",
                "address1": receiver.street or "",
                "city": receiver.city or "",
                "state": receiver.state_id.code or "",
                "postalCode": receiver.zip or "",
                "countryCode": receiver.country_id.code or "",
            },
            "packageDetails": {
                # One parcel holding the whole order. Nothing is packed when a
                # quote is taken, so a count here would be a guess the dispatch
                # then contradicts; the packed picking is what books.
                "weight": self._inxpress_weight_value(total_weight, weight_unit),
                "weightUnit": weight_unit,
                **self._inxpress_dimensions_payload(order, weight_unit),
                "numberOfUnits": 1,
            },
        }

        return self._inxpress_apply_freight(payload, product_lines.product_id)

    def _inxpress_fetch_quotes(self, order):
        """Call InXpress quote API.

        The new quote endpoint resolves services server-side,
        so a separate get_services() call is no longer needed.
        """
        client = self._inxpress_client()

        payload = self._inxpress_quote_payload(order)
        result = client.get_quote(payload)
        quote_items = result.get("rates", [])

        # Filter to specific carrier if configured
        if self.inxpress_carrier_code:
            quote_items = [
                q for q in quote_items
                if q.get("carrierCode") == self.inxpress_carrier_code
            ]

        return quote_items, result.get("reasonForExclusion", []), []

    # ------------------------------------------------------------------
    # delivery.carrier interface: rate_shipment
    # ------------------------------------------------------------------

    def inxpress_rate_shipment(self, order):
        """Called by Odoo to get the shipping rate at checkout / SO.

        Returns the cheapest rate. Users can click "Compare Rates"
        on the SO to see all rates and pick a different one.
        """
        try:
            quote_items, exclusions, services_raw = self._inxpress_fetch_quotes(order)
        except UserError as e:
            # Written for the user: missing configuration, a stale quote
            return {
                "success": False,
                "price": 0.0,
                "error_message": str(e),
                "warning_message": False,
            }
        except Exception as e:
            return {
                "success": False,
                "price": 0.0,
                "error_message": str(self._inxpress_opaque_error(e, "quote", order)),
                "warning_message": False,
            }

        # Deduplicate: keep one rate per serviceCode
        seen = {}
        for item in quote_items:
            # Freight rates repeat a serviceCode per level, so the level is
            # part of the identity; parcel rates carry none and are unaffected
            key = (
                item.get("serviceCode", "") or item.get("serviceName", ""),
                item.get("serviceLevelCode", ""),
            )
            if key not in seen:
                seen[key] = item
        quote_items = list(seen.values())

        # Log all returned rates
        _logger.info(
            "InXpress quote returned %d unique rates, %d exclusions",
            len(quote_items), len(exclusions),
        )
        for idx, item in enumerate(quote_items):
            _logger.debug(
                "  Rate %d: carrier=%s (%s), service=%s, price=%s, transit=%s days",
                idx + 1,
                item.get("carrierName", ""),
                item.get("carrierCode", ""),
                item.get("serviceName", ""),
                item.get("amount", "N/A"),
                item.get("transitTime", "N/A"),
            )
        for exc in exclusions[:5]:  # Log first 5 exclusions
            _logger.debug(
                "  Excluded: carrier=%s, code=%s, reason=%s",
                exc.get("scac", ""),
                exc.get("code", ""),
                exc.get("resolution", ""),
            )
        if len(exclusions) > 5:
            _logger.debug("  ... and %d more exclusions", len(exclusions) - 5)

        if not quote_items:
            msg = "No rates returned"
            if exclusions:
                msgs = [e.get("message", "") for e in exclusions if e.get("message")]
                if msgs:
                    msg = "; ".join(msgs)
            return {
                "success": False,
                "price": 0.0,
                "error_message": msg,
                "warning_message": False,
            }

        # Pick cheapest
        best = min(quote_items, key=lambda q: q.get("amount", 0) or float("inf"))
        _logger.debug(
            "InXpress selected cheapest: carrier=%s (%s), service=%s, price=%s",
            best.get("carrierName", ""),
            best.get("carrierCode", ""),
            best.get("serviceName", ""),
            best.get("amount", "N/A"),
        )

        # Store all quotes and services on the order
        order.inxpress_all_quotes = json.dumps(quote_items)
        order.inxpress_selected_carrier_code = best.get("carrierCode", "")
        order.inxpress_selected_carrier_name = best.get("carrierName", "")
        order.inxpress_selected_service_name = best.get("serviceName", "")
        order.inxpress_selected_service_code = best.get("serviceCode", "")
        order.inxpress_selected_service_level_code = best.get("serviceLevelCode", "")
        order.inxpress_quote_data = json.dumps(best)

        carrier_name = best.get("carrierName", "")
        service_name = best.get("serviceName", "")
        num_rates = len(quote_items)
        warning = False
        if num_rates > 1:
            warning = _(
                "Showing cheapest rate (%(carrier)s - %(service)s). "
                "%(count)s rates available; click 'Compare Rates' to compare.",
                carrier=carrier_name,
                service=service_name,
                count=num_rates,
            )

        return {
            "success": True,
            "price": best.get("amount", 0.0),
            "error_message": False,
            "warning_message": warning,
        }

    # ------------------------------------------------------------------
    # Rate wizard launcher
    # ------------------------------------------------------------------

    def action_inxpress_view_rates(self, order):
        """Open the rate selection wizard with all cached quotes."""
        self.ensure_one()
        raw = order.inxpress_all_quotes
        if not raw:
            raise UserError(_("No rates cached. Please click 'Add Shipping' first."))

        quote_items = json.loads(raw)
        weight_unit = self._inxpress_odoo_weight_unit()
        wizard = self.env["inxpress.rate.wizard"].create({
            "order_id": order.id,
            "carrier_id": self.id,
            "weight_unit": weight_unit,
            "total_weight": self._inxpress_weight_value(
                order._inxpress_total_weight(), weight_unit
            ),
        })

        for q in quote_items:
            self.env["inxpress.rate.wizard.line"].create({
                "wizard_id": wizard.id,
                "carrier_code": q.get("carrierCode", ""),
                "carrier_name": q.get("carrierName", ""),
                "service_name": q.get("serviceName", ""),
                "service_code": q.get("serviceCode", ""),
                "service_level_code": q.get("serviceLevelCode", ""),
                "price": q.get("amount", 0.0),
                "transit_time": q.get("transitTime", ""),
                "currency": q.get("currency", "USD"),
                "quote_data": json.dumps(q),
            })

        return {
            "type": "ir.actions.act_window",
            "name": _("Select Shipping Rate"),
            "res_model": "inxpress.rate.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    # ------------------------------------------------------------------
    # delivery.carrier interface: send_shipping
    # ------------------------------------------------------------------

    def inxpress_send_shipping(self, pickings):
        """Book shipments with InXpress."""
        results = []
        client = self._inxpress_client()

        for picking in pickings:
            receiver = picking.partner_id
            order = picking.sale_id

            # Get selected service code from the quote the user chose
            service_code = ""
            if order:
                service_code = order.inxpress_selected_service_code or ""

            if not service_code:
                raise UserError(_(
                    "No service code found for picking %s. Please select a rate first.",
                    picking.name,
                ))

            weight = self._inxpress_shipment_weight(picking)

            # Build commodity description from picking moves
            commodity_description = ", ".join(
                move.product_id.name for move in picking.move_ids if move.product_id.name
            )[:200] or "General Merchandise"

            weight_unit = self._inxpress_odoo_weight_unit()
            piece_count = self._inxpress_piece_count(picking)

            # The rate was priced on a weight: refuse to book a different one
            self._inxpress_check_quote_drift(picking, weight)

            packages = self._inxpress_packages_payload(picking, order, weight_unit)
            self._inxpress_warn_uneven_packages(picking, packages)

            payload = {
                "partner": "ODOO",
                "orderNumber": order.name if order else picking.name,
                "serviceCode": service_code,
                "destination": {
                    "name": receiver.commercial_company_name or receiver.name or "",
                    "contactName": receiver.name or "",
                    "email": receiver.email or "",
                    "phone": receiver.phone or receiver.mobile or "",
                    "address1": receiver.street or "",
                    "address2": receiver.street2 or "",
                    "city": receiver.city or "",
                    "state": receiver.state_id.code or "",
                    "postalCode": receiver.zip or "",
                    "countryCode": receiver.country_id.code or "",
                },
                # The company's own currency, the one Odoo prices this order in
                "currency": (picking.company_id or self.env.company).currency_id.name,
                "packageDetails": {
                    # Weight is read per parcel, so the shipment total is split
                    "weight": self._inxpress_weight_value(
                        weight / piece_count, weight_unit
                    ),
                    "weightUnit": weight_unit,
                    **self._inxpress_details_dimensions(packages),
                    "numberOfUnits": piece_count,
                    "commodityDescription": commodity_description,
                },
                "idempotencyKey": picking._inxpress_idempotency_key(),
            }

            self._inxpress_apply_freight(payload, picking.move_ids.product_id)
            # Freight rates from one carrier share a serviceCode and differ
            # only by level, so the booking needs the level the user picked
            service_level_code = (
                order.inxpress_selected_service_level_code if order else ""
            )
            if service_level_code:
                payload["serviceLevelCode"] = service_level_code

            incoterm_source = None
            if self._inxpress_is_international(picking):
                # Declarable lane: DHL needs the incoterm whether or not the
                # goods lines make it through, so it is set before them
                payload["incoterm"], incoterm_source = self._inxpress_incoterm(picking)
                customs_items = self._inxpress_customs_items(picking, weight_unit)
                if customs_items:
                    payload["customsItems"] = customs_items

            # Serialised by hand: requests' json= cannot encode a date object
            invoice_date = order.inxpress_invoice_issue_date if order else False
            if invoice_date:
                payload["invoiceIssueDate"] = fields.Date.to_string(invoice_date)

            try:
                resp = client.dispatch_shipment(payload)
                # CommerceLabelResponse uses shipmentId/trackingNumber; the
                # older shipment payload uses id/airbillNumber
                shipment_id = resp.get("shipmentId") or resp.get("id")
                tracking = (
                    resp.get("trackingNumber")
                    or resp.get("airbillNumber")
                    or ""
                )
                # CommerceLabelResponse prices the booking as amount; the
                # older shipment payload uses displayPrice/price
                price = (
                    resp.get("amount")
                    or resp.get("displayAmount")
                    or resp.get("displayPrice")
                    or resp.get("price")
                    or 0.0
                )

                # Not a UserError: nothing here is the user's to fix, so it
                # goes out through the generic handler below like any other fault
                if not shipment_id:
                    raise ValueError("dispatch response carried no shipment id")

                picking.inxpress_shipment_id = shipment_id
                picking.inxpress_label_url = resp.get("labelUrl") or ""
                self._inxpress_attach_label(
                    client, picking, shipment_id, resp.get("labelUrl"),
                )
                if incoterm_source:
                    picking.message_post(body=_(
                        "InXpress customs: Incoterm %(code)s sent, taken from "
                        "%(origin)s.",
                        code=payload["incoterm"], origin=incoterm_source,
                    ))

                results.append({
                    "exact_price": float(price),
                    "tracking_number": tracking,
                })
            except UserError:
                raise
            except Exception as e:
                raise self._inxpress_opaque_error(e, "dispatch", picking) from e

        return results

    def _inxpress_attach_label(self, client, picking, shipment_id, label_url=None):
        """Download the shipping label PDF and attach it to the picking.

        Returns the attachment, or an empty recordset when the label could not
        be fetched.
        """
        if not shipment_id:
            return self.env["ir.attachment"]
        try:
            pdf_bytes = self._inxpress_fetch_label_bytes(
                client, shipment_id, label_url,
            )
        except Exception:
            _logger.warning(
                "Could not fetch label for shipment %s", shipment_id, exc_info=True,
            )
            return self.env["ir.attachment"]

        if not pdf_bytes:
            return self.env["ir.attachment"]

        name = f"InXpress-Label-{shipment_id}.pdf"
        values = {
            "name": name,
            "type": "binary",
            "datas": base64.b64encode(pdf_bytes),
            "res_model": "stock.picking",
            "res_id": picking.id,
            "mimetype": "application/pdf",
        }
        # Re-printing refreshes the existing attachment instead of piling up copies
        attachment = self.env["ir.attachment"].search([
            ("res_model", "=", "stock.picking"),
            ("res_id", "=", picking.id),
            ("name", "=", name),
        ], limit=1)
        if attachment:
            attachment.write(values)
            return attachment
        return self.env["ir.attachment"].create(values)

    def _inxpress_fetch_label_bytes(self, client, shipment_id, label_url=None):
        """Return the label PDF bytes, preferring the dispatch response URL.

        That URL is presigned and short-lived, so a failure asks the shipment
        for a fresh one: the documented re-presign, which serves the PDF that
        was already bought rather than rendering a new one. The document
        endpoint stays as the last resort for shipments the newer path does
        not answer for.
        """
        if label_url:
            try:
                return client.download_label_url(label_url)
            except Exception:
                _logger.warning(
                    "Presigned label URL expired or failed for shipment %s, "
                    "asking for a fresh one", shipment_id, exc_info=True,
                )

        try:
            fresh = (client.get_label_url(shipment_id) or {}).get("labelUrl")
            if fresh:
                return client.download_label_url(fresh)
        except Exception:
            _logger.warning(
                "Label re-presign failed for shipment %s, falling back to the "
                "document endpoint", shipment_id, exc_info=True,
            )

        return client.get_label(shipment_id)

    # ------------------------------------------------------------------
    # delivery.carrier interface: get_tracking_link
    # ------------------------------------------------------------------

    def inxpress_fetch_tracking(self, picking):
        """Current tracking state for a dispatched picking, {} when there is none.

        Keyed on the InXpress shipment id kept from the dispatch: the carrier
        tracking number will not address this endpoint. A 404 here means the
        shipment belongs to another account rather than that it was deleted,
        so the token is the first thing to check.
        """
        if not picking.inxpress_shipment_id:
            return {}
        try:
            client = self._inxpress_client()
            return client.get_tracking(picking.inxpress_shipment_id) or {}
        except Exception:
            _logger.warning(
                "Could not fetch tracking for %s (shipment %s)",
                picking.name, picking.inxpress_shipment_id, exc_info=True,
            )
            return {}

    def inxpress_get_tracking_link(self, picking):
        """Return the carrier deep-link InXpress reports for this shipment.

        Null when the carrier has no link template or there is no tracking
        number yet, in which case Odoo shows no link rather than an empty one.
        """
        return self.inxpress_fetch_tracking(picking).get("trackingLink") or False

    # ------------------------------------------------------------------
    # delivery.carrier interface: cancel_shipment
    # ------------------------------------------------------------------

    def inxpress_cancel_shipment(self, pickings):
        """Cancel shipments via InXpress."""
        client = self._inxpress_client()
        for picking in pickings:
            shipment_id = picking.inxpress_shipment_id
            if not shipment_id:
                raise UserError(_(
                    "No InXpress shipment ID found on picking %s.",
                    picking.name,
                ))
            try:
                client.void_shipment(shipment_id, "Cancelled from Odoo")
                picking.carrier_tracking_ref = False
                picking.inxpress_shipment_id = False
                picking.inxpress_label_url = False
                # A deliberate re-dispatch is a new booking, so it needs a new key
                picking.inxpress_dispatch_seq += 1
            except UserError:
                raise
            except Exception as e:
                raise self._inxpress_opaque_error(e, "cancellation", picking) from e


class SaleOrder(models.Model):
    _inherit = "sale.order"

    inxpress_all_quotes = fields.Text(
        string="InXpress All Quotes (JSON)",
        copy=False,
    )
    inxpress_selected_carrier_code = fields.Char(
        string="Selected Carrier Code",
        copy=False,
    )
    inxpress_selected_carrier_name = fields.Char(
        string="Selected Carrier Name",
        copy=False,
    )
    inxpress_selected_service_name = fields.Char(
        string="Selected Service",
        copy=False,
    )
    inxpress_selected_service_code = fields.Char(
        string="Selected Service Code",
        copy=False,
    )
    inxpress_selected_service_level_code = fields.Char(
        string="InXpress Service Level Code",
        copy=False,
        help="Freight only: the level of the chosen rate, sent as "
             "serviceLevelCode so the booking matches the quoted rate.",
    )
    inxpress_quote_data = fields.Text(
        string="Selected Quote Data (JSON)",
        copy=False,
    )
    inxpress_services_data = fields.Text(
        string="InXpress Services (JSON)",
        copy=False,
    )
    inxpress_commercial_invoice_number = fields.Char(
        string="Commercial Invoice Number",
        copy=False,
        help="Required for international DHL shipments.",
    )
    inxpress_invoice_issue_date = fields.Date(
        string="Invoice Issue Date",
        copy=False,
        help="Required for international DHL shipments.",
    )
    inxpress_quoted_weight = fields.Float(
        string="Quoted Weight",
        readonly=True,
        copy=False,
        help="Total weight the last rates were priced on, in the Odoo weight "
             "unit.",
    )
    inxpress_content_type = fields.Selection(
        [("PACKAGE", "Package"), ("DOCUMENT", "Document")],
        string="Content Type",
        default="PACKAGE",
        copy=False,
        help="Package for physical goods, Document for paperwork only.",
    )

    def _inxpress_goods_volume(self):
        """Volume of the goods on this order, 0.0 when none is set."""
        self.ensure_one()
        return sum(
            (line.product_id.volume or 0.0) * line.product_uom_qty
            for line in self.order_line
            if line.product_id and not line.is_delivery
        )

    def _inxpress_goods_weight(self):
        """Real weight of the goods on this order, 0.0 when none is set."""
        self.ensure_one()
        return self.shipping_weight or sum(
            (line.product_id.weight or 0.0) * line.product_uom_qty
            for line in self.order_line
            if line.product_id and not line.is_delivery
        )

    def _inxpress_total_weight(self):
        """Weight sent to InXpress as packageDetails.weight.

        Falls back to 1.0 so the API is never sent a zero, but says so: a
        quote rated at 1 kg against a shipment that really weighs 28 kg is
        the gap that makes the accepted price meaningless.
        """
        self.ensure_one()
        weight = self._inxpress_goods_weight()
        if weight < 0.1:
            _logger.warning(
                "InXpress: %s has no product weights, so it is quoted at the "
                "1.0 minimum. Set a weight on the products before quoting.",
                self.name,
            )
            return 1.0
        return weight

    def action_inxpress_view_rates(self):
        """Button action to open the rate selection wizard."""
        self.ensure_one()
        carrier = self.carrier_id
        if not carrier or carrier.delivery_type != "inxpress":
            raise UserError(_("Please select a InXpress delivery method first."))
        return carrier.action_inxpress_view_rates(self)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    inxpress_shipment_id = fields.Char(
        string="InXpress Shipment ID",
        copy=False,
    )
    inxpress_label_url = fields.Char(
        string="InXpress Label URL",
        copy=False,
        help="Presigned label URL returned by the last dispatch. Expires, so "
             "printing falls back to the document endpoint.",
    )
    inxpress_dispatch_seq = fields.Integer(
        string="InXpress Dispatch Attempt",
        default=0,
        copy=False,
        help="Incremented when a shipment is cancelled, so a re-dispatch is "
             "treated as a new booking instead of a retry.",
    )
    inxpress_tracking_status = fields.Char(
        string="InXpress Tracking Status",
        readonly=True,
        copy=False,
        help="Last status reported by the carrier, as InXpress spells it. "
             "Polling stops once it reaches a terminal state.",
    )
    inxpress_tracking_date = fields.Datetime(
        string="InXpress Tracking Checked",
        readonly=True,
        copy=False,
    )
    inxpress_number_of_packages = fields.Integer(
        string="InXpress Parcels",
        default=0,
        copy=False,
        help="How many boxes this transfer ships in, used when nothing has "
             "been packed in Odoo. Leave at 0 to ship as a single parcel. "
             "Packing the transfer overrides it.",
    )
    inxpress_tracking_history = fields.Text(
        string="InXpress Tracking History",
        readonly=True,
        copy=False,
        help="Checkpoints as InXpress last reported them. Rewritten in full "
             "on every sync, so it reflects one moment rather than "
             "accumulating: the carrier restates its own history and a "
             "correction there has to be able to correct what is shown here.",
    )
    inxpress_has_label = fields.Boolean(
        string="InXpress Label Attached",
        compute="_compute_inxpress_has_label",
        help="Whether the label PDF is already on the transfer. Only drives "
             "the form: with the label attached, fetching it again would just "
             "re-download the document the paperclip already serves.",
    )

    @api.depends("inxpress_shipment_id")
    def _compute_inxpress_has_label(self):
        """Whether this transfer already carries its InXpress label.

        Named the same way _inxpress_attach_label names it, so the two cannot
        drift apart without the button reappearing.
        """
        for picking in self:
            picking.inxpress_has_label = bool(
                picking.inxpress_shipment_id
                and self.env["ir.attachment"].search_count([
                    ("res_model", "=", "stock.picking"),
                    ("res_id", "=", picking.id),
                    ("name", "=",
                     f"InXpress-Label-{picking.inxpress_shipment_id}.pdf"),
                ], limit=1)
            )

    def open_website_url(self):
        """Refresh the carrier status before opening the tracking page.

        The Tracking button is the one place a user asks InXpress where the
        parcel is, so the status on screen is brought up to date in the same
        click rather than needing a button of its own. A failed refresh is
        swallowed by _inxpress_sync_tracking, so the link still opens.
        """
        self.filtered(
            lambda p: p.inxpress_shipment_id
            and p.carrier_id.delivery_type == "inxpress"
        )._inxpress_sync_tracking()
        return super().open_website_url()

    @api.model
    def _inxpress_tracking_events(self, tracking):
        """Checkpoints out of a tracking response, oldest first.

        The one place the response shape is read. An endpoint that names its
        fields differently is handled here and nowhere else.
        """
        events = []
        for raw in tracking.get("trackingEvents") or []:
            events.append({
                "date": raw.get("eventDateTime") or raw.get("date") or "",
                "status": raw.get("statusEnumName") or raw.get("status") or "",
                "location": raw.get("location") or "",
                "description": raw.get("description") or "",
            })
        return events

    @api.model
    def _inxpress_format_events(self, events):
        """Checkpoints as the lines shown on the transfer, oldest first.

        Empty when there are none, so the field stays hidden rather than
        showing a heading over nothing.
        """
        lines = []
        for event in events:
            when = (event["date"] or "").replace("T", " ")[:16]
            where = " - %s" % event["location"] if event["location"] else ""
            what = event["status"] or event["description"] or "?"
            lines.append("%s  %s%s" % (when or "?", what, where))
        return "\n".join(lines)

    def _inxpress_sync_tracking(self):
        """Store the reported status, and the tracking number once it exists.

        Checkpoints are copied onto the picking as well, so the history is
        readable without leaving Odoo. The carrier's page stays the record:
        what is stored here is only what the last sync was told.
        """
        for picking in self:
            carrier = picking.carrier_id
            if not picking.inxpress_shipment_id or carrier.delivery_type != "inxpress":
                continue

            tracking = carrier.inxpress_fetch_tracking(picking)
            if not tracking:
                continue

            values = {
                "inxpress_tracking_status": tracking.get("statusEnumName") or "unknown",
                "inxpress_tracking_date": fields.Datetime.now(),
                "inxpress_tracking_history": self._inxpress_format_events(
                    self._inxpress_tracking_events(tracking)
                ),
            }
            # The carrier can assign the number after the booking is accepted
            pro_number = tracking.get("proNumber")
            if pro_number and pro_number != picking.carrier_tracking_ref:
                values["carrier_tracking_ref"] = pro_number
            picking.write(values)

    @api.model
    def _inxpress_cron_sync_tracking(self):
        """Poll shipments that are still moving, called by the scheduled action."""
        pickings = self.search([
            ("inxpress_shipment_id", "!=", False),
            ("carrier_id.delivery_type", "=", "inxpress"),
            "|",
            ("inxpress_tracking_status", "=", False),
            ("inxpress_tracking_status", "not in", list(INXPRESS_TRACKING_TERMINAL)),
        ])
        _logger.info("InXpress tracking: polling %d shipment(s)", len(pickings))
        pickings._inxpress_sync_tracking()

    def action_inxpress_print_label(self):
        """Fetch the label from the dispatch response URL and open it."""
        self.ensure_one()
        if not self.inxpress_shipment_id:
            raise UserError(_("This transfer has no InXpress shipment yet."))
        if self.carrier_id.delivery_type != "inxpress":
            raise UserError(_("This transfer is not shipped via InXpress."))

        client = self.carrier_id._inxpress_client()
        attachment = self.carrier_id._inxpress_attach_label(
            client, self, self.inxpress_shipment_id, self.inxpress_label_url,
        )
        if not attachment:
            raise UserError(_(
                "Could not fetch the label for shipment %s.",
                self.inxpress_shipment_id,
            ))

        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=false",
            "target": "new",
        }

    def _inxpress_idempotency_key(self):
        """Stable key for this booking so a retry cannot double-book.

        Derived, not stored: a failed dispatch raises UserError, which rolls
        the transaction back, so a key written during the attempt would be lost
        and the retry would look like a new request.
        """
        self.ensure_one()
        return f"odoo-picking-{self.id}-{self.inxpress_dispatch_seq}"
