from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInXpressPayload(TransactionCase):
    """Payload shape. No HTTP: these build the body, they do not send it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.delivery_product = cls.env["product.product"].create({
            "name": "InXpress Delivery",
            "type": "service",
        })
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "InXpress Test",
            "delivery_type": "inxpress",
            "product_id": cls.delivery_product.id,
            "inxpress_base_url": "https://example.invalid",
            "inxpress_api_token": "ixpx_test",
        })
        cls.package_type = cls.env["stock.package.type"].create({
            "name": "InXpress Test Box",
            "packaging_length": 400,
            "width": 300,
            "height": 200,
        })
        country = cls.env.ref("base.be")
        cls.partner = cls.env["res.partner"].create({
            "name": "Acme Belgium NV",
            "street": "Grote Markt 12",
            "city": "Brussels",
            "zip": "1000",
            "country_id": country.id,
        })
        cls.goods = cls.env["product.product"].create({
            "name": "Test Goods",
            "type": "consu",
            "weight": 5.0,
        })
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "order_line": [(0, 0, {
                "product_id": cls.goods.id,
                "product_uom_qty": 2,
            })],
        })

    def test_quote_payload_has_destination_and_package_details(self):
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertIn("destination", payload)
        self.assertIn("packageDetails", payload)
        self.assertEqual(payload["destination"]["countryCode"], "BE")
        self.assertEqual(payload["destination"]["postalCode"], "1000")

    def test_quote_payload_sends_no_origin(self):
        """Origin comes from the InXpress account, never from Odoo."""
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertNotIn("origin", payload)

    def test_quote_is_always_a_single_parcel(self):
        """Nothing is packed at quote time, so a count would be a guess."""
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertEqual(payload["packageDetails"]["numberOfUnits"], 1)

    def test_quote_records_the_weight_it_was_priced_on(self):
        self.carrier._inxpress_quote_payload(self.order)
        self.assertGreater(self.order.inxpress_quoted_weight, 0.0)

    def test_weight_and_dimension_units_match_systems(self):
        payload = self.carrier._inxpress_quote_payload(self.order)
        details = payload["packageDetails"]
        if details.get("dimensionUnit"):
            expected = "cm" if details["weightUnit"] == "kg" else "in"
            self.assertEqual(details["dimensionUnit"], expected)

    def test_default_package_type_supplies_dimensions(self):
        self.carrier.inxpress_default_package_type_id = self.package_type
        payload = self.carrier._inxpress_quote_payload(self.order)
        details = payload["packageDetails"]
        for key in ("length", "width", "height"):
            self.assertIn(key, details)
            self.assertGreater(details[key], 0.0)

    def test_parcel_mode_sends_no_freight_fields(self):
        """Parcel is the API default; freight keys must stay absent."""
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertNotIn("mode", payload)
        self.assertNotIn("stackType", payload)
        self.assertNotIn("accessorials", payload)

    def test_freight_mode_sets_mode_and_stack_type(self):
        self.carrier.inxpress_shipping_mode = "freight"
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertEqual(payload["mode"], "FREIGHT")
        # WSX maps a null stackType onto an enum and fails with an opaque 500
        self.assertEqual(payload["stackType"], "SINGLE")

    def test_freight_always_declares_a_package_type(self):
        """Omitting it makes WSX substitute Carton, which LTL carriers reject."""
        self.carrier.inxpress_shipping_mode = "freight"
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertTrue(payload["packageDetails"]["packageType"])

    def test_freight_class_is_sent_when_configured(self):
        self.carrier.write({
            "inxpress_shipping_mode": "freight",
            "inxpress_freight_class": "50",
        })
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertEqual(payload["packageDetails"]["classCode"], "50")

    def test_freight_accessorials_reach_the_payload(self):
        self.carrier.write({
            "inxpress_shipping_mode": "freight",
            "inxpress_freight_residential": True,
        })
        payload = self.carrier._inxpress_quote_payload(self.order)
        self.assertIn("RESIDENTIAL", payload["accessorials"])

    def test_address_payload_never_yields_none(self):
        """A blank field must serialise as "", not null."""
        blank = self.env["res.partner"].create({"name": "Blank"})
        address = self.carrier._inxpress_address_payload(blank, "origin")
        for key, value in address.items():
            self.assertIsNotNone(value, f"{key} is None")
            self.assertIsInstance(value, str)
