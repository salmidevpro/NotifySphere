import sqlite3
import socket
import ssl
import sys

DB='instance/app.db'
USER='salmidevpro@gmail.com'

# Check DB for app_password_enc
try:
    conn=sqlite3.connect(DB)
    cur=conn.cursor()
    cur.execute("SELECT id, username, app_password_enc, gmail_address FROM user WHERE username=?", (USER,))
    row=cur.fetchone()
    if not row:
        print('USER_NOT_FOUND')
    else:
        print('USER_FOUND', row[0], row[1])
        print('HAS_APP_PASSWORD', bool(row[2]))
        print('GMAIL_ADDRESS', row[3])
    conn.close()
except Exception as e:
    print('DB_ERROR', e)

# Test TCP connect to smtp.gmail.com:587
host='smtp.gmail.com'
port=587
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(10)
try:
    s.connect((host, port))
    print('TCP_CONNECT_OK', host, port)
    # try to read banner
    try:
        data=s.recv(1024).decode(errors='ignore')
        print('BANNER:', data.strip())
    except Exception:
        pass
    s.close()
except Exception as e:
    print('TCP_CONNECT_FAIL', e)

# Also test TLS handshake on 587 via STARTTLS sequence using smtplib
try:
    import smtplib
    server = smtplib.SMTP(host, port, timeout=10)
    server.set_debuglevel(0)
    code = server.noop()[0]
    print('SMTP_NOOP_CODE', code)
    server.quit()
except Exception as e:
    print('SMTP_FAIL', e)

sys.exit(0)