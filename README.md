# HaulHub — Subcontractor Invoice Portal

A Flask + SQLite web app for managing subcontractor invoice submissions.

## Setup

```bash
cd truckportal

# Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

Open http://localhost:5000 in your browser.

## Default Admin Account

| Field    | Value              |
|----------|--------------------|
| Email    | admin@portal.com   |
| Password | admin123           |

**Change these in app.py before going live.**

## Project Structure

```
truckportal/
├── app.py              # Main application
├── requirements.txt
├── uploads/            # Uploaded invoice files (auto-created)
├── instance/
│   └── portal.db       # SQLite database (auto-created)
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── dashboard.html   # Subcontractor view
    └── admin.html       # Admin view
```

## Features

- Subcontractors self-register and log in
- Upload invoices (PDF, PNG, JPG — up to 16 MB)
- Admin sees all invoices with contractor name + email
- Admin can mark invoices as Pending / Reviewed / Paid
- Admin can download or delete invoices
- Stats summary (total, pending, reviewed, paid, contractors)

## Planned Features (next steps)

- Weekly load tracking
- Auto-confirm loads
- Email notifications on upload
- Invoice amount + load date fields
- Export to CSV/Excel for tax reporting
