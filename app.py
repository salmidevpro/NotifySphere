import os
import re
import json
import urllib.request
import urllib.error
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, abort, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge, HTTPException
from werkzeug.utils import secure_filename
import openpyxl
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
import threading

# --- Authentication and DB imports ---
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet

from sqlalchemy import text

# Load environment variables from .env when present
load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
# SQLAlchemy (simple local DB for users)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fernet key for encrypting stored app passwords and tokens.
# Persist the key to .env so saved passwords remain decryptable across restarts.
dotenv_path = Path(__file__).resolve().parent / '.env'
FERNET_KEY = os.environ.get('FERNET_KEY')
if FERNET_KEY:
    try:
        # Validate provided key before use
        Fernet(FERNET_KEY.encode())
    except Exception:
        print('Warning: invalid FERNET_KEY in environment. Generating a new Fernet key for this session.')
        FERNET_KEY = Fernet.generate_key().decode()
        try:
            with open(dotenv_path, 'a', encoding='utf-8') as f:
                f.write(f"\nFERNET_KEY={FERNET_KEY}\n")
        except Exception as e:
            print(f'Warning: could not persist new FERNET_KEY to .env: {e}')
else:
    # generate a key for development if not provided
    FERNET_KEY = Fernet.generate_key().decode()
    try:
        with open(dotenv_path, 'a', encoding='utf-8') as f:
            f.write(f"\nFERNET_KEY={FERNET_KEY}\n")
    except Exception as e:
        print(f'Warning: could not persist generated FERNET_KEY to .env: {e}')
fernet = Fernet(FERNET_KEY.encode())

# Initialize DB and login manager
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

FLUTTERWAVE_PUBLIC_KEY = os.environ.get('FLUTTERWAVE_PUBLIC_KEY', 'FLWPUBK-52cc9e6d386bc513bc8709a4ad31735c-X')
# NOTE: Do NOT store or default a live secret key in source. Leave blank and set via environment.
FLUTTERWAVE_SECRET_KEY = os.environ.get('FLUTTERWAVE_SECRET_KEY', '')

@app.before_request
def require_payment():
    if current_user and getattr(current_user, 'is_authenticated', False):
        # Admin users should not be blocked by the paywall.
        if current_user.is_admin:
            return
        # Keep the main service available for the developer account.
        if current_user.gmail_address == 'salmidevpro@gmail.com':
            return
        if not current_user.is_paid:
            allowed_endpoints = {
                'payment',
                'payment_verify',
                'logout',
                'static',
                'login',
                'register',
                'index'
            }
            if request.endpoint not in allowed_endpoints:
                return redirect(url_for('payment'))
            if request.endpoint == 'index' and request.path == '/' and request.method == 'GET':
                return redirect(url_for('payment'))

# --- Simple in-memory job store for background sending (dev) ---
jobs = {}
jobs_lock = threading.Lock()
job_counter = 0

def new_job_id():
    global job_counter
    with jobs_lock:
        job_counter += 1
        jid = f"job-{job_counter}"
        jobs[jid] = {
            'status': 'queued',
            'total': 0,
            'sent': 0,
            'sent_emails': [],
            'failed': 0,
            'errors': [],
            'started_at': None,
            'finished_at': None
        }
        return jid

def update_job(jid, **kwargs):
    with jobs_lock:
        if jid in jobs:
            jobs[jid].update(kwargs)

def get_job(jid):
    with jobs_lock:
        return jobs.get(jid)

# Create upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Serve images placed in the repo-level `img/` folder (e.g. email-favicon.jpeg)
@app.route('/img/<path:filename>')
def img_file(filename):
    img_dir = Path(__file__).resolve().parent / 'img'
    return send_from_directory(str(img_dir), filename)

ALLOWED_EXCEL = {'xlsx', 'xls'}
ALLOWED_PDF = {'pdf'}


# --- User model ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    gmail_address = db.Column(db.String(256), nullable=True)
    refresh_token_enc = db.Column(db.Text, nullable=True)
    app_password_enc = db.Column(db.Text, nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    paid_until = db.Column(db.DateTime, nullable=True)
    template = db.Column(db.Text, nullable=True)
    subject = db.Column(db.String(256), nullable=True)

    @property
    def is_paid(self):
        if self.is_admin:
            return True
        if self.gmail_address == 'salmidevpro@gmail.com':
            return True
        return bool(self.paid_until and self.paid_until > datetime.utcnow())

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def set_refresh_token(self, token_plain):
        if token_plain is None:
            self.refresh_token_enc = None
        else:
            self.refresh_token_enc = fernet.encrypt(token_plain.encode()).decode()

    def get_refresh_token(self):
        if not self.refresh_token_enc:
            return None
        try:
            return fernet.decrypt(self.refresh_token_enc.encode()).decode()
        except Exception:
            return None

    def set_app_password(self, pwd_plain):
        if pwd_plain is None:
            self.app_password_enc = None
        else:
            self.app_password_enc = fernet.encrypt(pwd_plain.encode()).decode()

    def get_app_password(self):
        if not self.app_password_enc:
            return None
        try:
            return fernet.decrypt(self.app_password_enc.encode()).decode()
        except Exception:
            return None


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tx_id = db.Column(db.String(128), unique=True, nullable=False)
    tx_ref = db.Column(db.String(128), nullable=True)
    amount = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(16), nullable=True)
    status = db.Column(db.String(64), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    customer_email = db.Column(db.String(256), nullable=True)
    raw_response = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --- Ensure DB and migrations for simple dev workflow ---
with app.app_context():
    db.create_all()
    insp = db.inspect(db.engine)
    cols = [c['name'] for c in insp.get_columns('user')]
    if 'app_password_enc' not in cols:
        try:
            # SQLite and many DBs support simple ADD COLUMN
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN app_password_enc TEXT'))
                conn.commit()
            print('Migrated: added app_password_enc column')
        except Exception:
            print('Warning: automatic migration failed; run Alembic to add app_password_enc')
    if 'is_admin' not in cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN is_admin BOOLEAN DEFAULT 0'))
                conn.commit()
            print('Migrated: added is_admin column')
        except Exception:
            print('Warning: automatic migration failed; run Alembic to add is_admin')
    if 'paid_until' not in cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN paid_until DATETIME'))
                conn.commit()
            print('Migrated: added paid_until column')
        except Exception:
            print('Warning: automatic migration failed; run Alembic to add paid_until')
    if 'subject' not in cols:
        try:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN subject VARCHAR(256)'))
                conn.commit()
            print('Migrated: added subject column')
        except Exception:
            print('Warning: automatic migration failed; run Alembic to add subject')

def extract_name_from_email(email):
    """Extract name from email by removing domain"""
    name = email.split('@')[0]
    return name

def get_emails_from_excel(file_path):
    """Read emails from Excel file (Column 2)"""
    try:
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        emails = []
        
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[1]:  # Column 2 (index 1)
                email = str(row[1]).strip()
                if email and '@' in email:
                    emails.append(email)
        
        return emails
    except Exception as e:
        raise Exception(f"Error reading Excel file: {str(e)}")

def send_email_with_pdf(sender_email, sender_password, recipient_email, recipient_name, 
                        message_template, pdf_path, subject=None, smtp_server='smtp.gmail.com', smtp_port=587):
    """Send personalized email with PDF attachment"""
    try:
        # Personalize message (support {name} and {timestamp})
        personalized_message = message_template.replace('{name}', recipient_name)
        personalized_message = personalized_message.replace('{timestamp}', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
        
        # Create message
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject or f'Message for {recipient_name}'
        
        # Attach message body
        msg.attach(MIMEText(personalized_message, 'plain'))
        
        # Attach PDF
        if os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
            
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename= {os.path.basename(pdf_path)}')
            msg.attach(part)
        else:
            raise Exception(f"PDF not found: {pdf_path}")
        
        # Send email with a larger timeout and clear error reporting
        server = None
        try:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=60)
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception as e_starttls:
                raise Exception(f"STARTTLS failed: {e_starttls}")

            try:
                server.login(sender_email, sender_password)
            except Exception as e_login:
                raise Exception(f"SMTP login failed: {e_login}")

            server.send_message(msg)
        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

        return True
    except Exception as e:
        raise Exception(f"Error sending email to {recipient_email}: {str(e)}")


@app.route('/landing')
def landing():
    """Public landing page"""
    return render_template('landing.html')

@app.route('/')
def index():
    # Show landing page if not authenticated, otherwise show dashboard
    if not (current_user and getattr(current_user, 'is_authenticated', False)):
        return render_template('landing.html')
    
    # Pass current user info to template for dashboard
    # compute subscription days left
    days_left = None
    paid_until_iso = None
    try:
        if current_user.paid_until:
            delta = current_user.paid_until - datetime.utcnow()
            days_left = max(delta.days, 0)
            paid_until_iso = current_user.paid_until.isoformat()
    except Exception:
        days_left = None

    user_info = {
        'is_authenticated': True,
        'username': current_user.username,
        'gmail_address': current_user.gmail_address,
        'has_app_password': bool(current_user.get_app_password()),
        'template': current_user.template or '',
        'subject': current_user.subject or '',
        'is_admin': bool(current_user.is_admin),
        'paid_until': paid_until_iso,
        'days_left': days_left,
        'is_paid': current_user.is_paid
    }
    return render_template('index.html', user_info=user_info)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')
    data = request.form
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return render_template('register.html', error='Missing username or password')
    if User.query.filter_by(username=username).first():
        return render_template('register.html', error='Username already exists')
    is_admin = User.query.count() == 0
    user = User(username=username, is_admin=is_admin)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return redirect(url_for('payment'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')
    data = request.form
    username = data.get('username')
    password = data.get('password')
    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return render_template('login.html', error='Invalid credentials')
    login_user(user)
    if not user.is_paid:
        return redirect(url_for('payment'))
    return redirect(url_for('index'))


@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('admin_login.html')
    data = request.form
    username = data.get('username')
    password = data.get('password')
    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password) or not user.is_admin:
        return render_template('admin_login.html', error='Invalid admin credentials')
    login_user(user)
    return redirect(url_for('admin'))


@app.route('/payment')
def payment():
    if not (current_user and getattr(current_user, 'is_authenticated', False)):
        return render_template('payment.html', user=None, public_key=FLUTTERWAVE_PUBLIC_KEY, error='Register or login first to pay for access.')
    if current_user.is_paid:
        return redirect(url_for('index'))
    return render_template('payment.html', user=current_user, public_key=FLUTTERWAVE_PUBLIC_KEY, amount=30, currency='USD')


@app.route('/payment/verify', methods=['POST'])
@login_required
def payment_verify():
    data = request.get_json() or {}
    transaction_id = data.get('transaction_id') or data.get('tx_id')
    if not transaction_id:
        return jsonify({'error': 'Missing transaction_id'}), 400
    if not FLUTTERWAVE_SECRET_KEY:
        return jsonify({'error': 'Payment configuration is not available.'}), 500

    verify_url = f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify'
    req = urllib.request.Request(verify_url, headers={
        'Authorization': f'Bearer {FLUTTERWAVE_SECRET_KEY}',
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            response_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode()
        except Exception:
            err_body = str(exc)
        return jsonify({'error': f'Unable to verify payment: {err_body}'}), 500
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

    if response_data.get('status') != 'success':
        return jsonify({'error': 'Payment verification failed.'}), 400

    transaction = response_data.get('data', {})
    tx_status = transaction.get('status')
    if tx_status != 'successful':
        return jsonify({'error': 'Payment not completed.'}), 400

    # Extract data safely
    tx_id = str(transaction.get('id') or transaction.get('transaction_id') or transaction_id)
    tx_ref = transaction.get('tx_ref') or transaction.get('txRef')
    amount_paid = float(transaction.get('amount', 0) or 0)
    currency = transaction.get('currency') or transaction.get('currency_code') or 'USD'
    customer = transaction.get('customer') or {}
    customer_email = customer.get('email') or customer.get('customer_email') or None

    # Check amount - enforce minimum expected amount
    if amount_paid < 30:
        return jsonify({'error': 'Insufficient payment amount.'}), 400

    # Idempotency: skip if we've already recorded this transaction
    existing = Payment.query.filter_by(tx_id=tx_id).first()
    if existing:
        # If already successful and linked to a user, ensure user's paid_until is up-to-date
        if existing.user_id:
            try:
                user = User.query.get(existing.user_id)
                if user:
                    if not user.paid_until or user.paid_until < datetime.utcnow():
                        user.paid_until = datetime.utcnow() + timedelta(days=180)
                        db.session.commit()
            except Exception:
                pass
        return jsonify({'success': True, 'message': 'Payment already recorded.'})

    # Create payment record
    try:
        payment = Payment(
            tx_id=tx_id,
            tx_ref=tx_ref,
            amount=amount_paid,
            currency=currency,
            status=tx_status,
            customer_email=customer_email,
            raw_response=json.dumps(response_data)
        )
        # Try to associate with current user or by customer email
        associated_user = None
        if current_user and getattr(current_user, 'is_authenticated', False):
            associated_user = current_user
        elif customer_email:
            associated_user = User.query.filter_by(gmail_address=customer_email).first()

        if associated_user:
            payment.user_id = associated_user.id
            # Ensure user has an email stored
            if not associated_user.gmail_address and customer_email:
                associated_user.gmail_address = customer_email
            # Extend or set paid_until by 180 days from now or from existing expiry
            if associated_user.paid_until and associated_user.paid_until > datetime.utcnow():
                associated_user.paid_until = associated_user.paid_until + timedelta(days=180)
            else:
                associated_user.paid_until = datetime.utcnow() + timedelta(days=180)

        db.session.add(payment)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({'error': f'Failed to record payment: {str(exc)}'}), 500

    return jsonify({'success': True, 'message': 'Payment verified and recorded. Your account is now active.'})


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'GET':
        return render_template('account.html', user=current_user)
    # Update template text and default subject
    tpl = request.form.get('template')
    subject = request.form.get('subject')
    if tpl is not None:
        current_user.template = tpl
    if subject is not None:
        current_user.subject = subject
    db.session.commit()
    return redirect(url_for('account'))


@app.route('/account/app-password', methods=['POST'])
@login_required
def account_app_password():
    pwd = request.form.get('app_password')
    if not pwd:
        return redirect(url_for('account'))
    current_user.set_app_password(pwd)
    db.session.commit()
    return redirect(url_for('account'))


@app.route('/account/email', methods=['POST'])
@login_required
def account_email():
    email = request.form.get('gmail_address')
    if email:
        current_user.gmail_address = email
        db.session.commit()
    return redirect(url_for('account'))


@app.route('/admin')
@login_required
def admin():
    if not current_user.is_admin:
        abort(403)
    users = User.query.order_by(User.username).all()
    job_list = []
    with jobs_lock:
        for jid, info in jobs.items():
            job_list.append({
                'id': jid,
                'status': info.get('status'),
                'total': info.get('total'),
                'sent': info.get('sent'),
                'failed': info.get('failed'),
                'started_at': info.get('started_at'),
                'finished_at': info.get('finished_at'),
                'errors': info.get('errors', []),
                'payload': info.get('payload', {})
            })
    return render_template('admin.html', users=users, jobs=job_list)


@app.route('/admin/jobs')
@login_required
def admin_jobs_api():
    if not current_user.is_admin:
        abort(403)
    job_list = []
    with jobs_lock:
        for jid, info in jobs.items():
            job_list.append({
                'id': jid,
                'status': info.get('status'),
                'total': info.get('total'),
                'sent': info.get('sent'),
                'failed': info.get('failed'),
                'started_at': info.get('started_at'),
                'finished_at': info.get('finished_at'),
                'errors': info.get('errors', []),
                'payload': info.get('payload', {})
            })
    return jsonify(job_list)

@app.route('/admin/toggle-admin/<int:user_id>', methods=['POST'])
@login_required
def admin_toggle_admin(user_id):
    if not current_user.is_admin:
        abort(403)
    if current_user.id == user_id:
        abort(400, 'Cannot change your own admin status')
    target = User.query.get_or_404(user_id)
    target.is_admin = not target.is_admin
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if not current_user.is_admin:
        abort(403)
    if current_user.id == user_id:
        abort(400, 'Cannot delete your own account')
    target = User.query.get_or_404(user_id)
    db.session.delete(target)
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/clear-jobs', methods=['POST'])
@login_required
def admin_clear_jobs():
    if not current_user.is_admin:
        abort(403)
    with jobs_lock:
        finished_jobs = [jid for jid, job in jobs.items() if job.get('status') == 'finished']
        for jid in finished_jobs:
            jobs.pop(jid, None)
    return redirect(url_for('admin'))

@app.route('/upload', methods=['POST'])
def upload_files():
    """Handle file uploads"""
    try:
        if 'excel_file' not in request.files or 'pdf_folder' not in request.files:
            return jsonify({'error': 'Missing files'}), 400
        
        excel_file = request.files['excel_file']
        pdf_files = request.files.getlist('pdf_folder')
        message = request.form.get('message', '')
        
        if not message:
            return jsonify({'error': 'Message template is required'}), 400
        
        # Validate and save Excel file
        if not excel_file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({'error': 'Only .xlsx and .xls files allowed'}), 400
        
        excel_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(excel_file.filename))
        excel_file.save(excel_path)
        
        # Save PDF files
        pdf_paths = {}
        pdf_folder_path = os.path.join(app.config['UPLOAD_FOLDER'], 'pdfs')
        os.makedirs(pdf_folder_path, exist_ok=True)
        
        for pdf_file in pdf_files:
            # Some browsers include the relative path in filename when uploading a folder.
            # Use only the base name and validate extension case-insensitively.
            base_name = Path(pdf_file.filename).name
            if base_name and base_name.lower().endswith('.pdf'):
                pdf_name = secure_filename(base_name)
                pdf_file_path = os.path.join(pdf_folder_path, pdf_name)
                pdf_file.save(pdf_file_path)

                # Extract email identifier from PDF filename (without extension)
                email_identifier = os.path.splitext(pdf_name)[0]
                pdf_paths[email_identifier] = pdf_file_path
        
        # Get emails from Excel
        emails = get_emails_from_excel(excel_path)
        
        # Prepare send data
        send_data = {
            'emails': emails,
            'pdf_paths': pdf_paths,
            'message': message,
            'excel_path': excel_path
        }
        
        return jsonify({
            'success': True,
            'emails_count': len(emails),
            'pdfs_count': len(pdf_paths),
            'send_data': send_data
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/send-emails', methods=['POST'])
def send_emails():
    """Send personalized emails"""
    try:
        data = request.json

        # Prefer logged-in user's stored Gmail address and app password
        sender_email = None
        sender_password = None

        if current_user and getattr(current_user, 'is_authenticated', False):
            if current_user.gmail_address:
                sender_email = current_user.gmail_address
            sender_password = current_user.get_app_password()

        # If not logged in or missing data, use supplied credentials
        if not sender_email:
            sender_email = data.get('sender_email')
        if not sender_password:
            sender_password = data.get('sender_password')

        emails = data.get('emails', [])
        pdf_paths = data.get('pdf_paths', {})
        message_template = data.get('message', '')
        subject = data.get('subject') or (current_user.subject if current_user and getattr(current_user, 'is_authenticated', False) else None)

        if not sender_email or not sender_password:
            return jsonify({'error': 'Email credentials required'}), 400
        
        results = {
            'sent': [],
            'failed': [],
            'total': len(emails)
        }
        
        for email in emails:
            try:
                name = extract_name_from_email(email)
                email_identifier = name  # PDF is named without @gmail.com
                
                # Find matching PDF
                pdf_file = None
                for key, path in pdf_paths.items():
                    if key.lower() == email_identifier.lower():
                        pdf_file = path
                        break
                
                if not pdf_file:
                    results['failed'].append({
                        'email': email,
                        'reason': f'PDF not found for {email_identifier}'
                    })
                    continue

                send_email_with_pdf(
                    sender_email,
                    sender_password,
                    email,
                    name,
                    message_template,
                    pdf_file,
                    subject=subject
                )

                results['sent'].append(email)
                
            except Exception as e:
                results['failed'].append({
                    'email': email,
                    'reason': str(e)
                })
        
        return jsonify(results), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _send_job_worker(jid, job_payload):
    # Run inside application context so DB and Flask extensions work in thread
    with app.app_context():
        update_job(jid, status='running', started_at=datetime.utcnow().isoformat())
        emails = job_payload.get('emails', [])
        pdf_paths = job_payload.get('pdf_paths', {})
        message_template = job_payload.get('message', '')
        subject = job_payload.get('subject')
        user_id = job_payload.get('user_id')

        total = len(emails)
        update_job(jid, total=total)
        sent = 0
        sent_emails = []
        failed = 0
        errors = []

        # If job is associated with a user, load credentials
        job_user = None
        sender_email = None
        sender_password = None
        if user_id:
            job_user = User.query.get(user_id)
            if job_user:
                sender_email = job_user.gmail_address
                sender_password = job_user.get_app_password()
                if not sender_email:
                    sender_email = job_payload.get('sender_email')

        for idx, email in enumerate(emails, start=1):
            try:
                name = extract_name_from_email(email)
                email_identifier = name
                # find pdf
                pdf_file = None
                for key, path in pdf_paths.items():
                    if key.lower() == email_identifier.lower():
                        pdf_file = path
                        break
                if not pdf_file:
                    raise Exception(f'PDF not found for {email_identifier}')

                send_email_with_pdf(sender_email, sender_password, email, name, message_template, pdf_file, subject=subject)

                sent += 1
                sent_emails.append(email)
            except Exception as e:
                failed += 1
                errors.append({'email': email, 'reason': str(e)})
                print(f'Error sending to {email}: {str(e)}')

            update_job(jid, sent=sent, sent_emails=sent_emails, failed=failed, errors=errors)

        update_job(jid, status='finished', finished_at=datetime.utcnow().isoformat())


@app.route('/start-send', methods=['POST'])
@login_required
def start_send():
    data = request.json or {}
    # Use user's template if empty
    if not data.get('message') and current_user.template:
        data['message'] = current_user.template
    if not data.get('subject') and current_user.subject:
        data['subject'] = current_user.subject

    jid = new_job_id()
    # store payload in job (for small jobs in-memory) and set total immediately
    total_emails = len(data.get('emails', [])) if data.get('emails') else 0
    update_job(jid, payload=data, total=total_emails)
    t = threading.Thread(target=_send_job_worker, args=(jid, data), daemon=True)
    t.start()
    return jsonify({'job_id': jid}), 202


@app.route('/job-status/<jid>')
@login_required
def job_status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(j)

@app.route('/retry-send/<jid>', methods=['POST'])
@login_required
def retry_send(jid):
    """Retry sending to failed emails from a previous job"""
    job = get_job(jid)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    errors = job.get('errors', [])
    if not errors:
        return jsonify({'error': 'No failed emails to retry'}), 400
    
    # Extract failed emails from errors
    failed_emails = [e.get('email') for e in errors if e.get('email')]
    if not failed_emails:
        return jsonify({'error': 'No failed emails to retry'}), 400
    
    # Get original job payload
    payload = job.get('payload', {})
    pdf_paths = payload.get('pdf_paths', {})
    message = payload.get('message', '')
    subject = payload.get('subject', '')

    # Prefer to reuse the original job's user id so retries use the original sender credentials
    original_user_id = None
    try:
        original_user_id = payload.get('user_id')
    except Exception:
        original_user_id = None

    # If original_user_id is not present, fall back to the current user
    user_id_for_retry = original_user_id if original_user_id else (current_user.id if current_user.is_authenticated else None)

    # Create new job for retry
    retry_jid = new_job_id()
    retry_data = {
        'emails': failed_emails,
        'pdf_paths': pdf_paths,
        'message': message,
        'subject': subject,
        'user_id': user_id_for_retry
    }
    update_job(retry_jid, payload=retry_data)
    t = threading.Thread(target=_send_job_worker, args=(retry_jid, retry_data), daemon=True)
    t.start()
    return jsonify({'job_id': retry_jid}), 202

@app.route('/preview', methods=['POST'])
def preview():
    """Preview personalized message"""
    try:
        data = request.json
        message = data.get('message', '')
        sample_email = data.get('sample_email', 'john@gmail.com')
        
        name = extract_name_from_email(sample_email)
        preview_message = message.replace('{name}', name)
        preview_message = preview_message.replace('{timestamp}', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
        
        return jsonify({
            'original': message,
            'preview': preview_message,
            'sample_name': name
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({'error': 'Uploaded file(s) too large'}), 413


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    return jsonify({'error': e.description}), e.code

if __name__ == '__main__':
    app.run(debug=True, port=5000)
