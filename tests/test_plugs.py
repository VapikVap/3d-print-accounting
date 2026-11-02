"""Разбор ответов розеток. Образцы — по документации производителей, НЕ с реальных
устройств. Ответы с вашего оборудования добавляйте в класс RealSamplesTest."""
import unittest

from app import printers


class DocSamplesTest(unittest.TestCase):
    def test_shelly_gen1_plug_s(self):
        r = printers.parse_shelly1({"relays": [{"ison": True}],
                                    "meters": [{"power": 12.5, "total": 60000, "is_valid": True}]})
        self.assertEqual(r["power_w"], 12.5)
        self.assertAlmostEqual(r["energy_kwh"], 1.0)  # 60000 Вт·мин = 1 кВт·ч
        self.assertTrue(r["on"])

    def test_shelly_gen1_em(self):
        r = printers.parse_shelly1({"emeters": [{"power": 100, "total": 1500}, {"power": 50, "total": 500}]})
        self.assertEqual(r["power_w"], 150)
        self.assertAlmostEqual(r["energy_kwh"], 2.0)

    def test_shelly_gen2(self):
        r = printers.parse_shelly2({"id": 0, "output": True, "apower": 230.4, "aenergy": {"total": 2500.0}})
        self.assertEqual(r["power_w"], 230.4)
        self.assertAlmostEqual(r["energy_kwh"], 2.5)

    def test_tasmota(self):
        r = printers.parse_tasmota({"StatusSNS": {"ENERGY": {"Total": 3.21, "Power": 145, "Voltage": 229}}})
        self.assertEqual(r["power_w"], 145)
        self.assertEqual(r["energy_kwh"], 3.21)

    def test_tasmota_no_sensor(self):
        with self.assertRaises(ValueError):
            printers.parse_tasmota({"StatusSNS": {"Time": "x"}})

    def test_tuya_defaults(self):
        plug = {"dps_power": "19", "dps_energy": "17", "power_scale": 0.1, "energy_scale": 0.001}
        r = printers.parse_tuya({"dps": {"1": True, "17": 1234, "18": 980, "19": 2215, "20": 2301}}, plug)
        self.assertAlmostEqual(r["power_w"], 221.5)
        self.assertAlmostEqual(r["energy_kwh"], 1.234)

    def test_tuya_custom_dps_and_no_energy(self):
        plug = {"dps_power": "104", "dps_energy": "", "power_scale": 1, "energy_scale": 0.001}
        r = printers.parse_tuya({"dps": {"1": True, "104": 55}}, plug)
        self.assertEqual(r["power_w"], 55)
        self.assertIsNone(r["energy_kwh"])

    def test_tuya_error(self):
        with self.assertRaises(ValueError):
            printers.parse_tuya({"Error": "Device Unreachable", "Err": "905", "Payload": None}, {})

    def test_yandex(self):
        j = {"status": "ok", "id": "abc", "name": "Розетка",
             "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}],
             "properties": [
                 {"type": "devices.properties.float", "parameters": {"instance": "voltage", "unit": "unit.volt"},
                  "state": {"instance": "voltage", "value": 229}},
                 {"type": "devices.properties.float", "parameters": {"instance": "power", "unit": "unit.watt"},
                  "state": {"instance": "power", "value": 187.3}}]}
        r = printers.parse_yandex(j)
        self.assertEqual(r["power_w"], 187.3)
        self.assertIsNone(r["energy_kwh"])
        self.assertTrue(r["on"])

    def test_yandex_missing_instance_lists_available(self):
        j = {"status": "ok", "properties": [{"parameters": {"instance": "amperage"}, "state": {"value": 1}}]}
        with self.assertRaises(ValueError) as cm:
            printers.parse_yandex(j)
        self.assertIn("amperage", str(cm.exception))


class RealSamplesTest(unittest.TestCase):
    """Сюда — реальные ответы ваших устройств (после «Проверить связь»)."""


if __name__ == "__main__":
    unittest.main()
