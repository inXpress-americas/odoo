from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInXpressUnits(TransactionCase):
    """Unit conversion, which is where a wrong number becomes a wrong price."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "InXpress Test",
            "delivery_type": "inxpress",
            "product_id": cls.env["product.product"].create({
                "name": "InXpress Delivery",
                "type": "service",
            }).id,
            "inxpress_base_url": "https://example.invalid",
            "inxpress_api_token": "ixpx_test",
        })

    def test_weight_unchanged_in_native_unit(self):
        native = self.carrier._inxpress_odoo_weight_unit()
        self.assertEqual(self.carrier._inxpress_weight_value(10.0, native), 10.0)

    def test_weight_converted_between_systems(self):
        """The argument is the target unit; the source is always Odoo's own."""
        native = self.carrier._inxpress_odoo_weight_unit()
        converted = self.carrier._inxpress_weight_value(10.0, "lb" if native == "kg" else "kg")
        expected = 10.0 * 2.20462 if native == "kg" else 10.0 / 2.20462
        self.assertAlmostEqual(converted, round(expected, 3), places=2)

    def test_weight_rounded_to_three_places(self):
        value = self.carrier._inxpress_weight_value(1.23456789, "kg")
        self.assertEqual(value, round(value, 3))

    def test_length_conversion_targets_are_supported(self):
        for target in ("cm", "in"):
            value = self.carrier._inxpress_length_value(1000.0, target)
            self.assertGreater(value, 0.0)

    def test_inches_are_smaller_than_centimetres(self):
        """The same physical length is a smaller number in inches."""
        cm = self.carrier._inxpress_length_value(1000.0, "cm")
        inches = self.carrier._inxpress_length_value(1000.0, "in")
        self.assertLess(inches, cm)

    def test_volume_side_is_a_cube_root(self):
        """A cube of volume v has sides of v ** (1/3), not v."""
        side = self.carrier._inxpress_volume_side(0.027, "cm")
        self.assertAlmostEqual(side, 30.0, places=1)

    def test_freight_accessorials_follow_the_checkboxes(self):
        self.assertEqual(self.carrier._inxpress_freight_accessorials(), [])
        self.carrier.write({
            "inxpress_freight_residential": True,
            "inxpress_freight_liftgate_delivery": True,
            "inxpress_freight_liftgate_pickup": True,
        })
        self.assertEqual(
            self.carrier._inxpress_freight_accessorials(),
            ["RESIDENTIAL", "HYDRAUL", "TAILPICK"],
        )
