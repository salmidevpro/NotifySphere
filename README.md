# NotifySphere - Bulk Email with Personalized PDFs

A Flask web application to send personalized emails with PDF attachments to multiple recipients from an Excel file at scale.

## Features

- ✅ Public landing page showcasing features
- ✅ User registration and login system
- ✅ Upload Excel file with email addresses (Column 2)
- ✅ Upload multiple PDF files matched by email identifier
- ✅ Personalized message templates using {name} and {timestamp} placeholders
- ✅ Automatic name extraction from email addresses
- ✅ Send emails with PDF attachments
- ✅ Real-time progress tracking
- ✅ Beautiful UI with step-by-step guidance
- ✅ Admin dashboard for user and job monitoring
- ✅ Gmail app password support for secure email sending

## Setup Instructions

### 1. Install Python (if not installed)
Download from https://www.python.org/downloads/

### 2. Create Virtual Environment
```bash
cd "c:\Users\DELL\Desktop\Salmi\Email Sender"
python -m venv venv
venv\Scripts\activate
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` and update the values.
The app automatically loads `.env` with `python-dotenv`.
The app uses `SECRET_KEY`, optional `DATABASE_URL`, `FERNET_KEY`, and optional SMTP configuration.

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Run the Application
```bash
python app.py
```

The app will be available at `http://localhost:5000`

## How to Use

### 1. **Prepare Your Files**

**Excel File Format:**
- Column 1: Names (optional, just for reference)
- Column 2: Email addresses (required)
- Start from Row 2 (Row 1 should be headers)

Example:
```
Name          | Email
John Smith    | john@gmail.com
Jane Doe      | jane@gmail.com
```

**PDF Folder:**
- Name PDFs matching email usernames (without @gmail.com)
- Example: If email is `john@gmail.com`, name the PDF `john.pdf`

### 2. **Write Message Template**
Use `{name}` placeholder for personalization and `{timestamp}` for the current send timestamp:
```
Hello {name},

Attached is your monthly statement.

Timestamp: {timestamp}
```

### 3. **Admin Dashboard**
The first registered user becomes the initial admin and can access `/admin` to view users and job progress.

### 4. **Environment Variables**
The app supports the following optional environment variables:
- `SECRET_KEY` — Flask session signing key
- `DATABASE_URL` — SQLAlchemy database URI (default: `sqlite:///app.db`)
- `FERNET_KEY` — encryption key for stored refresh tokens and app passwords
- `FERNET_KEY` — encryption key for stored app passwords

### 5. **Get Gmail App Password**
1. Go to https://myaccount.google.com
2. Security → 2-Step Verification (enable if needed)
3. Security → App passwords → Generate
4. Select "Mail" and "Windows Computer"
5. Copy the generated password

### 4. **Upload and Send**
1. Upload Excel file
2. Select all PDF files
3. Enter your Gmail address and app password
4. Click "Start Sending"

## Troubleshooting

### PDFs not found?
- Ensure PDF filenames match email usernames exactly (case-insensitive)
- Example: `john@gmail.com` → `john.pdf`

### Gmail authentication fails?
- Use Gmail app password, NOT your regular Gmail password
- Enable 2-Step Verification first if needed

### Port 5000 already in use?
```bash
netstat -ano | findstr :5000
taskkill /PID <PID> /F
```

## File Structure
```
Email Sender/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── Procfile               # Render startup command
├── .env.example          # Environment variables template
├── README.md             # This file
├── templates/
│   └── index.html        # Web interface
└── uploads/              # Temporary upload folder (auto-created)
    └── pdfs/            # PDF storage
```

## Render deployment

This app can deploy on Render without Docker using the Python environment and a startup command.

### Render steps

1. Push this repo to GitHub.
2. Sign in to https://dashboard.render.com and create a new Web Service.
3. Connect your GitHub repo and choose the branch.
4. In service settings:
   - Environment: `Docker` or `Python` (choose Python if available)
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --log-level info`
5. Add environment variables in Render:
   - `SECRET_KEY`
   - `FERNET_KEY`
   - `DATABASE_URL` (optional; default is `sqlite:///app.db`)
   - `FLUTTERWAVE_SECRET_KEY` if you need payment verification
6. Deploy and open the rendered URL.

### Important notes

- Render web service storage is ephemeral by default. Use a managed database for reliable persistence.
- If you want persistent data on Render, use a managed PostgreSQL database and set `DATABASE_URL` accordingly.

## Security Notes

- ⚠️ Never commit `.env` with real credentials
- Use Gmail app passwords instead of real passwords
- The app runs locally on http://localhost:5000 (not accessible from outside)
- Uploaded files are stored temporarily and should be cleaned periodically

## Limitations

- Max 500MB total upload size (configurable in app.py)
- Requires 2-Step Verification enabled on Gmail
- One-way email sending (no replies tracking)
