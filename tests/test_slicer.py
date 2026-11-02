"""Образцы комментариев — по форматам, которые пишут слайсеры (не с вашего ПК).
Когда появятся ваши реальные файлы, добавим их сюда как образцы."""
import os
import tempfile
import time
import unittest
import zipfile

from app import slicer

ORCA_TAIL = """
G1 X10 Y10
; filament used [mm] = 4123.45
; filament used [cm3] = 9.92
; filament used [g] = 12.30
; filament cost = 0.25
; total filament used [g] = 12.30
; estimated printing time (normal mode) = 1h 23m 4s
; CONFIG_BLOCK_START
; layer_height = 0.2
"""

CURA_HEAD = """;FLAVOR:Marlin
;TIME:5025
;Filament used: 3.21456m
;Layer height: 0.2
;Generated with Cura_SteamEngine 5.7.1
"""

BAMBU_HEAD = """; HEADER_BLOCK_START
; BambuStudio 01.09.00.70
; model printing time: 52m 10s; total estimated time: 58m 3s
; total layer number: 120
; total filament length [mm] : 2950.12
; total filament volume [cm^3] : 7096.55
; total filament weight [g] : 8.80
; HEADER_BLOCK_END
"""

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="3483"/>
    <metadata key="weight" value="8.80"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="2.95" used_g="8.80"/>
  </plate>
</config>
"""


class SlicerTest(unittest.TestCase):
    def test_orca(self):
        r = slicer.parse_gcode_text(ORCA_TAIL)
        self.assertAlmostEqual(r["filament_mm"], 4123.45)
        self.assertAlmostEqual(r["filament_g"], 12.30)
        self.assertEqual(r["est_time_s"], 3600 + 23 * 60 + 4)

    def test_cura(self):
        r = slicer.parse_gcode_text(CURA_HEAD)
        self.assertEqual(r["est_time_s"], 5025)
        self.assertAlmostEqual(r["filament_mm"], 3214.56)
        self.assertIsNone(r["filament_g"])

    def test_bambu_gcode_header(self):
        r = slicer.parse_gcode_text(BAMBU_HEAD)
        self.assertAlmostEqual(r["filament_g"], 8.80)
        self.assertAlmostEqual(r["filament_mm"], 2950.12)
        self.assertEqual(r["est_time_s"], 58 * 60 + 3)

    def test_multi_extruder_sum(self):
        r = slicer.parse_gcode_text("; filament used [g] = 1.50, 2.25\n")
        self.assertAlmostEqual(r["filament_g"], 3.75)

    def test_duration_formats(self):
        self.assertEqual(slicer.parse_duration("1d 2h 3m 4s"), 93784)
        self.assertEqual(slicer.parse_duration("01:02:03"), 3723)

    def test_3mf_and_match(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "Benchy box.gcode.3mf")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("Metadata/slice_info.config", SLICE_INFO)
            z.writestr("Metadata/plate_1.gcode", BAMBU_HEAD)
        g = os.path.join(d, "sub", "cube_PLA.gcode")
        os.makedirs(os.path.dirname(g))
        with open(g, "w", encoding="utf-8") as f:
            f.write("G28\n" * 10 + ORCA_TAIL)
        old = time.time() - 100
        os.utime(p, (old, old))

        info = slicer.parse_file(p)
        self.assertAlmostEqual(info["filament_g"], 8.80)
        self.assertEqual(info["est_time_s"], 3483)
        self.assertAlmostEqual(info["filament_mm"], 2950)

        files = slicer.scan(d)
        self.assertEqual([f["name"] for f in files], ["cube_PLA.gcode", "Benchy box.gcode.3mf"])
        # Bambu: subtask_name без расширения и с суффиксом пластины
        self.assertEqual(slicer.match(d, "Benchy box")["name"], "Benchy box.gcode.3mf")
        self.assertEqual(slicer.match(d, "Benchy box_plate_1")["name"], "Benchy box.gcode.3mf")
        # Moonraker: путь с подпапкой
        self.assertEqual(slicer.match(d, "jobs/cube_PLA.gcode")["name"], "cube_PLA.gcode")
        self.assertIsNone(slicer.match(d, "other"))


if __name__ == "__main__":
    unittest.main()
