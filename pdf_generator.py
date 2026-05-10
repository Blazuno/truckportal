from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
import io

SPECIAL_INSTRUCTIONS = [
    "Charges may apply for late pick-ups and deliveries",
    "It is the driver's responsibility to ensure that the load is safe, secure and legal for transport.",
    "Driver is required to check call daily by 10:00AM. If not, $50.00 will be charged.",
    "All Trailers must be clean, empty and odor free with no holes.",
    "Any deviation from dispatch instructions must be called in immediately.",
    "All products SHORTAGES must be reported at time of PICKUP. Failure to report will result in additional charges.",
    "Re-brokering, assigning or interlining of this shipment will void our obligation to pay our freight.",
]

def generate_load_confirmation_pdf(load):
    """