from flask import Flask, render_template, redirect, url_for, request, session, flash, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
import os

app = Flask(__name__)
app.secret_key = 'change-this-in-production-use-secrets-module'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///portal.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(120), nullable=False)
    email    = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created  = db.Column(db.DateTime, default=datetime.utcnow)
    invoices = db.relationship('Invoice', backref='submitter', lazy=True)

class Invoice(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    filename   = db.Column(db.String(255), nullable=False)
    original   = db.Column(db.String(255), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    uploaded   = db.Column(db.DateTime, default=datetime.utcnow)
    status     = db.Column(db.String(50), default='Pending')   # Pending / Reviewed / Paid

# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to continue.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard' if session.get('is_admin') else 'dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name  = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')

        if not name or not email or not pw:
            flash('All fields are required.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return render_template('register.html')

        user = User(name=name, email=email, password=generate_password_hash(pw))
        db.session.add(user)
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')
        user  = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, pw):
            session['user_id']  = user.id
            session['user_name'] = user.name
            session['is_admin'] = user.is_admin
            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Subcontractor routes ──────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    invoices = Invoice.query.filter_by(user_id=session['user_id']).order_by(Invoice.uploaded.desc()).all()
    return render_template('dashboard.html', invoices=invoices)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'invoice' not in request.files:
        flash('No file selected.', 'error')
        return redirect(url_for('dashboard'))

    f = request.files['invoice']
    if f.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('dashboard'))

    if not allowed_file(f.filename):
        flash('Only PDF, PNG, JPG files are allowed.', 'error')
        return redirect(url_for('dashboard'))

    original  = secure_filename(f.filename)
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    filename  = f"{session['user_id']}_{timestamp}_{original}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    invoice = Invoice(filename=filename, original=original, user_id=session['user_id'])
    db.session.add(invoice)
    db.session.commit()
    flash('Invoice uploaded successfully!', 'success')
    return redirect(url_for('dashboard'))

# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    invoices = Invoice.query.order_by(Invoice.uploaded.desc()).all()
    contractors = User.query.filter_by(is_admin=False).order_by(User.name).all()
    return render_template('admin.html', invoices=invoices, contractors=contractors)

@app.route('/admin/status/<int:invoice_id>', methods=['POST'])
@login_required
@admin_required
def update_status(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    new_status = request.form.get('status')
    if new_status in ('Pending', 'Reviewed', 'Paid'):
        invoice.status = new_status
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete/<int:invoice_id>', methods=['POST'])
@login_required
@admin_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], invoice.filename)
    if os.path.exists(filepath):
        os.remove(filepath)
    db.session.delete(invoice)
    db.session.commit()
    flash('Invoice deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/download/<int:invoice_id>')
@login_required
def download(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    # Subcontractors can only download their own; admins can download all
    if not session.get('is_admin') and invoice.user_id != session['user_id']:
        abort(403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], invoice.filename, as_attachment=True, download_name=invoice.original)

# ── Init ──────────────────────────────────────────────────────────────────────

def seed_admin():
    if not User.query.filter_by(is_admin=True).first():
        admin = User(
            name     = 'Admin',
            email    = 'admin@portal.com',
            password = generate_password_hash('admin123'),
            is_admin = True
        )
        db.session.add(admin)
        db.session.commit()
        print("✓ Admin seeded  →  admin@portal.com / admin123")

with app.app_context():
    db.create_all()
    seed_admin()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
