from flask import Flask, render_template, redirect, url_for, request, session, flash, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
from io import BytesIO
import os, random, string, boto3
from botocore.client import Config

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'fallback-for-local-dev')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///portal.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

def get_r2():
    return boto3.client(
        's3',
        endpoint_url          = os.environ.get('R2_ENDPOINT'),
        aws_access_key_id     = os.environ.get('R2_ACCESS_KEY_ID'),
        aws_secret_access_key = os.environ.get('R2_SECRET_ACCESS_KEY'),
        config                = Config(signature_version='s3v4'),
        region_name           = 'auto',
    )

R2_BUCKET = os.environ.get('R2_BUCKET_NAME', 'invoices')
db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(120), nullable=False)
    email            = db.Column(db.String(120), unique=True, nullable=False)
    password         = db.Column(db.String(255), nullable=False)
    is_admin         = db.Column(db.Boolean, default=False)
    created          = db.Column(db.DateTime, default=datetime.utcnow)
    business_name    = db.Column(db.String(200))
    business_address = db.Column(db.String(300))
    phone            = db.Column(db.String(50))
    invoices           = db.relationship('Invoice', backref='submitter', lazy=True)
    load_confirmations = db.relationship('LoadConfirmation', backref='contractor', lazy=True)

class Invoice(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    original = db.Column(db.String(255), nullable=False)
    user_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    uploaded = db.Column(db.DateTime, default=datetime.utcnow)
    status   = db.Column(db.String(50), default='Pending')

class Route(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    from_loc       = db.Column(db.String(200), nullable=False)  # full address
    to_loc         = db.Column(db.String(200), nullable=False)  # full address
    shipper        = db.Column(db.String(200), nullable=False)  # shipper name at pickup
    rate           = db.Column(db.Numeric(10, 2), nullable=False)
    fuel_surcharge = db.Column(db.Numeric(10, 2), default=0)
    created        = db.Column(db.DateTime, default=datetime.utcnow)

class LoadConfirmation(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    load_date    = db.Column(db.String(20), nullable=False)
    commodity    = db.Column(db.String(200), nullable=False)
    weight       = db.Column(db.String(100), nullable=False)
    trailer_type = db.Column(db.String(100))
    load_number  = db.Column(db.String(100))   # formerly vrid
    status       = db.Column(db.String(50), default='Pending')
    r2_key       = db.Column(db.String(255))
    created      = db.Column(db.DateTime, default=datetime.utcnow)
    legs         = db.relationship('LoadLeg', backref='load', lazy=True,
                                   cascade='all, delete-orphan',
                                   order_by='LoadLeg.position')

class LoadLeg(db.Model):
    """One route leg within a load confirmation."""
    id       = db.Column(db.Integer, primary_key=True)
    load_id  = db.Column(db.Integer, db.ForeignKey('load_confirmation.id'), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    route_id = db.Column(db.Integer, db.ForeignKey('route.id'), nullable=False)
    route    = db.relationship('Route')

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

def generate_order_number():
    while True:
        num = ''.join(random.choices(string.digits, k=4)) + random.choice(string.ascii_uppercase)
        if not LoadConfirmation.query.filter_by(order_number=num).first():
            return num

# ── PDF Builder ───────────────────────────────────────────────────────────────

SPECIAL_INSTRUCTIONS = [
    "Charges may apply for late pick-ups and deliveries.",
    "It is the driver's responsibility to ensure that the load is safe, secure and legal for transport.",
    "Driver is required to check call daily by 10:00AM. If not, $50.00 will be charged.",
    "All Trailers must be clean, empty and odor free with no holes.",
    "Any deviation from dispatch instructions must be called in immediately.",
    "All products SHORTAGES must be reported at time of PICKUP. Failure to report will result in additional charges.",
    "Re-brokering, assigning or interlining of this shipment will void our obligation to pay your freight.",
]

def build_pdf(load):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    bold   = ParagraphStyle('bold',   parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9)
    normal = ParagraphStyle('normal', parent=styles['Normal'], fontName='Helvetica', fontSize=9)
    small  = ParagraphStyle('small',  parent=styles['Normal'], fontName='Helvetica', fontSize=8)
    center = ParagraphStyle('center', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER)

    contractor = load.contractor
    story = []

    # Header
    story.append(Paragraph("LOAD CONFIRMATION &amp; RATE AGREEMENT", center))
    story.append(Spacer(1, 6))
    hdr = Table([[Paragraph(f"<b>DATE:</b> {load.load_date}", normal),
                  Paragraph(f"<b>ORDER:</b> {load.order_number}", normal)]],
                colWidths=[3.5*inch, 3.5*inch])
    hdr.setStyle(TableStyle([('ALIGN', (1,0), (1,0), 'RIGHT')]))
    story.append(hdr)
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.black))
    story.append(Spacer(1, 6))

    # Company + carrier
    info = Table([[
        Paragraph("<b>Viveck Aryan Transport Inc.</b><br/>"
                  "50 Wright Cres, Niagara on the Lake, ON, L0S-1J0<br/>"
                  "Point of Contact: 647-923-8880<br/>"
                  "viveck.aryan.trans@gmail.com", normal),
        Paragraph(f"<b>Carrier:</b> {contractor.business_name or contractor.name}<br/>"
                  f"{contractor.business_address or ''}<br/>"
                  f"Phone: {contractor.phone or ''}", normal),
    ]], colWidths=[3.5*inch, 3.5*inch])
    info.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(1,0),(1,0),20)]))
    story.append(info)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))

    # Special instructions
    story.append(Paragraph("<b>Special Instructions:</b>", bold))
    story.append(Spacer(1, 3))
    for inst in SPECIAL_INSTRUCTIONS:
        story.append(Paragraph(f"• {inst}", small))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))

    # Load info header
    story.append(Paragraph("<b>LOAD INFORMATION</b>", bold))
    story.append(Spacer(1, 4))

    # Commodity / weight / trailer row
    meta_rows = [
        [Paragraph("<b>Commodity</b>", small), Paragraph("<b>Weight</b>", small), Paragraph("<b>Trailer</b>", small)],
        [Paragraph(load.commodity, small), Paragraph(load.weight, small), Paragraph(load.trailer_type or '', small)],
    ]
    mt = Table(meta_rows, colWidths=[2.33*inch, 2.33*inch, 2.33*inch])
    mt.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f0f0f0')),
        ('GRID',(0,0),(-1,-1),0.5,colors.grey),
        ('FONTSIZE',(0,0),(-1,-1),8),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
    ]))
    story.append(mt)
    story.append(Spacer(1, 8))

    # Route legs
    for i, leg in enumerate(load.legs):
        r = leg.route
        label = "Pickup" if i == 0 else f"Drop {i} / Re-Pickup"
        leg_rows = [
            [Paragraph(f"<b>{label}</b>", small),
             Paragraph("<b>Shipper</b>", small),
             Paragraph("<b>From</b>", small),
             Paragraph("<b>To</b>", small),
             Paragraph("<b>Rate</b>", small),
             Paragraph("<b>Fuel</b>", small)],
            [Paragraph(load.load_date, small),
             Paragraph(r.shipper, small),
             Paragraph(r.from_loc, small),
             Paragraph(r.to_loc, small),
             Paragraph(f"${float(r.rate):.2f}", small),
             Paragraph(f"${float(r.fuel_surcharge):.2f}", small)],
        ]
        lt = Table(leg_rows, colWidths=[0.8*inch,1.1*inch,1.6*inch,1.6*inch,0.85*inch,0.85*inch])
        lt.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f0f0f0')),
            ('GRID',(0,0),(-1,-1),0.5,colors.grey),
            ('FONTSIZE',(0,0),(-1,-1),8),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ]))
        story.append(lt)
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 4))

    # Rate summary
    total_rate = sum(float(leg.route.rate) for leg in load.legs)
    total_fuel = sum(float(leg.route.fuel_surcharge) for leg in load.legs)
    total      = total_rate + total_fuel
    rt = Table([[Paragraph("<b>Agreed Rate</b>", bold),
                 Paragraph(f"${total_rate:.2f} + ${total_fuel:.2f} fuel surcharge", normal),
                 Paragraph(f"<b>TOTAL: ${total:.2f}</b>", bold)]],
               colWidths=[1.5*inch, 3.5*inch, 2*inch])
    rt.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f5f5f5')),
        ('BOX',(0,0),(-1,-1),1,colors.black),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ('ALIGN',(2,0),(2,0),'RIGHT'),
    ]))
    story.append(rt)
    story.append(Spacer(1, 6))

    ln_text = f"  <b>Load #:</b> {load.load_number}" if load.load_number else ""
    story.append(Paragraph(f"To be paid 30 days from receipt of invoice.{ln_text}", small))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Spacer(1, 6))

    story.append(Paragraph(
        "<b>Invoicing Instructions:</b> Settlements paid within 30 days from the date we receive your invoice. "
        "All invoices must include a SIGNED DELIVERY RECEIPT, BOL and ORDER # and be sent to the address above. "
        "THIS AGREEMENT MUST BE SIGNED AND EMAILED TO viveck.aryan.trans@gmail.com", small))
    story.append(Spacer(1, 10))

    # Signature — carrier only
    sig = Table([
        [Paragraph(f"<b>CARRIER: {contractor.business_name or contractor.name}</b>", small)],
        [Spacer(1, 30)],
        [Paragraph("NAME: ___________________________", small)],
        [Paragraph("TITLE: __________________________", small)],
        [Paragraph("SIGNATURE: ______________________", small)],
    ], colWidths=[7*inch])
    sig.setStyle(TableStyle([
        ('BOX',(0,0),(-1,-1),0.5,colors.grey),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
        ('LEFTPADDING',(0,0),(-1,-1),8),
    ]))
    story.append(sig)

    doc.build(story)
    buf.seek(0)
    return buf

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_dashboard' if session.get('is_admin') else 'dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name       = request.form.get('name','').strip()
        email      = request.form.get('email','').strip().lower()
        pw         = request.form.get('password','')
        pw_confirm = request.form.get('password_confirm','')
        if not name or not email or not pw or not pw_confirm:
            flash('All fields are required.','error')
            return render_template('register.html')
        if pw != pw_confirm:
            flash('Passwords do not match.','error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.','error')
            return render_template('register.html')
        user = User(name=name, email=email, password=generate_password_hash(pw))
        db.session.add(user)
        db.session.commit()
        flash('Account created! Please log in.','success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','')
        user  = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, pw):
            session['user_id']   = user.id
            session['user_name'] = user.name
            session['is_admin']  = user.is_admin
            return redirect(url_for('admin_dashboard' if user.is_admin else 'dashboard'))
        flash('Invalid email or password.','error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Profile ───────────────────────────────────────────────────────────────────

@app.route('/profile', methods=['GET','POST'])
@login_required
def profile():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        user.business_name    = request.form.get('business_name','').strip()
        user.business_address = request.form.get('business_address','').strip()
        user.phone            = request.form.get('phone','').strip()
        db.session.commit()
        flash('Profile updated.','success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)

# ── Subcontractor ─────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    invoices = Invoice.query.filter_by(user_id=session['user_id']).order_by(Invoice.uploaded.desc()).all()
    loads    = LoadConfirmation.query.filter_by(user_id=session['user_id']).order_by(LoadConfirmation.created.desc()).all()
    return render_template('dashboard.html', invoices=invoices, loads=loads)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'invoice' not in request.files:
        flash('No file selected.','error'); return redirect(url_for('dashboard'))
    f = request.files['invoice']
    if f.filename == '':
        flash('No file selected.','error'); return redirect(url_for('dashboard'))
    if not allowed_file(f.filename):
        flash('Only PDF, PNG, JPG files are allowed.','error'); return redirect(url_for('dashboard'))
    original  = secure_filename(f.filename)
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    r2_key    = f"invoices/{session['user_id']}_{timestamp}_{original}"
    try:
        get_r2().upload_fileobj(f, R2_BUCKET, r2_key)
    except Exception as e:
        flash('File upload failed. Please try again.','error')
        app.logger.error(f"R2 upload error: {e}")
        return redirect(url_for('dashboard'))
    db.session.add(Invoice(filename=r2_key, original=original, user_id=session['user_id']))
    db.session.commit()
    flash('Invoice uploaded successfully!','success')
    return redirect(url_for('dashboard'))

@app.route('/load/new', methods=['GET','POST'])
@login_required
def new_load():
    user = User.query.get(session['user_id'])
    if not user.business_name or not user.business_address or not user.phone:
        flash('Please complete your business profile before creating a load confirmation.','error')
        return redirect(url_for('profile'))
    routes = Route.query.order_by(Route.from_loc).all()
    if not routes:
        flash('No routes have been set up yet. Please contact the admin.','error')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        load_date    = request.form.get('load_date','').strip()
        commodity    = request.form.get('commodity','').strip()
        weight       = request.form.get('weight','').strip()
        trailer_type = request.form.get('trailer_type','').strip()
        route_ids    = request.form.getlist('route_id[]')
        route_ids    = [r for r in route_ids if r and r != '0']
        if not load_date or not commodity or not weight:
            flash('Date, commodity and weight are required.','error')
            return render_template('new_load.html', routes=routes)
        if not route_ids:
            flash('Please add at least one route leg.','error')
            return render_template('new_load.html', routes=routes)
        load = LoadConfirmation(
            order_number=generate_order_number(), user_id=session['user_id'],
            load_date=load_date, commodity=commodity,
            weight=weight, trailer_type=trailer_type)
        db.session.add(load)
        db.session.flush()
        for i, rid in enumerate(route_ids):
            db.session.add(LoadLeg(load_id=load.id, position=i+1, route_id=int(rid)))
        db.session.commit()
        flash(f'Load {load.order_number} submitted for review.','success')
        return redirect(url_for('dashboard'))
    return render_template('new_load.html', routes=routes)

@app.route('/load/<int:load_id>/download')
@login_required
def download_load(load_id):
    load = LoadConfirmation.query.get_or_404(load_id)
    if not session.get('is_admin') and load.user_id != session['user_id']:
        abort(403)
    if load.status != 'Confirmed' or not load.r2_key:
        flash('This load has not been confirmed yet.','error')
        return redirect(url_for('dashboard'))
    try:
        url = get_r2().generate_presigned_url('get_object',
              Params={'Bucket': R2_BUCKET, 'Key': load.r2_key}, ExpiresIn=3600)
        return redirect(url)
    except Exception as e:
        app.logger.error(f"R2 presign error: {e}")
        flash('Could not generate download link.','error')
        return redirect(url_for('dashboard'))

# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    invoices    = Invoice.query.order_by(Invoice.uploaded.desc()).all()
    contractors = User.query.filter_by(is_admin=False).order_by(User.name).all()
    loads       = LoadConfirmation.query.order_by(LoadConfirmation.created.desc()).all()
    return render_template('admin.html', invoices=invoices, contractors=contractors, loads=loads)

@app.route('/admin/status/<int:invoice_id>', methods=['POST'])
@login_required
@admin_required
def update_status(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    new_status = request.form.get('status')
    if new_status in ('Pending','Reviewed','Paid'):
        invoice.status = new_status
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete/<int:invoice_id>', methods=['POST'])
@login_required
@admin_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    try: get_r2().delete_object(Bucket=R2_BUCKET, Key=invoice.filename)
    except Exception as e: app.logger.error(f"R2 delete error: {e}")
    db.session.delete(invoice)
    db.session.commit()
    flash('Invoice deleted.','success')
    return redirect(url_for('admin_dashboard'))

@app.route('/download/<int:invoice_id>')
@login_required
def download(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if not session.get('is_admin') and invoice.user_id != session['user_id']:
        abort(403)
    try:
        url = get_r2().generate_presigned_url('get_object',
              Params={'Bucket': R2_BUCKET, 'Key': invoice.filename}, ExpiresIn=3600)
        return redirect(url)
    except Exception as e:
        app.logger.error(f"R2 presign error: {e}")
        flash('Could not generate download link.','error')
        return redirect(url_for('dashboard'))

@app.route('/admin/load/<int:load_id>', methods=['GET','POST'])
@login_required
@admin_required
def review_load(load_id):
    load   = LoadConfirmation.query.get_or_404(load_id)
    routes = Route.query.order_by(Route.from_loc).all()
    if request.method == 'POST':
        action = request.form.get('action')
        load.load_number  = request.form.get('load_number','').strip() or None
        load.commodity    = request.form.get('commodity','').strip()
        load.weight       = request.form.get('weight','').strip()
        load.trailer_type = request.form.get('trailer_type','').strip()
        load.load_date    = request.form.get('load_date','').strip()
        db.session.commit()
        if action == 'confirm':
            try:
                pdf_buf = build_pdf(load)
                r2_key  = f"load_confirmations/{load.order_number}.pdf"
                get_r2().upload_fileobj(pdf_buf, R2_BUCKET, r2_key)
                load.r2_key = r2_key
                load.status = 'Confirmed'
                db.session.commit()
                flash(f'Load {load.order_number} confirmed and PDF generated.','success')
            except Exception as e:
                app.logger.error(f"PDF/R2 error: {e}")
                flash(f'Error generating PDF: {e}','error')
        else:
            flash('Load details updated.','success')
        return redirect(url_for('admin_dashboard'))
    return render_template('review_load.html', load=load, routes=routes)

@app.route('/admin/load/<int:load_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_load(load_id):
    load = LoadConfirmation.query.get_or_404(load_id)
    if load.r2_key:
        try: get_r2().delete_object(Bucket=R2_BUCKET, Key=load.r2_key)
        except Exception as e: app.logger.error(f"R2 delete error: {e}")
    db.session.delete(load)
    db.session.commit()
    flash('Load confirmation deleted.','success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/routes')
@login_required
@admin_required
def manage_routes():
    routes = Route.query.order_by(Route.from_loc).all()
    return render_template('routes.html', routes=routes)

@app.route('/admin/routes/add', methods=['POST'])
@login_required
@admin_required
def add_route():
    from_loc = request.form.get('from_loc','').strip()
    to_loc   = request.form.get('to_loc','').strip()
    shipper  = request.form.get('shipper','').strip()
    rate     = request.form.get('rate','').strip()
    fuel     = request.form.get('fuel_surcharge','0').strip()
    if not from_loc or not to_loc or not shipper or not rate:
        flash('All route fields are required.','error')
        return redirect(url_for('manage_routes'))
    db.session.add(Route(from_loc=from_loc, to_loc=to_loc, shipper=shipper,
                         rate=float(rate), fuel_surcharge=float(fuel or 0)))
    db.session.commit()
    flash('Route added.','success')
    return redirect(url_for('manage_routes'))

@app.route('/admin/routes/delete/<int:route_id>', methods=['POST'])
@login_required
@admin_required
def delete_route(route_id):
    db.session.delete(Route.query.get_or_404(route_id))
    db.session.commit()
    flash('Route deleted.','success')
    return redirect(url_for('manage_routes'))

# ── Init ──────────────────────────────────────────────────────────────────────

def seed_admin():
    if not User.query.filter_by(is_admin=True).first():
        admin = User(
            name     = 'Admin',
            email    = os.environ.get('ADMIN_EMAIL', 'admin@portal.com'),
            password = generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'admin123')),
            is_admin = True)
        db.session.add(admin)
        db.session.commit()
        print(f"✓ Admin seeded → {admin.email}")

with app.app_context():
    db.create_all()
    seed_admin()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
