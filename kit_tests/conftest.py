import pathlib
import sys

# The kit is vendored in the app; make it importable without installing Frappe.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "ipay_integration"))
