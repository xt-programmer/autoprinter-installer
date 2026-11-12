import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import peripheral_history as ph


class DetectionPolicyTests(unittest.TestCase):
    def test_driver_registration_is_not_camera_detection(self):
        e = ph.kernel_evidence("usbcore: registered new interface driver uvcvideo\nUSB Video Class driver (1.1.1)")
        self.assertFalse(e["usb_webcam"])

    def test_positive_uvc_history_triggers_camera(self):
        e = ph.kernel_evidence("uvcvideo 1-1:1.0: Found UVC 1.00 device Logi C270 HD WebCam (046d:0825)")
        state = ph.merge({"schema": 1, "devices": {}}, {}, e, now="first")
        self.assertTrue(ph.wanted(state, {})["usb_webcam"])

    def test_generic_klipper_h7_and_rom_dfu_are_not_cartographer(self):
        lines = "usb 5-1: Product: stm32h723xx\nusb 5-1: Manufacturer: Klipper\nusb 1-1: Product: STM32 BOOTLOADER"
        self.assertFalse(ph.kernel_evidence(lines)["cartographer"])

    def test_named_probe_history_is_required(self):
        self.assertTrue(ph.kernel_evidence("usb 1-2: Product: Cartographer v4")["cartographer"])
        self.assertFalse(ph.kernel_evidence("installer: Cartographer plugin was mentioned")["cartographer"])

    def test_unplug_preserves_positive_history(self):
        s = ph.merge({"schema": 1, "devices": {}}, {"cartographer": [{"evidence": "test identity"}]}, now="first")
        s = ph.merge(s, {"cartographer": [], "usb_webcam": []}, now="later")
        self.assertTrue(ph.wanted(s, {})["cartographer"])
        self.assertFalse(ph.wanted(s, {})["usb_webcam"])
        self.assertEqual(s["devices"]["cartographer"]["first_seen"], "first")

    def test_string_boolean_does_not_enable_install(self):
        self.assertFalse(ph.wanted({"devices": {"cartographer": {"seen": "false"}}}, {})["cartographer"])

    def test_corrupt_history_is_not_silently_forgotten(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "history.json"
            p.write_text('{"schema":2,"devices":{}}')
            with self.assertRaises(ValueError):
                ph.load(p)

    def test_no_devices_means_no_optional_install(self):
        self.assertEqual(ph.wanted({"schema": 1, "devices": {}}, {}), {"usb_webcam": False, "cartographer": False})

    def test_soc_decoder_does_not_trigger_usb_camera(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "usb").mkdir()
            (root / "video/video0/device").mkdir(parents=True)
            result = ph.current(root / "usb", root / "video", root / "byid", root / "serial")
            self.assertEqual(result["usb_webcam"], [])

    @unittest.skipIf(os.name == "nt", "Linux sysfs interface names contain a colon")
    def test_usb_video_interface_detected_without_opening_camera(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            p = root / "usb/1-1"
            (p / "1-1:1.0").mkdir(parents=True)
            (p / "idVendor").write_text("046d")
            (p / "idProduct").write_text("0825")
            (p / "product").write_text("Logitech camera")
            (p / "1-1:1.0/bInterfaceClass").write_text("0e")
            result = ph.current(root / "usb", root / "video", root / "byid", root / "serial")
            self.assertEqual(len(result["usb_webcam"]), 1)
            self.assertEqual(result["cartographer"], [])


if __name__ == "__main__":
    unittest.main()
