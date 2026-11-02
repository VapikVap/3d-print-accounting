import unittest

from app import logic


class LogicTest(unittest.TestCase):
    def test_mm_to_grams_pla(self):
        # 1 м PLA 1.75 мм ≈ 2.98 г (общеизвестная справочная величина)
        self.assertAlmostEqual(logic.mm_to_grams(1000, 1.75, 1.24), 2.98, places=2)

    def test_energy_counter(self):
        d, src = logic.energy_delta_kwh({"ts": 0, "power_w": 100, "energy_kwh": 1.0},
                                        {"ts": 5, "power_w": 100, "energy_kwh": 1.002})
        self.assertEqual(src, "counter")
        self.assertAlmostEqual(d, 0.002)

    def test_energy_power_integration(self):
        d, src = logic.energy_delta_kwh({"ts": 0, "power_w": 200, "energy_kwh": None},
                                        {"ts": 36, "power_w": 200, "energy_kwh": None})
        self.assertEqual(src, "power")
        self.assertAlmostEqual(d, 0.002)

    def test_energy_counter_reset_falls_back_to_power(self):
        d, src = logic.energy_delta_kwh({"ts": 0, "power_w": 100, "energy_kwh": 5.0},
                                        {"ts": 36, "power_w": 100, "energy_kwh": 0.0})
        self.assertEqual(src, "power")
        self.assertAlmostEqual(d, 0.001)

    def test_energy_gap_is_capped(self):
        d, _ = logic.energy_delta_kwh({"ts": 0, "power_w": 1000}, {"ts": 3600, "power_w": 1000}, max_gap_s=60)
        self.assertAlmostEqual(d, 1000 * 60 / 3600 / 1000)

    def test_costs(self):
        job = {"filament_g": 100, "duration_s": 7200, "energy_kwh": 0.5, "energy_source": "counter"}
        c = logic.compute_costs(job, {"depreciation_hour": 10, "avg_power_w": 150},
                                {"price_kg": 1500}, {"tariff_kwh": "6", "markup_percent": "50",
                                                     "labor_hour": "0", "labor_minutes": "0"})
        self.assertEqual(c["cost_filament"], 150.0)
        self.assertEqual(c["cost_energy"], 3.0)
        self.assertEqual(c["cost_depreciation"], 20.0)
        self.assertEqual(c["cost_total"], 173.0)
        self.assertEqual(c["price"], 259.5)

    def test_costs_estimate_energy_without_plug(self):
        job = {"filament_g": 0, "duration_s": 3600, "energy_kwh": 0, "energy_source": ""}
        c = logic.compute_costs(job, {"avg_power_w": 200}, None, {"tariff_kwh": "5"})
        self.assertEqual(c["cost_energy"], 1.0)


if __name__ == "__main__":
    unittest.main()
